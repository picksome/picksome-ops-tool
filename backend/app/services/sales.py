"""판매 주문 CRUD 서비스 (docs/22_api_spec.md §5).

business_key 규칙: SO-{YYYY}-{seq:05d}
  seq = 해당 org+연도의 기존 비삭제 주문 수 + 1.
  동시성 경합은 DB UNIQUE 제약으로 최종 보호 (retry 는 호출자 책임).

multi-tenant 격리: 모든 쿼리에 org_id = :o 조건 필수.
soft-delete: deleted_at IS NULL 조건으로 논리 삭제 구분.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError
from app.core.ids import new_id
from app.core.month_lock import assert_period_writable
from app.domain.sales import SaleOrderCreate, SaleOrderOut, SaleOrderUpdate
from app.services.integration_account import resolve_account
from app.services.sale_channel import resolve_sale_channel


def _now() -> datetime:
    return datetime.now(tz=UTC)


# 계정 라벨은 `expenses` 와 같은 관례로 **평면 필드**로 붙인다
# (`category_name`, `supplier_name` …). 중첩 객체를 쓰면 이 화면만 모양이 달라진다.
#
# LEFT JOIN 이다: legacy 주문은 `integration_account_id` 가 NULL 이고,
# INNER JOIN 으로 바꾸면 **그 주문들이 목록에서 통째로 사라진다.**
_SELECT = """
  SELECT so.id, so.business_key,
         so.sale_date::text AS sale_date,
         so.marketplace::text AS marketplace,
         so.marketplace_order_no, so.line_no,
         so.product_id, so.product_snapshot_name,
         so.quantity, so.sale_price, so.ad_cost, so.shipping_fee,
         so.vat_inclusion_type::text AS vat_inclusion_type,
         so.memo, so.import_source, so.created_at,
         so.integration_account_id,
         ia.account_label AS integration_account_label
    FROM sales_order so
    LEFT JOIN integration_account ia ON ia.id = so.integration_account_id
