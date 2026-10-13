"""사용자 관리 라우터 (ADMIN 전용).

endpoints (모두 require_roles("ADMIN")):
  GET    /admin/users           — 목록
  POST   /admin/users           — 즉시 활성 생성 (임시 비밀번호 meta 로 1회 노출)
  GET    /admin/users/{id}      — 상세
  PATCH  /admin/users/{id}      — 부분 수정 (role 변경 시 audit=PERMISSION_CHANGE)
  DELETE /admin/users/{id}      — soft delete (is_active=False)
  POST   /admin/users/invite    — 초대 이메일 (invitation_token 7일 만료, meta 로 1회 노출)
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.user import UserCreate, UserInvite, UserUpdate
from app.services.user import (
    create_user,
    deactivate_user,
    get_user,
    invite_user,
    list_users,
    update_user,
)

router = APIRouter(prefix="/admin/users", tags=["admin.users"])

_admin = require_roles("ADMIN")


@router.get("")
def list_(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    items = list_users(db, org_id=user.org_id, limit=limit, offset=offset)
    return envelope([u.model_dump() for u in items], meta={"limit": limit, "offset": offset})


@router.post("", status_code=201)
def create(
    body: UserCreate,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    temp_pw = secrets.token_urlsafe(12)
    out = create_user(db, org_id=user.org_id, actor=user.user_id, body=body, temporary_password=temp_pw)
    return envelope(out.model_dump(), meta={"temporary_password": temp_pw})


@router.get("/{user_id}")
def detail(
    user_id: UUID,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(get_user(db, org_id=user.org_id, user_id=user_id).model_dump())


@router.patch("/{user_id}")
def update(
    user_id: UUID,
    body: UserUpdate,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    out = update_user(db, org_id=user.org_id, actor=user.user_id, user_id=user_id, body=body)
    return envelope(out.model_dump())


@router.delete("/{user_id}", status_code=204, response_model=None)
def delete(
    user_id: UUID,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> None:
    deactivate_user(db, org_id=user.org_id, actor=user.user_id, user_id=user_id)


@router.post("/invite", status_code=201)
def invite(
    body: UserInvite,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    out, token = invite_user(db, org_id=user.org_id, actor=user.user_id, body=body)
    return envelope(out.model_dump(), meta={"invitation_token": token})
