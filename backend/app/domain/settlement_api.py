"""정산 API 요청/응답 schemas (Pydantic).

services/settlement.py 는 dataclass 도메인 (MonthlySettlementInput 등) 을 사용하므로
라우터에서 변환 어댑터 역할.

D-285: reopen_count + reopen_limit_override. 한도 초과 시 REOPEN_LIMIT_EXCEEDED.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CalculateSettlementRequest(BaseModel):
    year_month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}$")


class AdCostAllocationOut(BaseModel):
    year_month: str
    total_marketing_expense: int
    orders_updated: int
    orders_skipped_existing: int


class PartnerDistributionOut(BaseModel):
    partner_id: UUID
    distribution_kind: str
    amount: int
    ratio_used: Decimal | None = None


class SettlementCalcOut(BaseModel):
    year_month: str
    total_revenue: int
    total_direct_cost: int
    total_common_expense: int
    net_profit: int
    carryover_offset_applied: int
    distributable_amount: int
    distributions: list[PartnerDistributionOut]
    warnings: list[str]


class SettlementCreateRequest(BaseModel):
    year_month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}$")
    memo: str | None = None


class SettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    business_key: str
    year_month: str
    status: str
    total_revenue: int
    total_direct_cost: int
    total_common_expense: int
    net_profit: int
    carryover_offset: int
    distributable_amount: int
    priority_distribution_total: int
    ratio_distribution_total: int
    reopen_count: int
    reopen_limit_override: int | None
    confirmed_at: datetime | None
    confirmed_by: UUID | None
    locked_at: datetime | None
    locked_by: UUID | None
    snapshot_id: UUID | None
    memo: str | None
    created_at: datetime


class ReopenRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ConfirmRequest(BaseModel):
    note: str | None = None
    # 마감 검증의 경고를 "보고 넘겼다" 는 기록. 코드를 명시하지 않으면 확정이 거부된다.
    acknowledged_warnings: list[str] = Field(default_factory=list)
