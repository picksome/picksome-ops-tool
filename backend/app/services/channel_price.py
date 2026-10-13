"""판매처 listing / 가격 서비스 (M022).

핵심 원칙
--------
**가격은 UPDATE 하지 않는다. 새 버전을 INSERT 한다.**
과거 가격을 덮어쓰면 "그때 얼마에 팔았나" 를 잃는다. 잘못된 가격을 고치는 것도
정정 버전 생성이다.

기간 중복은 **DB exclusion constraint** 가 막는다. 여기서 미리 확인하는 것은
사용자에게 제약 이름 대신 문장을 보여주기 위해서고, 방어선은 DB 다 —
서비스 검증만으로는 동시 요청 두 개가 각자 통과해 둘 다 들어간다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db_errors import constraint_name_of
from app.core.exceptions import (
    ChannelPriceImmutableError,
    ChannelPriceOverlapError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.ids import new_id
from app.domain.channel_price import (
    LISTING_STATUSES,
    ChannelListingCreate,
    ChannelListingOut,
    ChannelListingUpdate,
    ChannelPriceIn,
    ChannelPriceOut,
    ResolvedPrice,
)
from app.services import channel_connector

_L_SELECT = """
    SELECT l.id, l.org_id, l.integration_account_id, l.product_id,
           l.external_product_id, l.external_product_url, l.status,
           l.auto_publish_price, l.created_at, l.updated_at,
           ia.account_label, ia.status AS account_status,
           COALESCE(sc.code, ia.provider) AS marketplace
      FROM product_channel_listing l
      JOIN integration_account ia ON ia.id = l.integration_account_id
 LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
"""

_P_SELECT = """
    SELECT id, org_id, listing_id, product_variant_id, currency,
           regular_price, sale_price, sale_start_at, sale_end_at,
           effective_from, effective_to, status, approval_request_id,
           publish_status, publish_requested_at, published_at,
           publish_error, publish_attempt_count, created_at, created_by, updated_at
      FROM product_channel_price
