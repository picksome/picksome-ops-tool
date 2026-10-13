"""Sourcing Core — 공급처 · 소싱처 · 원가 version · 환율 (M037).

세 가지 원가를 구분한다
--------------------
```
SOURCE COST      지금 다시 사면 얼마인가        ← 이 모듈이 다루는 것
INVENTORY COST   보유 재고의 취득원가            (미구현)
ORDER COST       실제 판매에 귀속된 원가         (product_margin_actual)
```

셋을 하나로 합치면 되돌릴 수 없다. 이 모듈은 **소싱 의사결정용**이다 —
"이 상품을 지금 들여오면 남는가" 에 답한다. 과거 판매의 마진을 다시 계산하지 않는다.

과거를 건드리지 않는다
------------------
새 원가 version 을 만들어도 이미 팔린 건의 실마진은 그대로다. 그건
`product_margin_actual` 에 그 시점 값으로 박제돼 있다. 여기서 바꾸면
"작년 순이익" 이 오늘 달라진다.

환율은 시점마다 다르다
------------------
소싱 검토 시점과 실제 결제 시점의 환율이 다르다. 하나의 "현재 환율" 로 뭉개면
어느 시점 기준인지 말할 수 없다. **없는 환율을 임의값으로 채우지 않는다** —
없으면 계산 불가로 표시한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db_errors import translate_integrity_error
from app.core.exceptions import ConflictError, NotFoundError, ValidationError

# 취득 부대비용 항목. `해외 직구 마진계산기` 시트에서 확인한 실제 항목들이다.
COST_COMPONENTS = (
    "domestic_shipping",
    "international_shipping",
    "customs_duty",
    "import_vat",
    "packaging_cost",
    "other_cost",
)

FX_RATE_TYPES = ("SOURCING", "PRICE_SETTING", "PURCHASE", "PAYMENT", "ACCOUNTING")


# ──────────────────────────────────────────────────────────────────────────
# 공급처
# ──────────────────────────────────────────────────────────────────────────

def next_supplier_code(db: Session, *, org_id: UUID) -> str:
    """`SUP-000001` 형태. **Excel 행 번호를 쓰지 않는다** — 행이 밀리면 어긋난다."""
    n = db.execute(text("""
        SELECT COALESCE(MAX(CAST(NULLIF(regexp_replace(supplier_code, '\\D', '', 'g'), '') AS INTEGER)), 0)
          FROM supplier_site WHERE org_id = :org
    """), {"org": str(org_id)}).scalar() or 0
    return f"SUP-{n + 1:06d}"


def domain_of(url: str | None) -> str | None:
    """URL 에서 도메인만. 실제 Excel 의 "구매처" 가 URL 이라 필요하다.

    `www.` 는 떼고 소문자로. `domeggook.com` 과 `www.domeggook.com` 이 다른
    공급처로 잡히면 같은 사이트가 둘로 갈라진다.
    """
    if not url:
        return None
    from urllib.parse import urlparse

    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


def assert_safe_url(url: str | None) -> None:
    """`http`/`https` 만 허용한다.

    `javascript:` 같은 scheme 을 저장하면 화면이 그것을 링크로 렌더할 때
    클릭 한 번에 실행된다. 프론트에서만 막으면 API 를 직접 부르는 순간 뚫린다.
    """
    if not url or not url.strip():
        return
    from urllib.parse import urlparse

    scheme = (urlparse(url.strip()).scheme or "").lower()
    if scheme and scheme not in ("http", "https"):
        raise ValidationError(
            f"허용되지 않는 링크 형식입니다: {scheme}. http 또는 https 만 사용하세요.",
            detail={"scheme": scheme},
        )


def create_supplier(
    db: Session, *, org_id: UUID, actor_user_id: UUID, name: str,
    supplier_type: str = "WHOLESALE", country_code: str | None = None,
    website_url: str | None = None, default_currency: str = "KRW",
    rating: int | None = None, memo: str | None = None,
    external_partner_id: UUID | None = None,
    credential_present: bool = False,
    supplier_code: str | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise ValidationError("공급처 이름이 비어 있습니다.")
    if rating is not None and not 1 <= rating <= 5:
        raise ValidationError("평점은 1~5 입니다.")

    assert_safe_url(website_url)
    code = supplier_code or next_supplier_code(db, org_id=org_id)
    try:
        row = db.execute(text("""
            INSERT INTO supplier_site
                   (org_id, supplier_code, name, supplier_type, country_code,
                    website_url, website_domain, default_currency, rating, memo,
                    external_partner_id, credential_present, created_by, updated_by)
            VALUES (:org, :code, :name, :type, :cc, :url, :domain, :cur, :rating,
                    :memo, :partner, :cred, :actor, :actor)
            RETURNING id
        """), {
            "org": str(org_id), "code": code, "name": name.strip(),
            "type": supplier_type, "cc": country_code, "url": website_url,
            "domain": domain_of(website_url), "cur": default_currency,
            "rating": rating, "memo": memo,
            "partner": str(external_partner_id) if external_partner_id else None,
            "cred": credential_present, "actor": str(actor_user_id),
        }).mappings().one()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        domain = translate_integrity_error(exc, {
            "uq_supplier_site_code": lambda: ConflictError(
                f"이미 있는 공급처 코드입니다: {code}",
            ),
        })
        if domain is not None:
            raise domain from exc
        raise

    sid = UUID(str(row["id"]))
    record_audit(
        db, org_id=org_id, domain_table="supplier_site", domain_row_id=sid,
        action="CREATE", actor_user_id=actor_user_id,
        after={"supplier_code": code, "name": name, "type": supplier_type},
    )
    db.commit()
    return get_supplier(db, org_id=org_id, supplier_id=sid)


def get_supplier(db: Session, *, org_id: UUID, supplier_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT s.id, s.supplier_code, s.name, s.supplier_type, s.country_code,
               s.website_url, s.website_domain, s.default_currency, s.status,
               s.rating, s.memo, s.supports_api, s.integration_status,
               s.external_partner_id, s.credential_present, s.created_at,
               (SELECT COUNT(*) FROM product_source ps
                 WHERE ps.supplier_site_id = s.id AND ps.deleted_at IS NULL)
                 AS linked_product_count
          FROM supplier_site s
         WHERE s.id = :i AND s.org_id = :org AND s.deleted_at IS NULL
    """), {"i": str(supplier_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("공급처를 찾을 수 없습니다.")
    out = dict(row)
    out["id"] = str(out["id"])
    out["external_partner_id"] = (
        str(out["external_partner_id"]) if out["external_partner_id"] else None
    )
    # **비밀번호는 어떤 경로로도 나가지 않는다.** 여기에 그 컬럼 자체가 없다.
    return out


def list_suppliers(
    db: Session, *, org_id: UUID, q: str | None = None, status: str | None = None,
    limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    where = ["s.org_id = :org", "s.deleted_at IS NULL"]
    params: dict[str, Any] = {"org": str(org_id), "lim": limit, "off": offset}
    if q:
        where.append("(s.name ILIKE :q OR s.supplier_code ILIKE :q"
                     " OR s.website_domain ILIKE :q)")
        params["q"] = f"%{q}%"
    if status:
        where.append("s.status = :st")
        params["st"] = status
    clause = " AND ".join(where)

    total = db.execute(
        text(f"SELECT COUNT(*) FROM supplier_site s WHERE {clause}"), params
    ).scalar_one()
    rows = db.execute(text(f"""
        SELECT s.id, s.supplier_code, s.name, s.supplier_type, s.country_code,
               s.website_url, s.website_domain, s.default_currency, s.status,
               s.rating, s.supports_api, s.integration_status, s.credential_present,
               (SELECT COUNT(*) FROM product_source ps
                 WHERE ps.supplier_site_id = s.id AND ps.deleted_at IS NULL)
                 AS linked_product_count,
               (SELECT MAX(ps.last_checked_at) FROM product_source ps
                 WHERE ps.supplier_site_id = s.id AND ps.deleted_at IS NULL)
                 AS last_used_at
          FROM supplier_site s
         WHERE {clause}
         ORDER BY s.name
         LIMIT :lim OFFSET :off
    """), params).mappings().all()

    items = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        items.append(d)
    return {"items": items, "total": total}


_SUPPLIER_PATCHABLE = (
    "name", "supplier_type", "country_code", "website_url",
    "default_currency", "status", "rating", "memo", "credential_present",
)


def update_supplier(
    db: Session, *, org_id: UUID, actor_user_id: UUID, supplier_id: UUID, **changes: Any,
) -> dict[str, Any]:
    before = get_supplier(db, org_id=org_id, supplier_id=supplier_id)
    unknown = sorted(set(changes) - set(_SUPPLIER_PATCHABLE))
    if unknown:
        raise ValidationError(f"수정할 수 없는 항목입니다: {unknown}")
    if not changes:
        return before

    sets = [f"{k} = :{k}" for k in changes]
    params: dict[str, Any] = {**changes, "i": str(supplier_id), "org": str(org_id),
                              "actor": str(actor_user_id)}
    if "website_url" in changes:
        # 도메인은 URL 에서 다시 뽑는다 — 손으로 넣게 두면 둘이 어긋난다.
        sets.append("website_domain = :domain")
        params["domain"] = domain_of(changes["website_url"])

    db.execute(text(f"""
        UPDATE supplier_site SET {", ".join(sets)},
               updated_at = now(), updated_by = :actor
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), params)

    record_audit(
        db, org_id=org_id, domain_table="supplier_site", domain_row_id=supplier_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={k: before.get(k) for k in changes},
        after={k: str(v) for k, v in changes.items()},
    )
    db.commit()
    return get_supplier(db, org_id=org_id, supplier_id=supplier_id)


def archive_supplier(
    db: Session, *, org_id: UUID, actor_user_id: UUID, supplier_id: UUID, reason: str,
) -> None:
    """행을 지우지 않는다 — 과거 소싱 기록이 어느 공급처였는지 남아야 한다."""
    if not reason.strip():
        raise ValidationError("공급처 정리에는 사유가 필요합니다.")
    res = db.execute(text("""
        UPDATE supplier_site SET deleted_at = now(), deleted_by = :actor
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(supplier_id), "org": str(org_id), "actor": str(actor_user_id)})
    if res.rowcount == 0:
        raise NotFoundError("공급처를 찾을 수 없습니다.")
    record_audit(
        db, org_id=org_id, domain_table="supplier_site", domain_row_id=supplier_id,
        action="DELETE", actor_user_id=actor_user_id, reason=reason,
    )
    db.commit()


# ──────────────────────────────────────────────────────────────────────────
# 상품 소싱처
# ──────────────────────────────────────────────────────────────────────────

def add_product_source(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    product_id: UUID, supplier_site_id: UUID,
    source_url: str | None = None, external_source_product_id: str | None = None,
    currency: str = "KRW", moq: int | None = None, lead_time_days: int | None = None,
    weight_g: int | None = None, is_primary: bool = False, memo: str | None = None,
) -> dict[str, Any]:
    """상품에 소싱처를 하나 붙인다.

    첫 소싱처는 자동으로 primary 가 된다 — 하나뿐인데 주 소싱처가 없으면
    예상 원가를 어디서 가져올지 정할 수 없다.
    """
    _assert_product(db, org_id, product_id)
    _assert_supplier(db, org_id, supplier_site_id)
    assert_safe_url(source_url)

    existing = db.execute(text("""
        SELECT COUNT(*) FROM product_source
         WHERE product_id = :p AND deleted_at IS NULL
    """), {"p": str(product_id)}).scalar() or 0
    primary = is_primary or existing == 0

    if primary:
        _clear_primary(db, product_id=product_id, actor_user_id=actor_user_id)

    try:
        row = db.execute(text("""
            INSERT INTO product_source
                   (org_id, product_id, supplier_site_id, source_url,
                    external_source_product_id, currency, moq, lead_time_days,
                    weight_g, is_primary, memo, created_by, updated_by)
            VALUES (:org, :p, :s, :url, :ext, :cur, :moq, :lead, :w, :prim, :memo,
                    :actor, :actor)
            RETURNING id
        """), {
            "org": str(org_id), "p": str(product_id), "s": str(supplier_site_id),
            "url": source_url, "ext": external_source_product_id, "cur": currency,
            "moq": moq, "lead": lead_time_days, "w": weight_g, "prim": primary,
            "memo": memo, "actor": str(actor_user_id),
        }).mappings().one()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        domain = translate_integrity_error(exc, {
            "uq_product_source_pair": lambda: ConflictError(
                "이 상품은 이미 그 공급처에 연결돼 있습니다."
            ),
            "uq_product_single_primary_source": lambda: ConflictError(
                "이 상품에는 이미 주 소싱처가 있습니다."
            ),
        })
        if domain is not None:
            raise domain from exc
        raise

    sid = UUID(str(row["id"]))
    record_audit(
        db, org_id=org_id, domain_table="product_source", domain_row_id=sid,
        action="CREATE", actor_user_id=actor_user_id,
        after={"product_id": str(product_id), "supplier_site_id": str(supplier_site_id),
               "is_primary": primary},
    )
    db.commit()
    return get_product_source(db, org_id=org_id, source_id=sid)


def set_primary_source(
    db: Session, *, org_id: UUID, actor_user_id: UUID, source_id: UUID,
) -> dict[str, Any]:
    """주 소싱처 교체.

    **먼저 내리고 올린다.** 순서를 뒤집으면 잠깐 primary 가 둘이 되는데,
    부분 UNIQUE 가 그 순간을 거부해 교체 자체가 실패한다.
    """
    src = get_product_source(db, org_id=org_id, source_id=source_id)
    if src["status"] != "ACTIVE":
        raise ConflictError("중단된 소싱처를 주 소싱처로 지정할 수 없습니다.")

    _clear_primary(db, product_id=UUID(src["product_id"]), actor_user_id=actor_user_id)
    db.execute(text("""
        UPDATE product_source SET is_primary = TRUE, updated_at = now(), updated_by = :a
         WHERE id = :i AND org_id = :org
    """), {"i": str(source_id), "org": str(org_id), "a": str(actor_user_id)})

    record_audit(
        db, org_id=org_id, domain_table="product_source", domain_row_id=source_id,
        action="UPDATE", actor_user_id=actor_user_id,
        after={"is_primary": True}, reason="주 소싱처 변경",
    )
    db.commit()
    return get_product_source(db, org_id=org_id, source_id=source_id)


def _clear_primary(db: Session, *, product_id: UUID, actor_user_id: UUID) -> None:
    db.execute(text("""
        UPDATE product_source
           SET is_primary = FALSE, updated_at = now(), updated_by = :a
         WHERE product_id = :p AND is_primary AND deleted_at IS NULL
    """), {"p": str(product_id), "a": str(actor_user_id)})


_SOURCE_PATCHABLE = (
    "source_url", "external_source_product_id", "currency",
    "moq", "lead_time_days", "weight_g", "status", "memo",
)


def update_product_source(
    db: Session, *, org_id: UUID, actor_user_id: UUID, source_id: UUID, **changes: Any,
) -> dict[str, Any]:
    """소싱처 수정.

    **`is_primary` 는 여기서 못 바꾼다.** 주 소싱처 교체는 내리고 올리는 두 단계라
    한 트랜잭션이어야 한다 — `set_primary_source` 가 담당한다.
    """
    before = get_product_source(db, org_id=org_id, source_id=source_id)
    unknown = sorted(set(changes) - set(_SOURCE_PATCHABLE))
    if unknown:
        raise ValidationError(f"수정할 수 없는 항목입니다: {unknown}")
    if "source_url" in changes:
        assert_safe_url(changes["source_url"])
    if not changes:
        return before

    sets = ", ".join(f"{k} = :{k}" for k in changes)
    db.execute(text(f"""
        UPDATE product_source SET {sets}, updated_at = now(), updated_by = :actor
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {**changes, "i": str(source_id), "org": str(org_id),
           "actor": str(actor_user_id)})

    record_audit(
        db, org_id=org_id, domain_table="product_source", domain_row_id=source_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={k: before.get(k) for k in changes},
        after={k: str(v) for k, v in changes.items()},
    )
    db.commit()
    return get_product_source(db, org_id=org_id, source_id=source_id)


def archive_product_source(
    db: Session, *, org_id: UUID, actor_user_id: UUID, source_id: UUID, reason: str,
) -> None:
    if not reason.strip():
        raise ValidationError("소싱처 정리에는 사유가 필요합니다.")
    res = db.execute(text("""
        UPDATE product_source
           SET deleted_at = now(), deleted_by = :actor, is_primary = FALSE
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(source_id), "org": str(org_id), "actor": str(actor_user_id)})
    if res.rowcount == 0:
        raise NotFoundError("소싱처를 찾을 수 없습니다.")
    record_audit(
        db, org_id=org_id, domain_table="product_source", domain_row_id=source_id,
        action="DELETE", actor_user_id=actor_user_id, reason=reason,
    )
    db.commit()


def get_product_source(db: Session, *, org_id: UUID, source_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT ps.id, ps.product_id, ps.supplier_site_id, ps.source_url,
               ps.external_source_product_id, ps.currency, ps.moq,
               ps.lead_time_days, ps.weight_g, ps.is_primary, ps.status,
               ps.last_checked_at, ps.memo,
               s.name AS supplier_name, s.supplier_code, s.country_code
          FROM product_source ps
          JOIN supplier_site s ON s.id = ps.supplier_site_id
         WHERE ps.id = :i AND ps.org_id = :org AND ps.deleted_at IS NULL
    """), {"i": str(source_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("소싱처를 찾을 수 없습니다.")
    d = dict(row)
    for k in ("id", "product_id", "supplier_site_id"):
        d[k] = str(d[k])
    return d


def list_product_sources(
    db: Session, *, org_id: UUID, product_id: UUID
) -> list[dict[str, Any]]:
    """주 소싱처가 먼저 나온다 — 화면이 정렬을 다시 하지 않게."""
    rows = db.execute(text("""
        SELECT ps.id, ps.supplier_site_id, ps.source_url, ps.currency,
               ps.moq, ps.lead_time_days, ps.is_primary, ps.status,
               ps.last_checked_at,
               s.name AS supplier_name, s.supplier_code, s.country_code
          FROM product_source ps
          JOIN supplier_site s ON s.id = ps.supplier_site_id
         WHERE ps.org_id = :org AND ps.product_id = :p AND ps.deleted_at IS NULL
         ORDER BY ps.is_primary DESC, s.name
    """), {"org": str(org_id), "p": str(product_id)}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["supplier_site_id"] = str(d["supplier_site_id"])
        out.append(d)
    return out


def _assert_product(db: Session, org_id: UUID, product_id: UUID) -> None:
    ok = db.execute(text("""
        SELECT 1 FROM product WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(product_id), "org": str(org_id)}).first()
    if ok is None:
        raise NotFoundError("상품을 찾을 수 없습니다.")


def _assert_supplier(db: Session, org_id: UUID, supplier_id: UUID) -> None:
    ok = db.execute(text("""
        SELECT 1 FROM supplier_site
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(supplier_id), "org": str(org_id)}).first()
    if ok is None:
        # 조직 격리 — 다른 org 의 공급처도 404 다.
        raise NotFoundError("공급처를 찾을 수 없습니다.")


# ──────────────────────────────────────────────────────────────────────────
# 원가 version
# ──────────────────────────────────────────────────────────────────────────

def add_cost_version(
    db: Session, *, org_id: UUID, actor_user_id: UUID, product_source_id: UUID,
    purchase_price: int, currency: str, valid_from: date,
    source_type: str = "MANUAL", memo: str | None = None,
    **components: int | None,
) -> dict[str, Any]:
    """새 원가 version.

    **기존 version 을 UPDATE 하지 않는다.** 앞 version 의 `valid_to` 를 새 시작일로
    닫고(반열린 구간이라 그날부터 새 값이다) 새 행을 넣는다. 과거에 얼마였는지가
    그 시점 판단의 근거이므로 사라지면 안 된다.
    """
    src = get_product_source(db, org_id=org_id, source_id=product_source_id)
    if purchase_price < 0:
        raise ValidationError("매입가는 음수가 될 수 없습니다.")
    unknown = set(components) - set(COST_COMPONENTS)
    if unknown:
        raise ValidationError(f"알 수 없는 비용 항목입니다: {sorted(unknown)}")

    # 앞 version 을 닫는다. 겹치면 EXCLUDE 제약이 거부한다 — 여기서 먼저 정리한다.
    db.execute(text("""
        UPDATE product_source_cost_version
           SET valid_to = :from
         WHERE product_source_id = :ps AND deleted_at IS NULL
           AND valid_from < :from AND (valid_to IS NULL OR valid_to > :from)
    """), {"ps": str(product_source_id), "from": valid_from})

    cols = ", ".join(COST_COMPONENTS)
    binds = ", ".join(f":{c}" for c in COST_COMPONENTS)
    params: dict[str, Any] = {
        "org": str(org_id), "ps": str(product_source_id),
        "price": purchase_price, "cur": currency, "from": valid_from,
        "st": source_type, "memo": memo, "actor": str(actor_user_id),
        **{c: components.get(c) for c in COST_COMPONENTS},
    }
    try:
        row = db.execute(text(f"""
            INSERT INTO product_source_cost_version
                   (org_id, product_source_id, purchase_price, currency,
                    valid_from, source_type, memo, observed_at, created_by, {cols})
            VALUES (:org, :ps, :price, :cur, :from, :st, :memo, now(), :actor, {binds})
            RETURNING id
        """), params).mappings().one()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        domain = translate_integrity_error(exc, {
            "ex_source_cost_no_overlap": lambda: ConflictError(
                "같은 기간에 이미 원가가 있습니다. 적용일을 확인하세요.",
                detail={"valid_from": valid_from.isoformat()},
            ),
        })
        if domain is not None:
            raise domain from exc
        raise

    cid = UUID(str(row["id"]))
    record_audit(
        db, org_id=org_id, domain_table="product_source_cost_version",
        domain_row_id=cid, action="CREATE", actor_user_id=actor_user_id,
        after={"product_source_id": str(product_source_id),
               "purchase_price": purchase_price, "currency": currency,
               "valid_from": valid_from.isoformat()},
        reason=f"{src['supplier_name']} 소싱 원가 등록",
    )
    db.commit()
    return cost_version_at(db, org_id=org_id, product_source_id=product_source_id,
                           at=valid_from) or {}


def cost_version_at(
    db: Session, *, org_id: UUID, product_source_id: UUID, at: date,
) -> dict[str, Any] | None:
    """그날 유효했던 원가. 반열린 구간 `[valid_from, valid_to)`."""
    row = db.execute(text("""
        SELECT id, purchase_price, currency, valid_from, valid_to, source_type,
               domestic_shipping, international_shipping, customs_duty,
               import_vat, packaging_cost, other_cost, observed_at, memo
          FROM product_source_cost_version
         WHERE org_id = :org AND product_source_id = :ps AND deleted_at IS NULL
           AND valid_from <= :at AND (valid_to IS NULL OR valid_to > :at)
    """), {"org": str(org_id), "ps": str(product_source_id), "at": at}).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["id"] = str(d["id"])
    return d


def cost_history(
    db: Session, *, org_id: UUID, product_source_id: UUID
) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, purchase_price, currency, valid_from, valid_to, source_type,
               domestic_shipping, international_shipping, customs_duty,
               import_vat, packaging_cost, other_cost, observed_at
          FROM product_source_cost_version
         WHERE org_id = :org AND product_source_id = :ps AND deleted_at IS NULL
         ORDER BY valid_from DESC
    """), {"org": str(org_id), "ps": str(product_source_id)}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        out.append(d)
    return out


# ──────────────────────────────────────────────────────────────────────────
# 환율
# ──────────────────────────────────────────────────────────────────────────

def record_fx_rate(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    base_currency: str, quote_currency: str, rate: Decimal | float | str,
    rate_type: str = "SOURCING", observed_at: datetime | None = None,
    source_type: str = "MANUAL", source_name: str | None = None,
) -> dict[str, Any]:
    if rate_type not in FX_RATE_TYPES:
        raise ValidationError(f"알 수 없는 환율 유형입니다: {rate_type}")
    value = Decimal(str(rate))
    if value <= 0:
        raise ValidationError("환율은 0보다 커야 합니다.")
    if base_currency == quote_currency:
        raise ValidationError("같은 통화 사이의 환율은 의미가 없습니다.")

    row = db.execute(text("""
        INSERT INTO fx_rate_snapshot
               (org_id, base_currency, quote_currency, rate, rate_type,
                observed_at, source_type, source_name, created_by)
        VALUES (:org, :b, :q, :rate, :rt, COALESCE(:obs, now()), :st, :sn, :actor)
        RETURNING id, observed_at
    """), {"org": str(org_id), "b": base_currency, "q": quote_currency,
           "rate": value, "rt": rate_type, "obs": observed_at,
           "st": source_type, "sn": source_name,
           "actor": str(actor_user_id)}).mappings().one()

    record_audit(
        db, org_id=org_id, domain_table="fx_rate_snapshot",
        domain_row_id=UUID(str(row["id"])), action="CREATE",
        actor_user_id=actor_user_id,
        after={"pair": f"{base_currency}/{quote_currency}", "rate": str(value),
               "rate_type": rate_type},
    )
    db.commit()
    return {"id": str(row["id"]), "base_currency": base_currency,
            "quote_currency": quote_currency, "rate": value,
            "rate_type": rate_type, "observed_at": row["observed_at"]}


def list_fx_rates(
    db: Session, *, org_id: UUID, base_currency: str | None = None,
    quote_currency: str = "KRW", rate_type: str = "SOURCING", limit: int = 100,
) -> list[dict[str, Any]]:
    where = ["org_id = :org", "deleted_at IS NULL", "quote_currency = :q",
             "rate_type = :rt"]
    params: dict[str, Any] = {"org": str(org_id), "q": quote_currency,
                              "rt": rate_type, "lim": limit}
    if base_currency:
        where.append("base_currency = :b")
        params["b"] = base_currency

    rows = db.execute(text(f"""
        SELECT id, base_currency, quote_currency, rate, rate_type,
               observed_at, source_type, source_name
          FROM fx_rate_snapshot
         WHERE {" AND ".join(where)}
         ORDER BY observed_at DESC
         LIMIT :lim
    """), params).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["rate"] = str(d["rate"])
        out.append(d)
    return out


def latest_fx_rate(
    db: Session, *, org_id: UUID, base_currency: str, quote_currency: str,
    rate_type: str = "SOURCING", at: datetime | None = None,
) -> dict[str, Any] | None:
    """기준 시점 이전의 가장 최근 환율.

    **없으면 `None`.** 임의값으로 채우지 않는다 — 환율을 지어내면 마진이
    통째로 틀리고, 사용자는 그 숫자로 가격을 정한다.
    """
    if base_currency == quote_currency:
        return {"rate": Decimal("1"), "observed_at": None, "source_type": "IDENTITY",
                "base_currency": base_currency, "quote_currency": quote_currency}
    row = db.execute(text("""
        SELECT id, rate, observed_at, source_type, source_name
          FROM fx_rate_snapshot
         WHERE org_id = :org AND deleted_at IS NULL
           AND base_currency = :b AND quote_currency = :q AND rate_type = :rt
           AND (CAST(:at AS TIMESTAMPTZ) IS NULL OR observed_at <= CAST(:at AS TIMESTAMPTZ))
         ORDER BY observed_at DESC
         LIMIT 1
    """), {"org": str(org_id), "b": base_currency, "q": quote_currency,
           "rt": rate_type, "at": at}).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["id"] = str(d["id"])
    d["base_currency"] = base_currency
    d["quote_currency"] = quote_currency
    return d


# ──────────────────────────────────────────────────────────────────────────
# 예상 landed cost
# ──────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class LandedCost:
    """소싱 의사결정용 예상 원가.

    **세무·회계상 취득원가와 같다고 가정하지 않는다.** 수입 부가세의 공제 여부처럼
    회계 처리가 갈리는 항목이 있고, 그 판단은 세금 정책이 한다. 여기 숫자는
    "이 값에 팔면 남는가" 를 보기 위한 경영관리용이다.
    """

    total_krw: int | None
    currency: str
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    fx: dict[str, Any] | None = None
    #: 계산에 필요한데 없는 것. 비어 있지 않으면 `total_krw` 는 신뢰할 수 없다.
    missing: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing and self.total_krw is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_krw": self.total_krw,
            "currency": self.currency,
            "breakdown": self.breakdown,
            "fx": (
                {**self.fx, "rate": str(self.fx["rate"]),
                 "observed_at": self.fx["observed_at"].isoformat()
                 if self.fx.get("observed_at") else None}
                if self.fx else None
            ),
            "missing": self.missing,
            "is_complete": self.is_complete,
        }


def _to_krw(amount: int, rate: Decimal) -> int:
    """원 단위로 반올림. float 를 쓰지 않는다 (CLAUDE.md 계산 규칙)."""
    return int((Decimal(amount) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_landed_cost(
    db: Session, *, org_id: UUID, product_source_id: UUID,
    at: date | None = None, fx_rate_override: Decimal | None = None,
) -> LandedCost:
    """소싱처 하나의 예상 원가(원화).

    누락값을 0 으로 가정하지 않는다
    ---------------------------
    환율이 없으면 `missing` 에 담고 총액을 `None` 으로 둔다. 0 으로 채우면
    "원가 0원, 마진 100%" 라는 숫자가 나오고 그건 명백히 거짓인데도
    화면에서는 그럴듯해 보인다.

    부대비용은 다르다: 입력하지 않은 항목은 **0 으로 본다.** 국제배송비를
    입력하지 않은 국내 소싱에 "계산 불가" 를 띄우면 국내 상품은 영원히
    원가를 못 본다. 대신 breakdown 에 항목별로 남겨 무엇이 0인지 보이게 한다.
    """
    day = at or date.today()
    cost = cost_version_at(db, org_id=org_id, product_source_id=product_source_id, at=day)
    if cost is None:
        return LandedCost(total_krw=None, currency="KRW", missing=["SOURCE_COST"])

    currency = cost["currency"]
    fx = None
    if currency == "KRW":
        rate = Decimal("1")
    elif fx_rate_override is not None:
        rate = Decimal(str(fx_rate_override))
        fx = {"rate": rate, "source_type": "OVERRIDE", "observed_at": None,
              "base_currency": currency, "quote_currency": "KRW"}
    else:
        fx = latest_fx_rate(db, org_id=org_id, base_currency=currency,
                            quote_currency="KRW")
        if fx is None:
            # 환율을 지어내지 않는다.
            return LandedCost(
                total_krw=None, currency=currency,
                missing=[f"FX_RATE:{currency}/KRW"],
            )
        rate = Decimal(str(fx["rate"]))

    breakdown: list[dict[str, Any]] = []
    total = 0

    def add(label: str, key: str, amount: int | None, foreign: bool) -> None:
        nonlocal total
        value = int(amount or 0)
        krw = _to_krw(value, rate) if foreign else value
        total += krw
        breakdown.append({
            "key": key, "label": label,
            "amount": value, "currency": currency if foreign else "KRW",
            "amount_krw": krw,
            # 입력하지 않아 0 인지, 실제로 0 인지 화면이 구분할 수 있게 한다.
            "provided": amount is not None,
        })

    add("매입가", "purchase_price", cost["purchase_price"], foreign=True)
    # 공급처 국내배송비는 공급처 통화다 (중국 내 배송비는 위안).
    add("현지 배송비", "domestic_shipping", cost["domestic_shipping"], foreign=True)
    # 나머지는 국내에서 원화로 낸다.
    add("국제 배송비", "international_shipping", cost["international_shipping"], foreign=False)
    add("관세", "customs_duty", cost["customs_duty"], foreign=False)
    add("수입 부가세", "import_vat", cost["import_vat"], foreign=False)
    add("포장비", "packaging_cost", cost["packaging_cost"], foreign=False)
    add("기타", "other_cost", cost["other_cost"], foreign=False)

    return LandedCost(total_krw=total, currency=currency, breakdown=breakdown, fx=fx)


def primary_source_of(
    db: Session, *, org_id: UUID, product_id: UUID
) -> dict[str, Any] | None:
    row = db.execute(text("""
        SELECT id FROM product_source
         WHERE org_id = :org AND product_id = :p
           AND is_primary AND status = 'ACTIVE' AND deleted_at IS NULL
    """), {"org": str(org_id), "p": str(product_id)}).first()
    if row is None:
        return None
    return get_product_source(db, org_id=org_id, source_id=UUID(str(row[0])))
