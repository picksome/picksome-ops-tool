"""상품 / 옵션 / 변형 CRUD (D-286 옵션 정규화).

3테이블 구조:
  product              — 상품 본체 (has_variants 플래그)
  option_group         — 옵션 축 (예: "색상", "크기")
  option_value         — 축의 값 (예: "BLACK", "L")
  product_variant      — 옵션 조합 = 실 SKU (재고/원가/판가 보유)

UUID 노출 금지 (D-283): 사용자 식별자는 business_key 컬럼 사용.

variant 삭제 제약 (VariantInUseError):
- sales_order 가 참조 중이면 거부 (실거래 추적 보호)
- product_margin_expected 가 참조 중이면 거부 (계산 박제 보호)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db_errors import translate_integrity_error
from app.core.exceptions import (
    ApprovalRequiredError,
    ConflictError,
    NotFoundError,
    ProductVersionConflictError,
    VariantInUseError,
)
from app.core.ids import new_id
from app.core.money import to_krw
from app.domain.product import (
    OptionGroupCreate,
    OptionGroupOut,
    OptionGroupUpdate,
    OptionValueCreate,
    OptionValueOut,
    OptionValueUpdate,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    VariantCreate,
    VariantOut,
    VariantUpdate,
)
from app.services.exchange_rate import get_latest_rate
from app.services.search import PRODUCT_COLUMNS, build_match, tokenize


def _now() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------

_P_SELECT = """
  SELECT id, business_key, sku, name_ko, name_en, category_path, margin_type,
         status, supplier_id, supply_url, cost_amount, cost_currency,
         cost_fx_rate, cost_amount_foreign, has_variants, memo, created_at,
         updated_at
    FROM product
"""


def _next_business_key(db: Session, *, org_id: UUID) -> str:
    """org 내 다음 PROD-NNNNNN 발급 (기존 시드 데이터와 동일 포맷)."""
    row = db.execute(
        text("""
          SELECT business_key FROM product
           WHERE org_id = :o AND business_key ~ '^PROD-[0-9]{6}$'
           ORDER BY business_key DESC LIMIT 1
        """),
        {"o": str(org_id)},
    ).first()
    next_seq = int(row.business_key.split("-")[1]) + 1 if row else 0
    return f"PROD-{next_seq:06d}"


def _resolve_fx_rate(
    db: Session, cost_currency: str | None, cost_fx_rate_manual: Decimal | None
) -> Decimal | None:
    """환율 자동 채움 (M008, docs/27 D-288).

    사용자가 직접 입력했으면 그 값 그대로 사용(override). 안 입력했고 해외통화면
    exchange_rate_snapshot 의 최신 고시환율을 자동으로 채운다 — 채워진 값이 그대로
    cost_fx_rate 컬럼에 박제되므로, 나중에 환율이 바뀌어도 이 상품의 원가 계산은
    입력 시점 값 그대로 재현된다(정책 박제 원칙과 동일 논리).
    """
    if cost_fx_rate_manual is not None:
        return cost_fx_rate_manual
    if cost_currency and cost_currency != "KRW":
        return get_latest_rate(db, currency=cost_currency)
    return None


def _resolve_cost_amount(
    cost_currency: str | None,
    cost_fx_rate: Decimal | None,
    cost_amount_foreign: int | None,
    cost_amount_manual: int | None,
) -> int | None:
    """cost_currency 가 KRW 가 아니고 현지가격+환율이 둘 다 있으면 자동 환산.

    그 외(국내 소싱, 정보 일부 누락)엔 수동 입력값(cost_amount_manual) 그대로 사용.
    """
    if cost_currency and cost_currency != "KRW" and cost_fx_rate is not None and cost_amount_foreign is not None:
        return to_krw(Decimal(cost_amount_foreign) * cost_fx_rate)
    return cost_amount_manual


def list_products(
    db: Session,
    *,
    org_id: UUID,
    status: str | None = None,
    margin_type: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ProductOut], int]:
    where = ["org_id = :o", "deleted_at IS NULL"]
    params: dict[str, Any] = {"o": str(org_id)}
    if status:
        where.append("status = :status")
        params["status"] = status
    if margin_type:
        where.append("margin_type = :margin_type")
        params["margin_type"] = margin_type
    if q:
        # 공통 검색 규칙(services/search.py)을 쓴다 — 목록 필터와 ⌘K 팔레트의
        # 매칭이 갈리면 "목록엔 나오는데 검색엔 안 나온다" 가 된다.
        # 단일 `%q%` 였을 때는 `쿨링 스카프` 처럼 띄어쓰기가 섞이면 통째로 실패했다
        # (실제 상품명 `국산 쿨링 스카프 쿨스카프 얼음스카프`).
        match_sql, match_params = build_match(tokenize(q), PRODUCT_COLUMNS)
        where.append(f"({match_sql})")
        params.update(match_params)
    where_sql = " AND ".join(where)

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM product WHERE {where_sql}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0

    # ⚠️ `id` 타이브레이커가 반드시 있어야 한다. 엑셀 import 로 들어온 1,065건은
    # created_at 이 전부 같아서(한 트랜잭션) created_at 만으로 정렬하면 순서가
    # 비결정적이고, 페이지마다 같은 행이 중복으로 나오거나 아예 빠진다.
    rows = db.execute(
        text(f"""
          {_P_SELECT}
           WHERE {where_sql}
           ORDER BY created_at DESC, id DESC
           LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    ).all()
    return [ProductOut(**r._mapping) for r in rows], total


