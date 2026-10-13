"""인증 입출력 Pydantic 모델.

docs/22_api_spec.md §2 (인증) — POST /auth/login, /auth/refresh, /auth/invite/accept.
비밀번호 정책: 최소 8자 (services/security.hash_password 와 동일 기준).
초대 토큰: 16자 이상 — security.new_invitation_token() 이 32자 발급."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class InviteAcceptRequest(BaseModel):
    invitation_token: str = Field(min_length=16)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)


class CurrentUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    email: EmailStr
    display_name: str
    role: str
    last_login_at: datetime | None
