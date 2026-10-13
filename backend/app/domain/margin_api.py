"""마진 API 요청/응답 schemas (Pydantic).

services/margin.py 는 dataclass 도메인 (ExpectedMarginInput 등) 을 사용하므로
라우터에서 변환 어댑터 역할.

save=True 시:
- product_margin_expected INSERT (snapshot_id 박제)
- audit_log CREATE 1행 기록
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AppliedPolicyOut(BaseModel):
    """계산에 사용된 정책 출처 — FE PolicyAutoField 툴팁에 표시."""

    table: str
    version: int
    effective_from: date
    reliability_status: str
    source_type: str
    value: int = 0  # FE 미참조, placeholder


class CalculateExpectedRequest(BaseModel):
    product_id: UUID | None = None  # save=True 시에만 필수 (라우터에서 검증)
    product_variant_id: UUID | None = None
    margin_type: str = Field(pattern=r"^(PURCHASE_AGENCY|ROCKET_GROWTH|OVERSEAS_DIRECT|GENERAL_RESELL)$")
    marketplace: str
    cost_amount: int = Field(ge=0)
    sale_price: int = Field(ge=0)
    payment_method: str = "BUSINESS_CARD"
    extras: dict[str, Any] = Field(default_factory=dict)
    override_fields: dict[str, Any] | None = None
    save: bool = False  # True 시 product_margin_expected INSERT


class ExpectedMarginOut(BaseModel):
    marketplace_fee: int
    payment_fee: int
    ad_cost: int
    fulfillment_fee: int
    return_risk_cost: int
    vat: int
    margin_amount: int
    margin_rate: Decimal
    applied_policies: dict[str, AppliedPolicyOut] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    saved_id: UUID | None = None


class SimulateRequest(BaseModel):
    margin_type: str = Field(pattern=r"^(PURCHASE_AGENCY|ROCKET_GROWTH|OVERSEAS_DIRECT|GENERAL_RESELL)$")
    marketplace: str
    cost_amount: int = Field(ge=0)
    extras: dict[str, Any] = Field(default_factory=dict)
    payment_method: str = "BUSINESS_CARD"
    target_rates: list[Decimal] = Field(min_length=1, max_length=20)


class SimulateRow(BaseModel):
    target_rate: Decimal
    recommended_price: int
    expected_margin: ExpectedMarginOut


class CalculateActualRequest(BaseModel):
    sales_order_id: UUID
    settlement_record_id: UUID | None = None
    save: bool = False  # True 시 product_margin_actual UPSERT


class ActualMarginOut(BaseModel):
    revenue: int
    cost: int
    marketplace_fee: int
    payment_fee: int
    ad_cost: int
    fulfillment_fee: int
    return_cost: int
    vat: int
    margin_amount: int
    margin_rate: Decimal
    matched_expected_id: UUID | None
    delta_margin_amount: int | None
    delta_margin_rate: Decimal | None
    warnings: list[str] = Field(default_factory=list)
    saved_id: UUID | None = None


class ExpectedRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    product_variant_id: UUID | None
    margin_type: str
    marketplace: str
    input_cost: int
    input_sale_price: int
    computed_margin_amount: int
    computed_margin_rate: Decimal
    valid_from: Any  # date
    snapshot_id: UUID
    created_at: datetime