def get_product(db: Session, *, org_id: UUID, product_id: UUID) -> ProductOut:
    row = db.execute(
        text(f"{_P_SELECT} WHERE id = :i AND org_id = :o AND deleted_at IS NULL"),
        {"i": str(product_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("상품을 찾을 수 없습니다.")
    return ProductOut(**row._mapping)


def create_product(
    db: Session, *, org_id: UUID, actor: UUID, body: ProductCreate
) -> ProductOut:
    new_pid = new_id()
    business_key = body.business_key or _next_business_key(db, org_id=org_id)
    resolved_fx_rate = _resolve_fx_rate(db, body.cost_currency, body.cost_fx_rate)
    cost_amount = _resolve_cost_amount(
        body.cost_currency, resolved_fx_rate, body.cost_amount_foreign, body.cost_amount
    )
    db.execute(
        text("""
          INSERT INTO product (
            id, org_id, business_key, sku, name_ko, name_en, category_path,
            margin_type, status, supplier_id, supply_url,
            cost_amount, cost_currency, cost_fx_rate, cost_amount_foreign, has_variants, memo,
            created_by, updated_by
          ) VALUES (
            :id, :o, :bk, :sku, :nk, :ne, :cp,
            CAST(:mt AS enum_margin_type),
            CAST(:st AS enum_product_status),
            :si, :su, :ca, :cc, :fx, :caf, :hv, :memo,
            :actor, :actor
          )
        """),
        {
            "id": str(new_pid),
            "o": str(org_id),
            "bk": business_key,
            "sku": body.sku,
            "nk": body.name_ko,
            "ne": body.name_en,
            "cp": body.category_path,
            "mt": body.margin_type,
            "st": body.status,
            "si": str(body.supplier_id) if body.supplier_id else None,
            "su": body.supply_url,
            "ca": cost_amount,
            "cc": body.cost_currency,
            "fx": resolved_fx_rate,
            "caf": body.cost_amount_foreign,
            "hv": body.has_variants,
            "memo": body.memo,
            "actor": str(actor),
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product",
        domain_row_id=new_pid,
        action="CREATE",
        actor_user_id=actor,
        after={"business_key": business_key, "name_ko": body.name_ko},
    )
    db.commit()
    return get_product(db, org_id=org_id, product_id=new_pid)


def _version_conflict(
    expected: str, current: str | None, product_id: UUID
) -> ProductVersionConflictError:
    """409 를 한 곳에서 만든다 — 사전 검사와 WHERE 검사가 같은 응답을 내야 한다."""
    return ProductVersionConflictError(
        "다른 사용자가 이 상품을 먼저 수정했습니다. "
        "최신 정보를 불러온 뒤 다시 변경해 주세요.",
        detail={
            "expected_version": expected,
            "current_version": current,
            "product_id": str(product_id),
        },
    )


def update_product(
    db: Session, *, org_id: UUID, actor: UUID, product_id: UUID, body: ProductUpdate,
    _internal_approval_apply: bool = False,
) -> ProductOut:
    """상품 직접 수정.

    ⚠️ **승인 대상 필드는 이 경로로 바꿀 수 없다.**
    승인 워크플로를 만들어 놓고 일반 수정 API 를 열어두면 우회가 된다.
    승인이 필요한 필드는 `approval_request` 를 통해서만 반영되고,
    실제 적용은 `services/approval_targets.ProductAdapter.apply_changes` 가 수행한다.

    `_internal_approval_apply` 는 승인 적용 경로 전용 플래그다. **API 입력이 아니다** —
    라우터가 이 인자를 노출하지 않으므로 클라이언트가 설정할 수 없다.
    """
    fields = body.model_dump(exclude_unset=True)
    expected_version = fields.pop("expected_version", None)
    if not fields:
        return get_product(db, org_id=org_id, product_id=product_id)

    if not _internal_approval_apply:
        from app.services.approval_targets import all_approval_fields

        blocked = sorted(set(fields) & all_approval_fields("product"))
        if blocked:
            raise ApprovalRequiredError(
                f"승인이 필요한 필드는 직접 수정할 수 없습니다: {blocked}. "
                "승인 요청을 만들어 주세요.",
                detail={
                    "approval_required_fields": blocked,
                    "target_type": "product",
                    "target_id": str(product_id),
                },
            )

    before = get_product(db, org_id=org_id, product_id=product_id)

    # 낙관적 동시성 — 여기서는 **빠른 실패**만 한다. 실제 방어는 아래 UPDATE 의
    # WHERE 절이다. 이 시점의 비교만으로는 막을 수 없다: 확인한 뒤 UPDATE 를 실행하기
    # 전에 다른 커넥션이 커밋하면, READ COMMITTED 에서 여기는 옛 값을 봤으므로
    # 통과하고 UPDATE 가 남의 수정을 덮어쓴다(TOCTOU).
    # 이 사전 검사를 남겨두는 이유는 오류를 한 번의 왕복으로 알려주기 위해서다.
    if expected_version is not None and before.version_token != expected_version:
        raise _version_conflict(expected_version, before.version_token, product_id)

    if any(k in fields for k in ("cost_currency", "cost_fx_rate", "cost_amount_foreign")):
        effective_currency = fields.get("cost_currency", before.cost_currency)
        if "cost_fx_rate" in fields:
            effective_fx_rate = fields["cost_fx_rate"]  # 사용자가 명시적으로 지정(override)
        else:
            # 사용자가 안 건드렸으면 기존 값 우선, 없으면(최초 해외통화 지정 등) 자동 채움
            effective_fx_rate = _resolve_fx_rate(db, effective_currency, before.cost_fx_rate)
            if effective_fx_rate != before.cost_fx_rate:
                fields["cost_fx_rate"] = effective_fx_rate
        computed = _resolve_cost_amount(
            effective_currency,
            effective_fx_rate,
            fields.get("cost_amount_foreign", before.cost_amount_foreign),
            fields.get("cost_amount", before.cost_amount),
        )
        if computed is not None:
            fields["cost_amount"] = computed

    sets: list[str] = []
    params: dict[str, Any] = {"i": str(product_id), "o": str(org_id), "now": _now(), "actor": str(actor)}
    for k, v in fields.items():
        if k == "supplier_id" and v is not None:
            v = str(v)
        if k == "margin_type":
            sets.append(f"{k} = CAST(:{k} AS enum_margin_type)")
        elif k == "status":
            sets.append(f"{k} = CAST(:{k} AS enum_product_status)")
        else:
            sets.append(f"{k} = :{k}")
        params[k] = v
    # `updated_at` 은 트리거(`set_updated_at`)가 clock_timestamp() 로 채운다.
    # 여기서 값을 넣어도 BEFORE UPDATE 트리거가 덮어쓰므로 SET 에 넣지 않는다 —
    # 넣으면 "애플리케이션이 정한다" 는 잘못된 인상을 준다.
    sets.append("updated_by = :actor")

    where = ["id = :i", "org_id = :o", "deleted_at IS NULL"]
    if expected_version is not None:
        # **여기가 진짜 방어선이다.** 읽은 이후 행이 바뀌었으면 영향 행이 0 이 된다.
        # 검사와 쓰기가 한 문장이라 그 사이에 끼어들 틈이 없다.
        where.append("updated_at = CAST(:__expected_version AS TIMESTAMPTZ)")
        params["__expected_version"] = expected_version

    result = db.execute(
        text(f"UPDATE product SET {', '.join(sets)} WHERE {' AND '.join(where)}"),
        params,
    )

    if expected_version is not None and result.rowcount == 0:
        # 행이 안 맞았다. 두 가지 경우가 있고 사용자에게 할 말이 다르다.
        #   · 상품이 사라졌다(삭제)        → 404. "다시 불러오세요" 는 도움이 안 된다
        #   · 남이 먼저 저장했다           → 409. 최신값을 주고 다시 판단하게 한다
        current = db.execute(
            text("SELECT updated_at FROM product "
                 "WHERE id = :i AND org_id = :o AND deleted_at IS NULL"),
            {"i": str(product_id), "o": str(org_id)},
        ).scalar()
        if current is None:
            raise NotFoundError(
                "상품을 찾을 수 없습니다. 삭제되었을 수 있습니다.",
                detail={"product_id": str(product_id)},
            )
        raise _version_conflict(expected_version, current.isoformat(), product_id)
    # 승인 적용 경로에서는 감사 로그를 여기서 남기지 않는다.
    # services/approval.apply() 가 승인 요청 번호와 함께 before/after 를 이미 기록한다 —
    # 여기서 또 쓰면 같은 변경이 두 줄로 남아 "몇 번 바뀌었나" 를 셀 수 없게 된다.
    if not _internal_approval_apply:
        record_audit(
            db,
            org_id=org_id,
            domain_table="product",
            domain_row_id=product_id,
            action="UPDATE" if "status" not in fields else "STATUS_CHANGE",
            actor_user_id=actor,
            before=before.model_dump(mode="json"),
            after=fields,
        )

    # ⚠️ 승인 적용 경로에서는 **커밋하지 않는다.**
    # approval.apply() 가 "상품 변경 + 승인 상태 전이 + 감사 로그" 를 한 트랜잭션으로
    # 묶는다. 여기서 커밋해 버리면 뒤이어 감사 기록이 실패해도 상품 변경만 남는다
    # (실제로 감사 실패 롤백 테스트가 이 회귀를 잡았다).
    # 커밋 책임은 호출자(approval.apply)에게 있다.
    if not _internal_approval_apply:
        db.commit()
    return get_product(db, org_id=org_id, product_id=product_id)


def delete_product(db: Session, *, org_id: UUID, actor: UUID, product_id: UUID) -> None:
    # variant 가 사용 중인지 체크 (sales_order)
    used = db.execute(
        text("""
          SELECT 1 FROM sales_order
           WHERE product_id = :p AND deleted_at IS NULL LIMIT 1
        """),
        {"p": str(product_id)},
    ).first()
    if used:
        raise ConflictError("이 상품은 판매 주문에서 사용 중입니다.")

    db.execute(
        text("""
          UPDATE product SET deleted_at = :now, deleted_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(product_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product",
        domain_row_id=product_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()


# ---------------------------------------------------------------------------
# Option group
# ---------------------------------------------------------------------------


def list_option_groups(db: Session, *, org_id: UUID, product_id: UUID) -> list[OptionGroupOut]:
    # 상품이 이 조직 것인지 먼저 확인한다. 없으면 404.
    #
    # 확인 없이 빈 목록을 주면 "옵션이 없는 내 상품" 과 "남의 상품" 이 구분되지 않는다.
    # 화면은 그걸 "옵션을 추가할 수 있는 상품" 으로 읽고 추가를 시도했다가
    # 엉뚱한 오류를 만난다. 상세 조회(`GET /products/{id}`)와 동작이 같아야 한다.
    get_product(db, org_id=org_id, product_id=product_id)
    rows = db.execute(
        text("""
          SELECT id, product_id, name, sort_order
            FROM product_option_group
           WHERE org_id = :o AND product_id = :p AND deleted_at IS NULL
           ORDER BY sort_order, name
        """),
        {"o": str(org_id), "p": str(product_id)},
    ).all()
    return [OptionGroupOut(**r._mapping) for r in rows]


def create_option_group(
    db: Session, *, org_id: UUID, actor: UUID, product_id: UUID, body: OptionGroupCreate
) -> OptionGroupOut:
    new_gid = new_id()
    db.execute(
        text("""
          INSERT INTO product_option_group (
            id, org_id, product_id, name, sort_order, created_by, updated_by
          ) VALUES (:id, :o, :p, :n, :s, :a, :a)
        """),
        {
            "id": str(new_gid),
            "o": str(org_id),
            "p": str(product_id),
            "n": body.name,
            "s": body.sort_order,
            "a": str(actor),
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_option_group",
        domain_row_id=new_gid,
        action="CREATE",
        actor_user_id=actor,
        after={"product_id": str(product_id), "name": body.name},
    )
    db.commit()
    row = db.execute(
        text("""
          SELECT id, product_id, name, sort_order
            FROM product_option_group WHERE id = :i
        """),
        {"i": str(new_gid)},
    ).first()
    return OptionGroupOut(**row._mapping)


def update_option_group(
    db: Session, *, org_id: UUID, actor: UUID, group_id: UUID, body: OptionGroupUpdate
) -> OptionGroupOut:
    fields = body.model_dump(exclude_unset=True)
    if fields:
        sets = ", ".join([f"{k} = :{k}" for k in fields])
        db.execute(
            text(f"""
              UPDATE product_option_group SET {sets}, updated_at = :now, updated_by = :actor
               WHERE id = :i AND org_id = :o AND deleted_at IS NULL
            """),
            {**fields, "i": str(group_id), "o": str(org_id), "now": _now(), "actor": str(actor)},
        )
        record_audit(
            db,
            org_id=org_id,
            domain_table="product_option_group",
            domain_row_id=group_id,
            action="UPDATE",
            actor_user_id=actor,
            after=fields,
        )
        db.commit()
    row = db.execute(
        text("""
          SELECT id, product_id, name, sort_order
            FROM product_option_group WHERE id = :i AND deleted_at IS NULL
        """),
        {"i": str(group_id)},
    ).first()
    if row is None:
        raise NotFoundError("옵션 그룹을 찾을 수 없습니다.")
    return OptionGroupOut(**row._mapping)


def delete_option_group(db: Session, *, org_id: UUID, actor: UUID, group_id: UUID) -> None:
    db.execute(
        text("""
          UPDATE product_option_group SET deleted_at = :now, deleted_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(group_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_option_group",
        domain_row_id=group_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()


# ---------------------------------------------------------------------------
# Option value
# ---------------------------------------------------------------------------


def list_option_values(db: Session, *, org_id: UUID, group_id: UUID) -> list[OptionValueOut]:
    rows = db.execute(
        text("""
          SELECT id, option_group_id, value, sort_order
            FROM product_option_value
           WHERE org_id = :o AND option_group_id = :g AND deleted_at IS NULL
           ORDER BY sort_order, value
        """),
        {"o": str(org_id), "g": str(group_id)},
    ).all()
    return [OptionValueOut(**r._mapping) for r in rows]


def create_option_value(
    db: Session, *, org_id: UUID, actor: UUID, group_id: UUID, body: OptionValueCreate
) -> OptionValueOut:
    new_vid = new_id()
    db.execute(
        text("""
          INSERT INTO product_option_value (
            id, org_id, option_group_id, value, sort_order, created_by, updated_by
          ) VALUES (:id, :o, :g, :v, :s, :a, :a)
        """),
        {
            "id": str(new_vid),
            "o": str(org_id),
            "g": str(group_id),
            "v": body.value,
            "s": body.sort_order,
            "a": str(actor),
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_option_value",
        domain_row_id=new_vid,
        action="CREATE",
        actor_user_id=actor,
        after={"option_group_id": str(group_id), "value": body.value},
    )
    db.commit()
    row = db.execute(
        text("""
          SELECT id, option_group_id, value, sort_order
            FROM product_option_value WHERE id = :i
        """),
        {"i": str(new_vid)},
    ).first()
    return OptionValueOut(**row._mapping)


def update_option_value(
    db: Session, *, org_id: UUID, actor: UUID, value_id: UUID, body: OptionValueUpdate
) -> OptionValueOut:
    fields = body.model_dump(exclude_unset=True)
    if fields:
        sets = ", ".join([f"{k} = :{k}" for k in fields])
        db.execute(
            text(f"""
              UPDATE product_option_value SET {sets}, updated_at = :now, updated_by = :actor
               WHERE id = :i AND org_id = :o AND deleted_at IS NULL
            """),
            {**fields, "i": str(value_id), "o": str(org_id), "now": _now(), "actor": str(actor)},
        )
        record_audit(
            db,
            org_id=org_id,
            domain_table="product_option_value",
            domain_row_id=value_id,
            action="UPDATE",
            actor_user_id=actor,
            after=fields,
        )
        db.commit()
    row = db.execute(
        text("""
          SELECT id, option_group_id, value, sort_order
            FROM product_option_value WHERE id = :i AND deleted_at IS NULL
        """),
        {"i": str(value_id)},
    ).first()
    if row is None:
        raise NotFoundError("옵션 값을 찾을 수 없습니다.")
    return OptionValueOut(**row._mapping)


def delete_option_value(db: Session, *, org_id: UUID, actor: UUID, value_id: UUID) -> None:
    db.execute(
        text("""
          UPDATE product_option_value SET deleted_at = :now, deleted_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(value_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_option_value",
        domain_row_id=value_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()


# ---------------------------------------------------------------------------
# Variant
# ---------------------------------------------------------------------------

_V_SELECT = """
  SELECT id, product_id, variant_key, option_values_json, sku_external,
         cost_amount, sale_price, extra_price, stock_quantity, is_active
    FROM product_variant
"""


def list_variants(db: Session, *, org_id: UUID, product_id: UUID) -> list[VariantOut]:
    # 옵션 그룹 조회와 같은 이유로 상품 소유를 먼저 확인한다.
    get_product(db, org_id=org_id, product_id=product_id)
    rows = db.execute(
        text(f"""
          {_V_SELECT}
           WHERE org_id = :o AND product_id = :p AND deleted_at IS NULL
           ORDER BY variant_key
        """),
        {"o": str(org_id), "p": str(product_id)},
    ).all()
    return [VariantOut(**r._mapping) for r in rows]


def get_variant(db: Session, *, org_id: UUID, variant_id: UUID) -> VariantOut:
    row = db.execute(
        text(f"{_V_SELECT} WHERE id = :i AND org_id = :o AND deleted_at IS NULL"),
        {"i": str(variant_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("변형을 찾을 수 없습니다.")
    return VariantOut(**row._mapping)


def create_variant(
    db: Session, *, org_id: UUID, actor: UUID, product_id: UUID, body: VariantCreate
) -> VariantOut:
    """옵션 조합 생성.

    같은 상품에 같은 `variant_key` 가 두 번 들어오면 `uq_variant_product_key` 가 막는다.
    그걸 번역하지 않으면 **500 이 나간다** — 사용자는 "서버 오류" 를 보고 다시 시도하는데
    몇 번을 해도 같은 결과다. 무엇이 문제인지 알려줘야 한다.
    """
    new_vid = new_id()
    try:
        db.execute(
            text("""
              INSERT INTO product_variant (
                id, org_id, product_id, variant_key, option_values_json,
                sku_external, cost_amount, sale_price, extra_price,
                stock_quantity, is_active, created_by, updated_by
              ) VALUES (
                :id, :o, :p, :vk, CAST(:ov AS JSONB),
                :sx, :ca, :sp, :ep,
                :sq, :ia, :a, :a
              )
            """),
            {
                "id": str(new_vid),
                "o": str(org_id),
                "p": str(product_id),
                "vk": body.variant_key,
                "ov": json.dumps(body.option_values_json, ensure_ascii=False),
                "sx": body.sku_external,
                "ca": body.cost_amount,
                "sp": body.sale_price,
                "ep": body.extra_price,
                "sq": body.stock_quantity,
                "ia": body.is_active,
                "a": str(actor),
            },
        )
    except IntegrityError as exc:
        domain = translate_integrity_error(exc, {
            "uq_variant_product_key": lambda: ConflictError(
                f"같은 옵션 조합이 이미 있습니다: {body.variant_key}",
                detail={"variant_key": body.variant_key},
            ),
        })
        if domain is not None:
            raise domain from exc
        raise

    record_audit(
        db,
        org_id=org_id,
        domain_table="product_variant",
        domain_row_id=new_vid,
        action="CREATE",
        actor_user_id=actor,
        after={"product_id": str(product_id), "variant_key": body.variant_key},
    )
    db.commit()
    return get_variant(db, org_id=org_id, variant_id=new_vid)


def update_variant(
    db: Session, *, org_id: UUID, actor: UUID, variant_id: UUID, body: VariantUpdate
) -> VariantOut:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return get_variant(db, org_id=org_id, variant_id=variant_id)

    sets: list[str] = []
    params: dict[str, Any] = {"i": str(variant_id), "o": str(org_id), "now": _now(), "actor": str(actor)}
    for k, v in fields.items():
        if k == "option_values_json":
            sets.append(f"{k} = CAST(:{k} AS JSONB)")
            params[k] = json.dumps(v, ensure_ascii=False)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.extend(["updated_at = :now", "updated_by = :actor"])

    db.execute(
        text(f"""
          UPDATE product_variant SET {', '.join(sets)}
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        params,
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_variant",
        domain_row_id=variant_id,
        action="UPDATE",
        actor_user_id=actor,
        after=fields,
    )
    db.commit()
    return get_variant(db, org_id=org_id, variant_id=variant_id)


def delete_variant(db: Session, *, org_id: UUID, actor: UUID, variant_id: UUID) -> None:
    # VARIANT_IN_USE — sales_order 또는 product_margin_expected 참조 확인
    used = db.execute(
        text("""
          SELECT 'sales_order' AS table_name FROM sales_order
           WHERE product_variant_id = :v AND deleted_at IS NULL LIMIT 1
          UNION ALL
          SELECT 'product_margin_expected' FROM product_margin_expected
           WHERE product_variant_id = :v AND deleted_at IS NULL LIMIT 1
        """),
        {"v": str(variant_id)},
    ).first()
    if used:
        raise VariantInUseError(
            "변형이 사용 중입니다. 비활성(is_active=FALSE)으로 전환하세요.",
            detail={"in_use_table": used.table_name},
        )

    db.execute(
        text("""
          UPDATE product_variant SET deleted_at = :now, deleted_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(variant_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="product_variant",
        domain_row_id=variant_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()
