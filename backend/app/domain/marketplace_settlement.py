"""마켓플레이스 정산 레코드 도메인 schemas (docs/22_api_spec.md §5)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MarketplaceSettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    marketplace: str
    settlement_period_start: str  # date as string
    settlement_period_end: str
    marketplace_order_no: str | None
    settled_at: str | None
    gross_revenue: int
    marketplace_fee: int
    ad_cost: int
    return_cost: int
    fulfillment_fee: int
    other_deductions: int
    net_settlement: int
    matched_sales_order_id: UUID | None
    created_at: datetime
    # 이 정산서가 속한 판매자 계정 (M033).
    # **NULL 은 "모른다"** 이지 "계정 없음" 이 아니다.
    integration_account_id: UUID | None = None
    integration_account_label: str | None = None
