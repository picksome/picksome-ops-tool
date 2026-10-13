"""마켓플레이스 정산 레코드 서비스 (docs/22_api_spec.md §5).

읽기 전용 서비스 — 정산 레코드는 import_batch 를 통해 삽입되며
직접 CRUD 는 허용하지 않는다.

multi-tenant 격리: 모든 쿼리에 org_id = :o 조건 필수.
soft-delete: deleted_at IS NULL 조건으로 논리 삭제 구분.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.domain.marketplace_settlement import MarketplaceSettlementOut

# 계정 라벨은 `expenses`·`sales_order` 와 같은 평면 관례로 붙인다.
# LEFT JOIN 이다 — legacy 정산 레코드는 계정이 NULL 이고, INNER 로 바꾸면
# 그 레코드들이 목록에서 통째로 사라진다.
_SELECT = """
  SELECT msr.id,
         msr.marketplace::text AS marketplace,
         msr.settlement_period_start::text AS settlement_period_start,
         msr.settlement_period_end::text AS settlement_period_end,
         msr.marketplace_order_no,
         msr.settled_at::text AS settled_at,
         msr.gross_revenue, msr.marketplace_fee, msr.ad_cost,
         msr.return_cost, msr.fulfillment_fee, msr.other_deductions,
         msr.net_settlement,
         msr.matched_sales_order_id,
         msr.created_at,
         msr.integration_account_id,
         ia.account_label AS integration_account_label
    FROM marketplace_settlement_record msr
    LEFT JOIN integration_account ia ON ia.id = msr.integration_account_id
"""


def _row_to_out(row: Any) -> MarketplaceSettlementOut:
    return MarketplaceSettlementOut(**row._mapping)


def list_marketplace_settlements(
    db: Session,
    *,
    org_id: UUID,
    start_date: str | None = None,
    end_date: str | None = None,
    marketplace: str | None = None,
    integration_account_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    # ⚠️ `msr.` 로 한정한다. `_SELECT` 가 `integration_account` 를 join 하는데
    # 그 테이블에도 `org_id`·`deleted_at`·`created_at` 이 있다 (M032 에서 겪은 것과
    # 같은 문제 — 한정하지 않으면 "column reference is ambiguous" 로 조회가 죽는다).
    where = ["msr.org_id = :o", "msr.deleted_at IS NULL"]
    params: dict[str, Any] = {"o": str(org_id)}

    if start_date:
        where.append("msr.settlement_period_start >= :start_date")
        params["start_date"] = start_date
    if end_date:
        where.append("msr.settlement_period_end <= :end_date")
        params["end_date"] = end_date
    if marketplace:
        where.append("msr.marketplace = :marketplace")
        params["marketplace"] = marketplace
    if integration_account_id:
        # 같은 판매처에 계정이 여럿일 때 스토어별 정산서를 나눠 본다.
        where.append("msr.integration_account_id = :account_id")
        params["account_id"] = str(integration_account_id)

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM marketplace_settlement_record msr WHERE {where_sql}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(f"""
          {_SELECT}
           WHERE {where_sql}
           ORDER BY msr.settlement_period_start DESC, msr.created_at DESC
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


def get_marketplace_settlement(
    db: Session, *, org_id: UUID, record_id: UUID
) -> MarketplaceSettlementOut:
    row = db.execute(
        text(f"{_SELECT} WHERE msr.id = :i AND msr.org_id = :o AND msr.deleted_at IS NULL"),
        {"i": str(record_id), "o": str(org_id)},
    ).first()
    if row is None:
        raise NotFoundError("마켓플레이스 정산 레코드를 찾을 수 없습니다.")
    return _row_to_out(row)
