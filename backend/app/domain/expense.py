"""사업비용 도메인 schemas (D-284).

vat_inclusion_type 분기:
- INCLUDED:  gross = supply + vat, 자동 분리 vat = gross * r / (1+r)
- EXCLUDED:  사용자가 supply 입력, vat = supply * r
- EXEMPT:    vat = 0, supply = gross

is_manual_tax_override = TRUE 면 자동 분리 skip — 사용자 입력값 그대로.
tax_policy_id 는 effective tax_policy 의 id snapshot (계산 시점 박제).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExpenseCreate(BaseModel):
    expense_date: date
    category_id: UUID
    description: str = Field(min_length=1, max_length=400)

    gross_amount: int = Field(ge=0)
    supply_amount: int | None = Field(default=None, ge=0)
    vat_amount: int | None = Field(default=None, ge=0)
    vat_inclusion_type: str = Field(default="INCLUDED", pattern=r"^(INCLUDED|EXCLUDED|EXEMPT)$")
    tax_type: str = Field(default="GENERAL")
    is_manual_tax_override: bool = False

    payment_method: str
    payment_card_holder_id: UUID | None = None
    is_personal_card: bool = False
    is_business_use: bool = True

    evidence_type: str = Field(default="OTHER")
    evidence_status: str = Field(default="NONE")

    is_tax_deductible: bool = True
    is_settlement_deductible: bool = True
    attributable_partner_id: UUID | None = None
    supplier_id: UUID | None = None
    memo: str | None = None


class ExpenseUpdate(BaseModel):
    expense_date: date | None = None
    category_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=400)
    gross_amount: int | None = Field(default=None, ge=0)
    supply_amount: int | None = Field(default=None, ge=0)
    vat_amount: int | None = Field(default=None, ge=0)
    vat_inclusion_type: str | None = Field(default=None, pattern=r"^(INCLUDED|EXCLUDED|EXEMPT)$")
    tax_type: str | None = None
    is_manual_tax_override: bool | None = None
    payment_method: str | None = None
    payment_card_holder_id: UUID | None = None
    is_personal_card: bool | None = None
    is_business_use: bool | None = None
    evidence_type: str | None = None
    evidence_status: str | None = None
    is_tax_deductible: bool | None = None
    is_settlement_deductible: bool | None = None
    attributable_partner_id: UUID | None = None
    supplier_id: UUID | None = None
    memo: str | None = None


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    expense_date: date
    category_id: UUID
    description: str
    gross_amount: int
    supply_amount: int
    vat_amount: int
    vat_inclusion_type: str
    tax_type: str
    tax_policy_id: UUID | None
    is_manual_tax_override: bool
    payment_method: str
    payment_card_holder_id: UUID | None
    is_personal_card: bool
    is_business_use: bool
    evidence_type: str
    evidence_status: str
    is_tax_deductible: bool
    is_settlement_deductible: bool
    attributable_partner_id: UUID | None
    supplier_id: UUID | None
    memo: str | None
    linked_settlement_id: UUID | None
    created_at: datetime

    # 조인 필드 — 화면 표시용. 참조 대상이 지워졌으면 None 이다.
    # **저장 입력이 아니다.** 이름으로 수정 요청을 받으면 동명이인에서 갈린다.
    category_name: str | None = None
    card_holder_name: str | None = None
    attributable_partner_name: str | None = None
    supplier_name: str | None = None


class ExpenseAdjustmentCreate(BaseModel):
    """잠긴 달 비용 정정 조정.

    금액은 **차액**이다. 원본을 늘리면 양수, 되돌리면 음수.
    원본 금액을 그대로 넣으면 두 배가 된다.
    """

    adjustment_type: str = Field(pattern=r"^(LATE_ENTRY|CORRECTION|REVERSAL|RECLASSIFICATION)$")
    adjustment_reason: str = Field(min_length=1, max_length=400)
    adjustment_date: date
    amount_delta: int
    # 재분류처럼 카테고리를 바꾸는 경우에만 쓴다. 나머지는 원본을 따라간다.
    payload: dict[str, Any] | None = None
