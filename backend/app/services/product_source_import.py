"""상품↔공급처 연결 대량 가져오기.

왜 이게 지금 가장 급한가
--------------------
재고·발주·재주문이 전부 만들어져 있는데 `product_source` 가 **0건**이라
재주문이 전부 `NO_SOURCE` 로 막혀 있다. 상품 1,065개를 화면에서 한 건씩
연결하게 두면 아무도 안 한다.

원본 데이터가 없다는 사실
----------------------
2026-08-11 에 실제 데이터를 다 뒤졌다.

```
product.supply_url        22/1,065  전부 일본 아마존·라쿠텐 — 공급처 54곳과 무관
product.supplier_id       0
아이템리스트 시트           308행 중 제품명이 있는 것 4행 (사실상 빈 양식)
마진계산 시트              1,035행, **공급처 열 자체가 없음**
도매 매입 사이트들.xlsx     공급처 목록만 (이미 54건 등록됨)
```

**즉 상품↔공급처 매핑 데이터는 어디에도 없다.** 그래서 이 모듈은 기존 파일을
읽는 도구가 아니라, 사용자가 그 정보를 **넣을 수 있게 하는** 도구다.
`template()` 이 필요한 열을 알려준다.

추측해서 연결하지 않는다
--------------------
이름이 비슷하다고, URL 이 비슷하다고 연결하지 않는다. 잘못 연결하면 **틀린
공급처의 원가로 발주가 나간다.** 못 찾은 채로 두는 편이 훨씬 안전하다.

URL 두 개를 섞지 않는다
--------------------
```
supplier_site.website_url   https://domeggook.com          공급처 사이트
product_source.source_url   https://domeggook.com/item/123 이 상품의 페이지
```

둘은 다른 것이다. 합치면 "이 공급처의 주소" 와 "이 상품을 파는 페이지" 를
구분할 수 없게 된다.

원가는 append-only
-----------------
공급가가 들어와도 `product.cost_amount` 를 건드리지 않는다. 그건 마진 계산에
쓰이는 확정값이고 `COST_CHANGE` 승인을 거쳐야 바뀐다. 여기서는
`product_source_cost_version` 에 한 줄 **추가**할 뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services.supplier_import import (  # noqa: F401  (같은 어휘를 쓴다)
    MODES,
    ImportNotAllowedError,
    assert_import_allowed,
)

#: 상품을 찾는 열쇠. 순서가 우선순위다.
PRODUCT_KEYS = ("sku", "business_key", "product_id")

#: 공급처를 찾는 열쇠. `supplier_domain` 은 **정규화된 호스트명 완전일치**다 —
#: 부분일치나 유사도가 아니다.
SUPPLIER_KEYS = ("supplier_code", "supplier_domain", "supplier_site_id")

_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "sku": ("sku", "상품코드", "품번"),
    "business_key": ("business_key", "상품번호", "관리번호"),
    "product_id": ("product_id", "상품id"),
    "supplier_code": ("supplier_code", "공급처코드"),
    "supplier_domain": ("supplier_domain", "공급처도메인", "공급처사이트"),
    "supplier_site_id": ("supplier_site_id", "공급처id"),
    "source_url": ("source_url", "공급처url", "상품url", "매입url"),
    "lead_time_days": ("lead_time_days", "리드타임", "리드타임일"),
    "moq": ("moq", "최소주문수량", "최소수량"),
    "is_primary": ("is_primary", "주소싱처", "대표"),
    "purchase_price": ("purchase_price", "매입가", "공급가", "원가"),
    "currency": ("currency", "통화"),
    "memo": ("memo", "메모", "비고"),
}

#: 한 행의 처리 결과.
OUTCOMES = (
    "NEW_MAPPING",       # 새 연결
    "UPDATE_METADATA",   # 연결은 있고 리드타임·MOQ 등이 채워진다
    "EXISTING_SAME",     # 이미 같다 — 아무것도 안 한다
    "CONFLICT_PRIMARY",  # 이미 다른 주 소싱처가 있다
    "UNMATCHED_PRODUCT",
    "UNMATCHED_SUPPLIER",
    "INVALID",
)

#: 비밀로 보이는 열 이름. 값을 읽지도 않고 버린다.
_CREDENTIAL_HEADERS = ("id", "pw", "password", "비밀번호", "아이디", "계정")


@dataclass
class RowPlan:
    line_no: int
    outcome: str
    product_id: UUID | None = None
    product_name: str | None = None
    supplier_site_id: UUID | None = None
    supplier_name: str | None = None
    source_url: str | None = None
    lead_time_days: int | None = None
    moq: int | None = None
    is_primary: bool = False
    purchase_price: int | None = None
    currency: str = "KRW"
    memo: str | None = None
    #: 무엇이 채워지는지. `UPDATE_METADATA` 에서 사람이 보고 판단한다.
    changes: dict[str, Any] = field(default_factory=dict)
    message: str | None = None


@dataclass
class ImportPlan:
    mode: str
    rows: list[RowPlan] = field(default_factory=list)
    file_problems: list[str] = field(default_factory=list)
    #: 버린 비밀 열의 **개수**. 값은 읽지 않았다.
    credential_headers_skipped: int = 0

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in OUTCOMES}
        for r in self.rows:
            out[r.outcome] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "mode": self.mode,
            "rows_total": len(self.rows),
            "counts": counts,
            "credential_detected": self.credential_headers_skipped > 0,
            "credential_headers_skipped": self.credential_headers_skipped,
            "lead_time_available": sum(
                1 for r in self.rows if r.lead_time_days is not None),
            "moq_available": sum(1 for r in self.rows if r.moq is not None),
            "source_cost_candidates": sum(
                1 for r in self.rows if r.purchase_price is not None),
            "problems": [_row_dict(r) for r in self.rows if r.outcome in (
                "UNMATCHED_PRODUCT", "UNMATCHED_SUPPLIER", "CONFLICT_PRIMARY",
                "INVALID")][:200],
            "changes": [_row_dict(r) for r in self.rows if r.outcome in (
                "NEW_MAPPING", "UPDATE_METADATA")][:200],
            "file_problems": self.file_problems,
            "dry_run": self.mode != "APPLY",
        }


def _row_dict(r: RowPlan) -> dict[str, Any]:
    return {
        "line_no": r.line_no, "outcome": r.outcome,
        "product_name": r.product_name, "supplier_name": r.supplier_name,
        "source_url": r.source_url, "lead_time_days": r.lead_time_days,
        "moq": r.moq, "is_primary": r.is_primary,
        "purchase_price": r.purchase_price,
        "changes": r.changes, "message": r.message,
    }


# ── 파일 읽기 ──────────────────────────────────────────────────


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def normalize_host(url_or_host: str) -> str | None:
    """URL 이나 호스트에서 **정규화된 호스트명**을 뽑는다.

    `www.` 만 벗긴다. `m.` 은 벗기지 않는다 — `m.petbtob.co.kr` 이 실제로
    별도 공급처로 등록돼 있고, 모바일 도메인이 다른 업체인 경우가 있다.
    추측으로 합치면 엉뚱한 공급처에 연결된다.
    """
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return None
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def _map_headers(header_row: tuple[Any, ...]) -> tuple[dict[str, int], int]:
    lookup: dict[str, str] = {}
    for field_name, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            lookup[_norm(alias)] = field_name

    out: dict[str, int] = {}
    skipped = 0
    for idx, cell in enumerate(header_row):
        key = _norm(cell)
        if key in (_norm(c) for c in _CREDENTIAL_HEADERS):
            # 값을 읽지 않는다. 열 번호를 기억하지도 않는다.
            skipped += 1
            continue
        field_name = lookup.get(key)
        if field_name and field_name not in out:
            out[field_name] = idx
    return out, skipped


def _int_or_none(value: Any, label: str) -> tuple[int | None, str | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        # **없으면 없는 것이다.** 임의값을 만들지 않는다.
        return None, None
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None, f"{label} 를 숫자로 읽을 수 없습니다: {value!r}"
    if number != int(number) or number < 0:
        return None, f"{label} 가 0 이상 정수가 아닙니다: {value!r}"
    return int(number), None


def _bool_or_false(value: Any) -> bool:
    if value is None:
        return False
    return _norm(value) in ("1", "true", "y", "yes", "o", "예", "주", "대표")


# ── 계획 ───────────────────────────────────────────────────────


def plan(
    db: Session, *, org_id: UUID, rows: list[tuple[Any, ...]], mode: str,
) -> ImportPlan:
    """무엇이 일어날지 계산한다. **아무것도 쓰지 않는다.**"""
    if mode not in MODES:
        raise ValidationError(f"알 수 없는 단계입니다: {mode}")

    result = ImportPlan(mode=mode)
    if not rows:
        result.file_problems.append("파일이 비어 있습니다.")
        return result

    headers, skipped = _map_headers(rows[0])
    result.credential_headers_skipped = skipped

    if not any(k in headers for k in PRODUCT_KEYS):
        result.file_problems.append(
            "상품을 찾을 열이 없습니다. 'SKU'·'상품번호'·'product_id' 중 "
            "하나가 필요합니다. 상품명으로는 찾지 않습니다.")
    if not any(k in headers for k in SUPPLIER_KEYS):
        result.file_problems.append(
            "공급처를 찾을 열이 없습니다. '공급처코드'·'공급처도메인'·"
            "'supplier_site_id' 중 하나가 필요합니다.")
    if result.file_problems:
        return result

    def cell(row: tuple[Any, ...], key: str) -> Any:
        idx = headers.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    for line_no, row in enumerate(rows[1:], start=2):
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        result.rows.append(
            _plan_row(db, org_id=org_id, line_no=line_no, row=row, cell=cell,
                      mode=mode))
    return result


def _plan_row(
    db: Session, *, org_id: UUID, line_no: int, row: tuple[Any, ...],
    cell: Any, mode: str,
) -> RowPlan:
    lead, lead_err = _int_or_none(cell(row, "lead_time_days"), "리드타임")
    moq, moq_err = _int_or_none(cell(row, "moq"), "MOQ")
    price, price_err = _int_or_none(cell(row, "purchase_price"), "매입가")
    error = lead_err or moq_err or price_err
    if error:
        return RowPlan(line_no=line_no, outcome="INVALID", message=error)

    url = cell(row, "source_url")
    url = str(url).strip() if url is not None and str(url).strip() else None
    memo = cell(row, "memo")
    memo = str(memo).strip() if memo is not None and str(memo).strip() else None
    currency = str(cell(row, "currency") or "KRW").strip().upper()

    base = RowPlan(
        line_no=line_no, outcome="INVALID", source_url=url,
        lead_time_days=lead, moq=moq, is_primary=_bool_or_false(
            cell(row, "is_primary")),
        purchase_price=price, currency=currency, memo=memo,
    )

    if mode == "ANALYZE":
        # 파일만 본다. DB 를 보지 않는다.
        base.outcome = "NEW_MAPPING"
        return base

    product = _find_product(db, org_id=org_id, row=row, cell=cell)
    if product is None:
        base.outcome = "UNMATCHED_PRODUCT"
        base.message = "이 열쇠로 상품을 찾지 못했습니다."
        return base
    base.product_id, base.product_name = product

    supplier = _find_supplier(db, org_id=org_id, row=row, cell=cell)
    if supplier is None:
        base.outcome = "UNMATCHED_SUPPLIER"
        base.message = ("이 열쇠로 공급처를 찾지 못했습니다. 공급처를 먼저 "
                        "등록하세요.")
        return base
    base.supplier_site_id, base.supplier_name = supplier

    existing = db.execute(text("""
        SELECT id, source_url, lead_time_days, moq, is_primary
          FROM product_source
         WHERE org_id = :o AND product_id = :p AND supplier_site_id = :s
           AND deleted_at IS NULL
    """), {"o": str(org_id), "p": str(base.product_id),
           "s": str(base.supplier_site_id)}).mappings().first()

    if existing is None:
        base.outcome = "NEW_MAPPING"
        if base.is_primary:
            other = db.execute(text("""
                SELECT 1 FROM product_source
                 WHERE org_id = :o AND product_id = :p AND is_primary
                   AND status = 'ACTIVE' AND deleted_at IS NULL
            """), {"o": str(org_id), "p": str(base.product_id)}).scalar()
            if other:
                # 주 소싱처를 말없이 바꾸면 발주가 다른 곳으로 나간다.
                base.outcome = "CONFLICT_PRIMARY"
                base.message = ("이미 다른 주 소싱처가 있습니다. 바꾸려면 "
                                "기존 것을 먼저 해제하세요.")
        return base

    # 이미 연결돼 있다. **비어 있는 것만 채운다.**
    #
    # 값이 다른데 덮어쓰면 사람이 화면에서 고친 것이 파일 한 번으로 사라진다.
    changes: dict[str, Any] = {}
    for column, value in (("source_url", url), ("lead_time_days", lead),
                          ("moq", moq)):
        if value is not None and existing[column] is None:
            changes[column] = value
    if base.is_primary and not existing["is_primary"]:
        other = db.execute(text("""
            SELECT 1 FROM product_source
             WHERE org_id = :o AND product_id = :p AND is_primary
               AND status = 'ACTIVE' AND deleted_at IS NULL AND id <> :me
        """), {"o": str(org_id), "p": str(base.product_id),
               "me": str(existing["id"])}).scalar()
        if other:
            base.outcome = "CONFLICT_PRIMARY"
            base.message = "이미 다른 주 소싱처가 있습니다."
            return base
        changes["is_primary"] = True

    base.changes = changes
    # 매입가는 항상 새 버전으로 쌓인다 — 값이 같아도 "그날 확인했다" 는 사실이다.
    base.outcome = "UPDATE_METADATA" if (changes or price is not None) else "EXISTING_SAME"
    return base


def _find_product(
    db: Session, *, org_id: UUID, row: tuple[Any, ...], cell: Any,
) -> tuple[UUID, str] | None:
    """상품 찾기 — **완전일치만.**"""
    for key in PRODUCT_KEYS:
        raw = cell(row, key)
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip()
        column = "id" if key == "product_id" else key
        cast = "uuid" if column == "id" else "text"
        try:
            found = db.execute(text(f"""
                SELECT id, name_ko FROM product
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND {column} = CAST(:v AS {cast})
            """), {"o": str(org_id), "v": value}).mappings().first()
        except Exception:
            db.rollback()
            continue
        if found:
            return UUID(str(found["id"])), found["name_ko"]
    return None


def _find_supplier(
    db: Session, *, org_id: UUID, row: tuple[Any, ...], cell: Any,
) -> tuple[UUID, str] | None:
    """공급처 찾기 — **완전일치만.**

    도메인은 정규화 후 완전일치다. `domeggook.com` 과 `domeggook.co.kr` 은
    다른 것으로 본다 — 실제로 다를 수 있고, 틀리면 남의 원가로 발주가 나간다.
    """
    for key in SUPPLIER_KEYS:
        raw = cell(row, key)
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip()
        if key == "supplier_domain":
            host = normalize_host(value)
            if not host:
                continue
            found = db.execute(text("""
                SELECT id, name FROM supplier_site
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND lower(website_domain) = :h
            """), {"o": str(org_id), "h": host}).mappings().first()
        else:
            column = "id" if key == "supplier_site_id" else "supplier_code"
            cast = "uuid" if column == "id" else "text"
            try:
                found = db.execute(text(f"""
                    SELECT id, name FROM supplier_site
                     WHERE org_id = :o AND deleted_at IS NULL
                       AND {column} = CAST(:v AS {cast})
                """), {"o": str(org_id), "v": value}).mappings().first()
            except Exception:
                db.rollback()
                continue
        if found:
            return UUID(str(found["id"])), found["name"]
    return None


# ── 반영 ───────────────────────────────────────────────────────


def apply_plan(
    db: Session, *, org_id: UUID, actor_user_id: UUID, plan_result: ImportPlan,
    source_name: str,
) -> dict[str, Any]:
    """연결을 만든다.

    `product.cost_amount` 를 **절대 건드리지 않는다.** 매입가는
    `product_source_cost_version` 에 새 줄로 쌓인다.
    """
    assert_import_allowed("APPLY")
    from app.core.audit import record_audit

    created = 0
    updated = 0
    cost_versions = 0

    for r in plan_result.rows:
        if r.outcome == "NEW_MAPPING":
            source_id = _insert_source(
                db, org_id=org_id, actor_user_id=actor_user_id, row=r)
            if source_id is None:
                # `uq_product_source_pair` 가 막았다 — 동시에 같은 연결을
                # 만들려 한 것이다. check-then-insert 만 믿지 않는다.
                r.outcome = "EXISTING_SAME"
                r.message = "동시에 같은 연결이 만들어졌습니다."
                continue
            created += 1
            # 연결 한 건마다 감사를 남긴다. 이건 "어느 공급처에서 사는가" 라는
            # 업무 결정이고, 나중에 되짚을 수 있어야 한다.
            record_audit(
                db, org_id=org_id, domain_table="product_source",
                domain_row_id=source_id, action="CREATE",
                actor_user_id=actor_user_id,
                after={"supplier": r.supplier_name, "is_primary": r.is_primary},
                reason=f"소싱 연결 가져오기 ({source_name})",
            )
            if r.purchase_price is not None:
                _insert_cost_version(db, org_id=org_id, source_id=source_id,
                                     actor_user_id=actor_user_id, row=r)
                cost_versions += 1

        elif r.outcome == "UPDATE_METADATA":
            source_id = db.execute(text("""
                SELECT id FROM product_source
                 WHERE org_id = :o AND product_id = :p AND supplier_site_id = :s
                   AND deleted_at IS NULL
            """), {"o": str(org_id), "p": str(r.product_id),
                   "s": str(r.supplier_site_id)}).scalar()
            if source_id is None:
                continue
            source_id = UUID(str(source_id))
            if r.changes:
                _update_source(db, source_id=source_id,
                               actor_user_id=actor_user_id, changes=r.changes)
                updated += 1
                record_audit(
                    db, org_id=org_id, domain_table="product_source",
                    domain_row_id=source_id, action="UPDATE",
                    actor_user_id=actor_user_id, after=r.changes,
                    reason=f"소싱 정보 가져오기 ({source_name})",
                )
            if r.purchase_price is not None:
                if _insert_cost_version(
                    db, org_id=org_id, source_id=source_id,
                    actor_user_id=actor_user_id, row=r,
                ):
                    cost_versions += 1

    db.commit()
    return {
        "created": created, "updated": updated,
        "cost_versions": cost_versions,
        **plan_result.as_dict(),
    }


def _insert_source(
    db: Session, *, org_id: UUID, actor_user_id: UUID, row: RowPlan,
) -> UUID | None:
    """연결 생성. 이미 있으면 `None` — DB 제약이 최종 방어다."""
    result = db.execute(text("""
        INSERT INTO product_source (
          org_id, product_id, supplier_site_id, source_url, lead_time_days,
          moq, is_primary, status, currency, memo, created_by, updated_by
        ) VALUES (
          :o, :p, :s, :url, :lead, :moq, :prim, 'ACTIVE', :cur, :memo,
          :actor, :actor
        )
        ON CONFLICT (product_id, supplier_site_id)
          WHERE deleted_at IS NULL DO NOTHING
        RETURNING id
    """), {
        "o": str(org_id), "p": str(row.product_id),
        "s": str(row.supplier_site_id), "url": row.source_url,
        "lead": row.lead_time_days, "moq": row.moq, "prim": row.is_primary,
        "cur": row.currency, "memo": row.memo, "actor": str(actor_user_id),
    }).scalar()
    return UUID(str(result)) if result else None


def _update_source(
    db: Session, *, source_id: UUID, actor_user_id: UUID,
    changes: dict[str, Any],
) -> None:
    #: 파일이 바꿀 수 있는 열. 여기 없는 것은 파일로 못 바꾼다 —
    #: 상태·삭제 같은 것을 파일 한 줄로 뒤집을 수 있으면 안 된다.
    allowed = {"source_url", "lead_time_days", "moq", "is_primary"}
    sets = [f"{k} = :{k}" for k in changes if k in allowed]
    if not sets:
        return
    params: dict[str, Any] = {k: v for k, v in changes.items() if k in allowed}
    params["id"] = str(source_id)
    params["actor"] = str(actor_user_id)
    db.execute(text(f"""
        UPDATE product_source
           SET {', '.join(sets)}, updated_at = now(), updated_by = :actor
         WHERE id = :id
    """), params)


def _insert_cost_version(
    db: Session, *, org_id: UUID, source_id: UUID, actor_user_id: UUID,
    row: RowPlan,
) -> bool:
    """매입가를 **새 버전으로 쌓는다.**

    `product.cost_amount` 는 건드리지 않는다. 그건 마진 계산에 쓰이는 확정값이고
    `COST_CHANGE` 승인을 거쳐야 바뀐다. 여기서 바꾸면 파일 한 번으로 과거
    마진이 흔들린다.

    같은 날 같은 값이 이미 있으면 넣지 않는다 — 재적용 때 버전만 쌓인다.
    """
    today = date.today()
    same = db.execute(text("""
        SELECT 1 FROM product_source_cost_version
         WHERE org_id = :o AND product_source_id = :s AND deleted_at IS NULL
           AND purchase_price = :price AND valid_from = :today
    """), {"o": str(org_id), "s": str(source_id), "price": row.purchase_price,
           "today": today}).scalar()
    if same:
        return False

    db.execute(text("""
        INSERT INTO product_source_cost_version (
          org_id, product_source_id, purchase_price, currency, valid_from,
          observed_at, source_type, created_by
        ) VALUES (:o, :s, :price, :cur, :today, now(), 'MANUAL', :actor)
    """), {
        "o": str(org_id), "s": str(source_id), "price": row.purchase_price,
        "cur": row.currency, "today": today, "actor": str(actor_user_id),
    })
    return True


def completeness(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """소싱 정보가 얼마나 채워졌나.

    재주문이 왜 안 도는지 한눈에 보여주는 숫자다. 각 항목이 곧 재주문
    차단 사유(`NO_SOURCE` / `NO_LEAD_TIME` …)와 짝을 이룬다.
    """
    row = db.execute(text("""
        WITH p AS (
            SELECT id FROM product WHERE org_id = :o AND deleted_at IS NULL
        ),
        s AS (
            SELECT product_id,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE is_primary AND status = 'ACTIVE') AS primaries,
                   COUNT(*) FILTER (WHERE lead_time_days IS NOT NULL) AS with_lead,
                   COUNT(*) FILTER (WHERE moq IS NOT NULL) AS with_moq
              FROM product_source
             WHERE org_id = :o AND deleted_at IS NULL
             GROUP BY product_id
        )
        SELECT
          (SELECT COUNT(*) FROM p) AS products,
          (SELECT COUNT(*) FROM s) AS with_source,
          (SELECT COUNT(*) FROM s WHERE primaries = 0) AS without_primary,
          (SELECT COUNT(*) FROM s WHERE with_lead = 0) AS without_lead_time,
          (SELECT COUNT(*) FROM s WHERE with_moq = 0) AS without_moq,
          (SELECT COUNT(*) FROM product_source ps
            WHERE ps.org_id = :o AND ps.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM product_source_cost_version c
                 WHERE c.product_source_id = ps.id AND c.deleted_at IS NULL
              )) AS sources_without_cost
    """), {"o": str(org_id)}).mappings().one()

    products = row["products"]
    return {
        "products": products,
        "with_source": row["with_source"],
        "without_source": products - row["with_source"],
        "without_primary": row["without_primary"],
        "without_lead_time": row["without_lead_time"],
        "without_moq": row["without_moq"],
        "sources_without_cost": row["sources_without_cost"],
    }