"""


def _row_to_out(row: Any) -> SaleOrderOut:
    return SaleOrderOut(**row._mapping)


def list_sales(
    db: Session,
    *,
    org_id: UUID,
    start_date: str | None = None,
    end_date: str | None = None,
    marketplace: str | None = None,
    integration_account_id: UUID | None = None,
    product_id: UUID | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    # ⚠️ 컬럼을 `so.` 로 한정한다. `_SELECT` 가 `integration_account` 를 join 하는데
    # 그 테이블에도 `org_id`·`deleted_at`·`created_at` 이 있어서, 한정하지 않으면
    # "column reference is ambiguous" 로 목록 조회가 통째로 실패한다.
    where = ["so.org_id = :o", "so.deleted_at IS NULL"]
    params: dict[str, Any] = {"o": str(org_id)}

    if start_date:
        where.append("so.sale_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        where.append("so.sale_date <= :end_date")
        params["end_date"] = end_date
    if marketplace:
        where.append("so.marketplace = :marketplace")
        params["marketplace"] = marketplace
    if integration_account_id:
        # 같은 판매처에 계정이 여럿일 때 스토어별로 좁혀 본다.
        # `marketplace` 필터로는 본점과 2호점을 나눌 수 없다.
        where.append("so.integration_account_id = :account_id")
        params["account_id"] = str(integration_account_id)
    if product_id:
        where.append("so.product_id = :product_id")
        params["product_id"] = str(product_id)
    if q:
        where.append(
            "(so.marketplace_order_no ILIKE :q OR so.product_snapshot_name ILIKE :q"
            " OR so.business_key ILIKE :q)"
        )
        params["q"] = f"%{q}%"

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM sales_order so WHERE {where_sql}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(f"""
          {_SELECT}
           WHERE {where_sql}
           ORDER BY sale_date DESC, created_at DESC
           LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": page_size, "offset": offset},
    ).all()

    return {
        "items": [_row_to_out(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_sale(db: Session, *, org_id: UUID, sale_id: UUID) -> SaleOrderOut:
    row = db.execute(
        text(f"{_SELECT} WHERE so.id = :i AND so.org_id = :o AND so.deleted_at IS NULL"),
        {"i": str(sale_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("판매 주문을 찾을 수 없습니다.")
    return _row_to_out(row)


def _next_business_key(db: Session, *, org_id: UUID, year: int) -> str:
    """SO-{YYYY}-{seq:05d} 생성. seq = 해당 org+연도 기존 주문 수 + 1."""
    row = db.execute(
        text("""
          SELECT COUNT(*) AS c FROM sales_order
           WHERE org_id = :o
             AND EXTRACT(YEAR FROM sale_date) = :y
             AND deleted_at IS NULL
        """),
        {"o": str(org_id), "y": year},
    ).first()
    seq = (int(row.c) if row else 0) + 1
    return f"SO-{year}-{seq:05d}"


def _next_line_no(db: Session, *, org_id: UUID, marketplace: str, order_no: str) -> int:
    """같은 마켓 주문번호의 다음 라인 번호. 첫 라인이면 1.

    한 마켓 주문에 상품이 여러 개거나 분할 배송되면 판매장부 행이 여러 개 생긴다
    (M012). 그때 uq_sales_order_mkt_orderno 에 걸리지 않도록 자동 채번한다.
    """
    row = db.execute(
        text("""
          SELECT COALESCE(MAX(line_no), 0) AS m FROM sales_order
           WHERE org_id = :o AND marketplace = :mk AND marketplace_order_no = :ono
             AND deleted_at IS NULL
        """),
        {"o": str(org_id), "mk": marketplace, "ono": order_no},
    ).first()
    return (int(row.m) if row else 0) + 1


def create_sale(
    db: Session,
    *,
    org_id: UUID,
    actor: UUID | None,
    body: SaleOrderCreate,
    import_source: str = "MANUAL",
    import_batch_id: UUID | None = None,
) -> SaleOrderOut:
    """actor=None, import_source='BOT_AUTO'/'API_AUTO' 는 봇/API 자동입력 경로
    (services/integration_ingest.py) 용 — 사람이 개입하지 않아 audit actor 가 없다."""
    # 잠긴 월에는 판매를 추가할 수 없다. 프론트에서 버튼을 숨기는 것만으로는 부족하다 —
    # API 를 직접 부르면 뚫리므로 서비스 진입점에서 막는다 (docs/43 Phase 2).
    assert_period_writable(
        db, org_id=org_id, effective_date=body.sale_date,
        operation="CREATE", what="판매 내역",
    )
    resolve_sale_channel(db, org_id=org_id, code=body.marketplace)

    # 어느 판매자 계정에서 온 주문인가.
    #
    # **`marketplace` 문자열로 계정을 추론하지 않는다.** 같은 채널에 계정이 여럿이면
    # 어느 쪽인지 알 수 없고, 하나뿐이라도 "그 주문이 그 계정에서 왔다" 는 보장이 없다.
    # 계정을 아는 쪽(ingest 어댑터 / 사람)이 명시적으로 넘긴다.
    if body.integration_account_id is not None:
        resolve_account(
            db, org_id=org_id, account_id=body.integration_account_id,
            marketplace=body.marketplace,
        )

    year = int(body.sale_date[:4])
    business_key = _next_business_key(db, org_id=org_id, year=year)
    line_no = _next_line_no(
        db, org_id=org_id, marketplace=body.marketplace, order_no=body.marketplace_order_no
    )
    new_sid = new_id()

    db.execute(
        text("""
          INSERT INTO sales_order (
            id, org_id, business_key,
            sale_date, marketplace, marketplace_order_no, line_no,
            product_id, product_snapshot_name,
            quantity, sale_price, ad_cost, shipping_fee,
            vat_inclusion_type, memo, integration_account_id,
            import_source, import_batch_id, created_by, updated_by
          ) VALUES (
            :id, :o, :bk,
            :sale_date, :marketplace, :order_no, :line_no,
            :product_id, :snap_name,
            :quantity, :sale_price, :ad_cost, :shipping_fee,
            CAST(:vit AS enum_vat_inclusion_type), :memo, :account_id,
            :import_source, :import_batch_id, :actor, :actor
          )
        """),
        {
            "id": str(new_sid),
            "o": str(org_id),
            "bk": business_key,
            "sale_date": body.sale_date,
            "marketplace": body.marketplace,
            "order_no": body.marketplace_order_no,
            "line_no": line_no,
            "product_id": str(body.product_id) if body.product_id else None,
            "snap_name": body.product_snapshot_name,
            "quantity": body.quantity,
            "sale_price": body.sale_price,
            "ad_cost": body.ad_cost,
            "shipping_fee": body.shipping_fee,
            "vit": body.vat_inclusion_type,
            "memo": body.memo,
            "account_id": (
                str(body.integration_account_id) if body.integration_account_id else None
            ),
            "import_source": import_source,
            "import_batch_id": str(import_batch_id) if import_batch_id else None,
            "actor": str(actor) if actor else None,
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="sales_order",
        domain_row_id=new_sid,
        action="CREATE",
        actor_user_id=actor,
        after={
            "business_key": business_key,
            "sale_date": body.sale_date,
            "marketplace": body.marketplace,
            "sale_price": body.sale_price,
            "quantity": body.quantity,
            "import_source": import_source,
        },
    )
    db.commit()
    return get_sale(db, org_id=org_id, sale_id=new_sid)


def update_sale(
    db: Session, *, org_id: UUID, actor: UUID, sale_id: UUID, body: SaleOrderUpdate
) -> SaleOrderOut:
    before = get_sale(db, org_id=org_id, sale_id=sale_id)
    assert_period_writable(
        db, org_id=org_id, effective_date=before.sale_date,
        operation="UPDATE", what="판매 내역",
    )

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return before

    if fields.get("marketplace"):
        resolve_sale_channel(db, org_id=org_id, code=fields["marketplace"])

    # 계정을 바꿀 때도 생성과 같은 규칙으로 검증한다.
    # `exclude_unset` 이므로 키가 있으면 "명시적으로 지정했다" 는 뜻이고,
    # None 이면 "귀속을 지운다" 는 뜻이다(잘못 붙인 계정을 되돌릴 수 있어야 한다).
    if fields.get("integration_account_id") is not None:
        # 판매처를 같이 바꾸는 중일 수 있다 — **바뀐 뒤의 값**으로 검증해야 한다.
        # 기존 값으로 검사하면 "쿠팡→네이버 + 네이버 계정" 이 거절되고,
        # 반대로 "판매처만 바꾸기" 는 검사를 통째로 빠져나간다.
        resolve_account(
            db, org_id=org_id, account_id=fields["integration_account_id"],
            marketplace=fields.get("marketplace") or before.marketplace,
        )
    elif fields.get("marketplace") and before.integration_account_id:
        # 계정은 그대로 두고 판매처만 바꾸는 경우. 이것도 불일치를 만들 수 있다.
        resolve_account(
            db, org_id=org_id, account_id=before.integration_account_id,
            marketplace=fields["marketplace"],
        )

    # 마켓 또는 주문번호가 바뀌면 옮겨갈 주문의 라인 번호를 다시 채번한다.
    # 안 하면 이동 대상 주문에 이미 line_no=1 이 있을 때
    # uq_sales_order_mkt_orderno 위반으로 IntegrityError 가 난다.
    if "marketplace" in fields or "marketplace_order_no" in fields:
        new_mk = fields.get("marketplace") or before.marketplace
        new_ono = fields.get("marketplace_order_no") or before.marketplace_order_no
        if (new_mk, new_ono) != (before.marketplace, before.marketplace_order_no):
            fields["line_no"] = _next_line_no(
                db, org_id=org_id, marketplace=new_mk, order_no=new_ono
            )

    set_parts: list[str] = []
    params: dict[str, Any] = {
        "i": str(sale_id),
        "o": str(org_id),
        "now": _now(),
        "actor": str(actor),
    }
    enum_casts = {
        "vat_inclusion_type": "enum_vat_inclusion_type",
    }
    for k, v in fields.items():
        if k in enum_casts and v is not None:
            set_parts.append(f"{k} = CAST(:{k} AS {enum_casts[k]})")
        else:
            set_parts.append(f"{k} = :{k}")
        params[k] = str(v) if isinstance(v, UUID) else v

    set_parts.extend(["updated_at = :now", "updated_by = :actor"])

    db.execute(
        text(f"""
          UPDATE sales_order SET {', '.join(set_parts)}
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        params,
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="sales_order",
        domain_row_id=sale_id,
        action="UPDATE",
        actor_user_id=actor,
        before=before.model_dump(mode="json"),
        after=fields,
    )
    db.commit()
    return get_sale(db, org_id=org_id, sale_id=sale_id)


def delete_sale(db: Session, *, org_id: UUID, actor: UUID, sale_id: UUID) -> None:
    existing = get_sale(db, org_id=org_id, sale_id=sale_id)  # 존재 확인 + NotFoundError 위임
    assert_period_writable(
        db, org_id=org_id, effective_date=existing.sale_date,
        operation="DELETE", what="판매 내역",
    )

    db.execute(
        text("""
          UPDATE sales_order
             SET deleted_at = :now, updated_by = :actor
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "i": str(sale_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="sales_order",
        domain_row_id=sale_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()
