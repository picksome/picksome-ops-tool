"""password_vault_entry 도메인 schemas.

CLAUDE.md Security Rules: 실제 비밀번호 절대 저장 금지, 보관 위치만 기록.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class VaultEntryCreate(BaseModel):
    service_name: str = Field(min_length=1, max_length=120)
    vault_provider: str = Field(min_length=1, max_length=60)
    vault_item_name: str = Field(min_length=1, max_length=200)
    account_masked: str | None = Field(default=None, max_length=200)
    memo: str | None = Field(default=None, max_length=1000)


class VaultEntryUpdate(BaseModel):
    service_name: str | None = Field(default=None, min_length=1, max_length=120)
    vault_provider: str | None = Field(default=None, min_length=1, max_length=60)
    vault_item_name: str | None = Field(default=None, min_length=1, max_length=200)
    account_masked: str | None = Field(default=None, max_length=200)
    memo: str | None = Field(default=None, max_length=1000)


class VaultEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_name: str
    vault_provider: str
    vault_item_name: str
    account_masked: str | None
    memo: str | None
    created_at: datetime
    updated_at: datetime | None = None