"""

KST = timezone(timedelta(hours=9))


def today_kst() -> date:
    """KST 기준 오늘. 서버 시간대에 의존하지 않는다.

    `effective_from` 은 DATE 라 시각 정보가 없다. UTC 서버에서 `date.today()` 를 쓰면
    한국 자정 직후 9시간 동안 하루 전 날짜가 나온다.
    """
    return datetime.now(tz=KST).date()


def _to_out(row: Any) -> ChannelListingOut:
    out = ChannelListingOut(**row._mapping)
    support = channel_connector.support_for(out.marketplace)
    out.price_publish_supported = support.price_publish_supported
    out.price_publish_unsupported_reason = (
        None if support.price_publish_supported else support.reason
    )
    return out


# ──────────────────────────────────────────────────────────────
# listing
# ──────────────────────────────────────────────────────────────

def list_listings(
    db: Session, *, org_id: UUID, product_id: UUID | None = None,
    integration_account_id: UUID | None = None, external_product_id: str | None = None,
    status: str | None = None, limit: int = 50, offset: int = 0,
) -> tuple[list[ChannelListingOut], int]:
    where = ["l.org_id = :org", "l.deleted_at IS NULL"]
    params: dict[str, Any] = {"org": str(org_id)}
    if product_id:
        where.append("l.product_id = :pid")
        params["pid"] = str(product_id)
    if integration_account_id:
        where.append("l.integration_account_id = :aid")
        params["aid"] = str(integration_account_id)
    if external_product_id:
        where.append("l.external_product_id = :ext")
        params["ext"] = external_product_id
    if status:
        where.append("l.status = :st")
        params["st"] = status

    where_sql = " AND ".join(where)
    total = int(db.execute(text(
        f"SELECT COUNT(*) FROM product_channel_listing l WHERE {where_sql}"
    ), params).scalar() or 0)
    rows = db.execute(
        text(f"{_L_SELECT} WHERE {where_sql} ORDER BY l.created_at DESC, l.id DESC "
             "LIMIT :lim OFFSET :off"),
        {**params, "lim": limit, "off": offset},
    ).all()
    return [_to_out(r) for r in rows], total


def get_listing(db: Session, *, org_id: UUID, listing_id: UUID) -> ChannelListingOut:
    row = db.execute(
        text(f"{_L_SELECT} WHERE l.id = :i AND l.org_id = :o AND l.deleted_at IS NULL"),
        {"i": str(listing_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("판매처 연결을 찾을 수 없습니다.",
                            detail={"listing_id": str(listing_id)})
    return _to_out(row)


def _assert_account_usable(db: Session, org_id: UUID, account_id: UUID) -> str | None:
    """계정이 이 조직 것이고 쓸 수 있는 상태인지. 판매처 코드를 돌려준다."""
    row = db.execute(text("""
        SELECT ia.status, COALESCE(sc.code, ia.provider) AS marketplace
          FROM integration_account ia
     LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
         WHERE ia.id = :i AND ia.org_id = :o AND ia.deleted_at IS NULL
    """), {"i": str(account_id), "o": str(org_id)}).first()
    if row is None:
        # 다른 조직 계정도 404 다 — 403 이면 "그런 계정이 있다" 는 정보가 새어 나간다.
        raise NotFoundError("판매처 계정을 찾을 수 없습니다.",
                            detail={"integration_account_id": str(account_id)})
    if row.status != "ACTIVE":
        raise ValidationError(
            "비활성 판매처 계정에는 상품을 연결할 수 없습니다. "
            "설정에서 계정을 먼저 활성화하세요.",
            detail={"account_status": row.status},
        )
    return row.marketplace


def _translate_listing_conflict(exc: IntegrityError) -> Exception:
    name = constraint_name_of(exc)
    if name == "ux_pcl_account_external":
        return ConflictError(
            "이 판매처 계정에 같은 외부 상품번호가 이미 연결돼 있습니다.",
            detail={"constraint": name},
        )
    if name == "ux_pcl_account_product":
        return ConflictError(
            "이 상품은 해당 판매처 계정에 이미 연결돼 있습니다. 기존 연결을 수정하세요.",
            detail={"constraint": name},
        )
    return exc


def create_listing(
    db: Session, *, org_id: UUID, actor: UUID, product_id: UUID, body: ChannelListingCreate
) -> ChannelListingOut:
    from app.services.product import get_product

    get_product(db, org_id=org_id, product_id=product_id)  # 조직·존재 확인
    marketplace = _assert_account_usable(db, org_id, body.integration_account_id)

    if body.status not in LISTING_STATUSES:
        raise ValidationError(f"알 수 없는 상태입니다: {body.status}",
                              detail={"allowed": list(LISTING_STATUSES)})

    if body.auto_publish_price:
        # connector 가 없는데 자동 반영을 켜면 승인할 때마다 실패 작업이 쌓인다.
        channel_connector.assert_publish_supported(marketplace)

    lid = new_id()
    try:
        db.execute(text("""
            INSERT INTO product_channel_listing
                   (id, org_id, integration_account_id, product_id,
                    external_product_id, external_product_url, status,
                    auto_publish_price, created_by, updated_by)
            VALUES (:i, :o, :a, :p, :ext, :url, :st, :auto, :actor, :actor)
        """), {"i": str(lid), "o": str(org_id), "a": str(body.integration_account_id),
               "p": str(product_id), "ext": body.external_product_id,
               "url": body.external_product_url, "st": body.status,
               "auto": body.auto_publish_price, "actor": str(actor)})
    except IntegrityError as exc:
        db.rollback()
        raise _translate_listing_conflict(exc) from exc

    record_audit(db, org_id=org_id, domain_table="product_channel_listing",
                 domain_row_id=lid, action="CREATE", actor_user_id=actor,
                 before=None, after=body.model_dump(mode="json"))
    db.commit()
    return get_listing(db, org_id=org_id, listing_id=lid)


def update_listing(
    db: Session, *, org_id: UUID, actor: UUID, listing_id: UUID, body: ChannelListingUpdate
) -> ChannelListingOut:
    fields = body.model_dump(exclude_unset=True)
    expected = fields.pop("expected_version", None)
    if not fields:
        return get_listing(db, org_id=org_id, listing_id=listing_id)

    before = get_listing(db, org_id=org_id, listing_id=listing_id)

    if "status" in fields and fields["status"] not in LISTING_STATUSES:
        raise ValidationError(f"알 수 없는 상태입니다: {fields['status']}",
                              detail={"allowed": list(LISTING_STATUSES)})
    if fields.get("auto_publish_price"):
        channel_connector.assert_publish_supported(before.marketplace)

    sets = [f"{k} = :{k}" for k in fields]
    sets.append("updated_by = :actor")
    where = ["id = :i", "org_id = :o", "deleted_at IS NULL"]
    params: dict[str, Any] = {**fields, "i": str(listing_id), "o": str(org_id),
                              "actor": str(actor)}
    if expected is not None:
        where.append("updated_at = CAST(:__ev AS TIMESTAMPTZ)")
        params["__ev"] = expected

    try:
        result = db.execute(
            text(f"UPDATE product_channel_listing SET {', '.join(sets)} "
                 f"WHERE {' AND '.join(where)}"),
            params,
        )
    except IntegrityError as exc:
        db.rollback()
        raise _translate_listing_conflict(exc) from exc

    if expected is not None and result.rowcount == 0:
        raise ConflictError(
            "다른 사용자가 이 판매처 연결을 먼저 수정했습니다. 다시 불러온 뒤 시도해 주세요.",
            detail={"expected_version": expected},
        )

    record_audit(db, org_id=org_id, domain_table="product_channel_listing",
                 domain_row_id=listing_id, action="UPDATE", actor_user_id=actor,
                 before=before.model_dump(mode="json"), after=fields)
    db.commit()
    return get_listing(db, org_id=org_id, listing_id=listing_id)


def deactivate_listing(
    db: Session, *, org_id: UUID, actor: UUID, listing_id: UUID
) -> ChannelListingOut:
    """비활성화. **삭제가 아니다** — 과거 가격과 주문이 이 리스팅을 가리킨다."""
    return update_listing(db, org_id=org_id, actor=actor, listing_id=listing_id,
                          body=ChannelListingUpdate(status="ENDED"))


# ──────────────────────────────────────────────────────────────
# 가격 조회
# ──────────────────────────────────────────────────────────────

def list_prices(
    db: Session, *, org_id: UUID, listing_id: UUID,
    variant_id: UUID | None = None, status: str | None = None,
    at: date | None = None, approval_request_id: UUID | None = None,
    include_default: bool = False, limit: int = 100, offset: int = 0,
) -> tuple[list[ChannelPriceOut], int]:
    get_listing(db, org_id=org_id, listing_id=listing_id)  # 조직 확인

    where = ["org_id = :org", "listing_id = :lid", "deleted_at IS NULL"]
    params: dict[str, Any] = {"org": str(org_id), "lid": str(listing_id)}
    if variant_id is not None:
        # include_default: variant 가격 + 상품 기본 가격을 함께 본다(fallback 표시용).
        where.append("(product_variant_id = :vid OR product_variant_id IS NULL)"
                     if include_default else "product_variant_id = :vid")
        params["vid"] = str(variant_id)
    if status:
        where.append("status = :st")
        params["st"] = status
    if at:
        where.append("effective_from <= :at AND (effective_to IS NULL OR effective_to > :at)")
        params["at"] = at
    if approval_request_id:
        where.append("approval_request_id = :arid")
        params["arid"] = str(approval_request_id)

    where_sql = " AND ".join(where)
    total = int(db.execute(
        text(f"SELECT COUNT(*) FROM product_channel_price WHERE {where_sql}"), params
    ).scalar() or 0)
    rows = db.execute(
        text(f"{_P_SELECT} WHERE {where_sql} "
             "ORDER BY effective_from DESC, created_at DESC, id DESC "
             "LIMIT :lim OFFSET :off"),
        {**params, "lim": limit, "off": offset},
    ).all()
    return [ChannelPriceOut(**r._mapping) for r in rows], total


def resolve_price(
    db: Session, *, org_id: UUID, listing_id: UUID,
    variant_id: UUID | None = None, at: date | None = None,
) -> ResolvedPrice:
    """특정 시점·variant 에 **실제로 적용되는** 가격.

    규칙 (순서가 의미를 가진다)
      ① 해당 variant 의 유효한 ACTIVE 가격
      ② 없으면 `product_variant_id IS NULL` 인 상품 기본 가격
      ③ 할인 기간 안이면 `sale_price`, 아니면 `regular_price`
      ④ 둘 다 없으면 미입력(None) — **0 이 아니다**

    이 규칙을 화면에서 다시 구현하지 않는다. 두 군데 있으면 언젠가 갈린다.
    """
    get_listing(db, org_id=org_id, listing_id=listing_id)
    at = at or today_kst()

    def _lookup(vid: UUID | None):
        scope = "product_variant_id = :vid" if vid else "product_variant_id IS NULL"
        return db.execute(text(f"""
            {_P_SELECT}
             WHERE org_id = :org AND listing_id = :lid AND deleted_at IS NULL
               AND status = 'ACTIVE' AND {scope}
               AND effective_from <= :at
               AND (effective_to IS NULL OR effective_to > :at)
             ORDER BY effective_from DESC, created_at DESC
             LIMIT 1
        """), {"org": str(org_id), "lid": str(listing_id), "at": at,
               **({"vid": str(vid)} if vid else {})}).first()

    fallback = False
    row = _lookup(variant_id) if variant_id else None
    if row is None:
        row = _lookup(None)
        fallback = variant_id is not None

    if row is None:
        return ResolvedPrice(
            listing_id=listing_id, product_variant_id=variant_id, at=at,
            resolved_price=None, regular_price=None, sale_price=None,
            sale_start_at=None, sale_end_at=None, on_sale=False,
            currency=None, source_price_id=None, variant_fallback=False,
            effective_from=None, effective_to=None,
        )

    # 할인 적용 여부. `at` 은 날짜라 그 날 00:00 KST 를 기준 시각으로 본다.
    at_kst = datetime.combine(at, datetime.min.time(), tzinfo=KST)
    on_sale = False
    if row.sale_price is not None and row.sale_start_at is not None:
        started = row.sale_start_at <= at_kst
        not_ended = row.sale_end_at is None or row.sale_end_at > at_kst
        on_sale = started and not_ended

    return ResolvedPrice(
        listing_id=listing_id,
        product_variant_id=variant_id,
        at=at,
        resolved_price=row.sale_price if on_sale else row.regular_price,
        regular_price=row.regular_price,
        sale_price=row.sale_price,
        sale_start_at=row.sale_start_at,
        sale_end_at=row.sale_end_at,
        on_sale=on_sale,
        currency=row.currency,
        source_price_id=row.id,
        variant_fallback=fallback,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
    )


# ──────────────────────────────────────────────────────────────
# 가격 버전 생성
# ──────────────────────────────────────────────────────────────

def create_price_version(
    db: Session, *, org_id: UUID, actor: UUID, listing_id: UUID,
    payload: ChannelPriceIn, approval_request_id: UUID | None = None,
    _commit: bool = True,
) -> ChannelPriceOut:
    """새 가격 버전을 만든다. **기존 행의 금액을 UPDATE 하지 않는다.**

    같은 scope(listing + variant)의 현재 유효 가격이 있으면 그 `effective_to` 를
    새 가격의 시작일로 **닫는다**. 금액을 바꾸는 게 아니라 기간을 끝내는 것이라
    "그때 얼마였나" 는 그대로 남는다.

    호출자가 이미 트랜잭션을 열고 있으면(승인 적용) `_commit=False` 로 부른다 —
    가격 생성 · 승인 상태 전이 · 감사 로그 · outbox 가 한 트랜잭션이어야 한다.
    """
    listing = get_listing(db, org_id=org_id, listing_id=listing_id)

    if payload.product_variant_id is not None:
        owned = db.execute(text("""
            SELECT 1 FROM product_variant
             WHERE id = :v AND org_id = :o AND product_id = :p AND deleted_at IS NULL
        """), {"v": str(payload.product_variant_id), "o": str(org_id),
               "p": str(listing.product_id)}).scalar()
        if not owned:
            raise ValidationError(
                "이 상품의 옵션이 아닙니다.",
                detail={"product_variant_id": str(payload.product_variant_id)},
            )

    scope_sql = ("product_variant_id = :vid" if payload.product_variant_id
                 else "product_variant_id IS NULL")
    scope_params = ({"vid": str(payload.product_variant_id)}
                    if payload.product_variant_id else {})

    # 같은 scope 의 열린 가격을 FOR UPDATE 로 잡는다 — 동시 생성이 겹치지 않게.
    current = db.execute(text(f"""
        SELECT id, effective_from, effective_to FROM product_channel_price
         WHERE org_id = :o AND listing_id = :l AND deleted_at IS NULL
           AND status = 'ACTIVE' AND {scope_sql}
           AND (effective_to IS NULL OR effective_to > :frm)
         ORDER BY effective_from
         FOR UPDATE
    """), {"o": str(org_id), "l": str(listing_id), "frm": payload.effective_from,
           **scope_params}).all()

    for row in current:
        if row.effective_from >= payload.effective_from:
            # 새 가격이 기존 가격보다 앞서거나 같은 날 시작한다 — 과거를 덮어쓰는 셈이다.
            raise ChannelPriceImmutableError(
                "이미 등록된 가격과 같거나 앞선 시작일로는 만들 수 없습니다. "
                "정정하려면 더 뒤 날짜로 새 가격을 만드세요.",
                detail={"existing_effective_from": row.effective_from.isoformat(),
                        "requested_effective_from": payload.effective_from.isoformat()},
            )
        # **기간만 닫는다. status 는 건드리지 않는다.**
        # 종료일이 생겼다고 그 가격이 무효해진 게 아니다 — 새 가격이 시작하기 전까지는
        # 여전히 지금 팔리는 가격이다. 여기서 SUPERSEDED 로 바꾸면 그 구간의
        # resolve_price 가 "가격 없음" 을 돌려준다.
        #
        # SUPERSEDED 는 "취소되어 한 번도 유효하지 않았다" 를 뜻하는 상태로 남긴다.
        db.execute(text("""
            UPDATE product_channel_price
               SET effective_to = :to, updated_by = :actor
             WHERE id = :i
        """), {"to": payload.effective_from, "actor": str(actor), "i": str(row.id)})

    pid = new_id()
    try:
        db.execute(text("""
            INSERT INTO product_channel_price
                   (id, org_id, listing_id, product_variant_id, currency,
                    regular_price, sale_price, sale_start_at, sale_end_at,
                    effective_from, effective_to, status, approval_request_id,
                    created_by, updated_by)
            VALUES (:i, :o, :l, :v, :cur, :reg, :sale, :ss, :se,
                    :frm, :to, 'ACTIVE', :arid, :actor, :actor)
        """), {"i": str(pid), "o": str(org_id), "l": str(listing_id),
               "v": str(payload.product_variant_id) if payload.product_variant_id else None,
               "cur": payload.currency, "reg": payload.regular_price,
               "sale": payload.sale_price, "ss": payload.sale_start_at,
               "se": payload.sale_end_at, "frm": payload.effective_from,
               "to": payload.effective_to,
               "arid": str(approval_request_id) if approval_request_id else None,
               "actor": str(actor)})
        db.flush()
    except IntegrityError as exc:
        name = constraint_name_of(exc)
        if name == "ex_pcp_no_overlap":
            raise ChannelPriceOverlapError(
                "같은 기간에 적용되는 가격이 이미 있습니다. "
                "적용 시작일을 조정하거나 기존 가격을 먼저 종료하세요.",
                detail={"constraint": name},
            ) from exc
        raise

    record_audit(db, org_id=org_id, domain_table="product_channel_price",
                 domain_row_id=pid, action="CREATE", actor_user_id=actor,
                 before=None, after=payload.model_dump(mode="json"))

    if _commit:
        db.commit()

    row = db.execute(text(f"{_P_SELECT} WHERE id = :i"), {"i": str(pid)}).first()
    return ChannelPriceOut(**row._mapping)
