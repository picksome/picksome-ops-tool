"""정책 CRUD 도메인 schemas.

11개 정책 종류 공통 패턴 (CLAUDE.md Generalization Principle 박제 원칙):
- 공통 메타: version / effective_from / effective_to / source_* / reliability_status / memo / is_active
- 종류별 컬럼: kind 별 ALLOWED_COLUMNS 화이트리스트 (services/policy_crud.py).

수명주기:
1. POST  /policies      → PolicyCreate (DRAFT)
2. POST  /policies/{}/approve → approved_by/at 기록, is_active=True
3. POST  /policies/{}/deprecate → effective_to 종료 + 사유 기록 (소프트 삭제)

PolicyHistoryEntry: policy_history 테이블 — 모든 변경 before/after 박제.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PolicyKind = Literal[
    "marketplace_fee",
    "payment_fee",
    "tax",
    "fulfillment",
    "return_risk",
    "ad_cost",
    "target_margin",
    "settlement",
    "partner_share",
    "expense_category",
    "evidence_type",
]


class PolicyCreate(BaseModel):
    # 공통 메타
    version: int = Field(ge=1)
    effective_from: date
    effective_to: date | None = None
    source_type: str = Field(default="MANUAL")
    source_url: str | None = None
    reliability_status: str = Field(default="CONFIRMED")
    memo: str | None = None
    # 종류별 컬럼 (free-form)
    fields: dict[str, Any] = Field(default_factory=dict)


class PolicyUpdate(BaseModel):
    effective_from: date | None = None
    effective_to: date | None = None
    source_type: str | None = None
    source_url: str | None = None
    reliability_status: str | None = None
    memo: str | None = None
    fields: dict[str, Any] | None = None


class PolicyApprove(BaseModel):
    note: str | None = None


class PolicyDeprecate(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    effective_to: date | None = None  # default = today


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    effective_from: date
    effective_to: date | None
    source_type: str
    source_url: str | None
    reliability_status: str
    memo: str | None
    is_active: bool
    approved_by: UUID | None
    approved_at: datetime | None
    created_at: datetime
    fields: dict[str, Any]


class PolicyHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    change_action: str
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    reason: str | None
    changed_by: UUID
    changed_at: datetime
