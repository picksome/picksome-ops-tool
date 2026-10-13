"""사용자 도메인 schemas.

docs/22_api_spec.md §2-1 (사용자 관리). role 은 RBAC 6종 (core/rbac.ROLES).
UserCreate vs UserInvite: 전자는 직접 생성(admin), 후자는 이메일 초대(invitation_pending)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=80)
    role: str = Field(pattern=r"^(ADMIN|OPERATOR|SOURCING|ACCOUNTING|READONLY|EXTERNAL_ACCOUNTANT)$")


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: str | None = Field(
        default=None,
        pattern=r"^(ADMIN|OPERATOR|SOURCING|ACCOUNTING|READONLY|EXTERNAL_ACCOUNTANT)$",
    )
    is_active: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    invitation_pending: bool
    created_at: datetime


class UserInvite(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=80)
    role: str = Field(pattern=r"^(ADMIN|OPERATOR|SOURCING|ACCOUNTING|READONLY|EXTERNAL_ACCOUNTANT)$")
