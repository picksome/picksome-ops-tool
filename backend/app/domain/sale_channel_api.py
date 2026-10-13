"""판매채널(sale_channel) API schemas (M007, docs/27 D-287)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class SaleChannelCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40, pattern=r"^[A-Z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=100)


class SaleChannelOut(BaseModel):
    id: UUID
    org_id: UUID | None
    code: str
    display_name: str
    is_system_default: bool
    integration_kind: str
    is_active: bool
