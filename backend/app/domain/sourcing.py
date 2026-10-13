"""상품 소싱 후보 도메인 schemas (docs/22_api_spec.md §4)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourcingCandidateCreate(BaseModel):
    supplier_url: str = Field(min_length=1)
    supplier_name_snapshot: str | None = None
    supply_price: int | None = Field(default=None, ge=0)
    moq: int | None = Field(default=None, ge=1)
    category_path: str | None = None
    memo: str | None = None
    product_id: UUID | None = None


class SourcingCandidateUpdate(BaseModel):
    supplier_url: str | None = None
    supplier_name_snapshot: str | None = None
    supply_price: int | None = None
    moq: int | None = None
    category_path: str | None = None
    memo: str | None = None
    product_id: UUID | None = None


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1)


class SourcingCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID | None
    supplier_url: str
    supplier_name_snapshot: str | None
    supply_price: int | None
    moq: int | None
    category_path: str | None
    candidate_status: str
    rejected_reason: str | None
    memo: str | None
    created_at: datetime
