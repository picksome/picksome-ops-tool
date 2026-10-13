"""password_vault_entry 라우터 (CLAUDE.md Security Rules).

저장 원칙: 실제 비밀번호는 저장하지 않고 **보관 위치만** 저장.
  예) label="쿠팡 Wing", vault_location="Bitwarden - 쿠팡 Wing 항목"

endpoints:
  GET    /admin/vault           — 목록 (ADMIN/OPERATOR)
  GET    /admin/vault/{id}      — 상세 (ADMIN/OPERATOR)
  POST   /admin/vault           — 생성 (ADMIN)
  PATCH  /admin/vault/{id}      — 부분 수정 (ADMIN)
  DELETE /admin/vault/{id}      — soft delete (ADMIN)

services.vault 가 _looks_like_password() 패턴 매칭으로 평문 비밀번호 차단:
  → MaskedSecretDetectedError → 422 MASKED_SECRET_DETECTED."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.vault import VaultEntryCreate, VaultEntryUpdate
from app.services.vault import (
    create_vault_entry,
    delete_vault_entry,
    get_vault_entry,
    list_vault_entries,
    update_vault_entry,
)

router = APIRouter(prefix="/admin/vault", tags=["admin.vault"])

_admin = require_roles("ADMIN")
_read = require_roles("ADMIN", "OPERATOR")


@router.get("")
def list_(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_vault_entries(db, org_id=user.org_id, limit=limit, offset=offset)
    return envelope(
        [i.model_dump() for i in items],
        meta={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/{entry_id}")
def detail(
    entry_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(get_vault_entry(db, org_id=user.org_id, entry_id=entry_id).model_dump())


@router.post("", status_code=201)
def create(
    body: VaultEntryCreate,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    out = create_vault_entry(db, org_id=user.org_id, actor=user.user_id, body=body)
    return envelope(out.model_dump())


@router.patch("/{entry_id}")
def update(
    entry_id: UUID,
    body: VaultEntryUpdate,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    out = update_vault_entry(
        db, org_id=user.org_id, actor=user.user_id, entry_id=entry_id, body=body
    )
    return envelope(out.model_dump())


@router.delete("/{entry_id}", status_code=204, response_model=None)
def delete(
    entry_id: UUID,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> None:
    delete_vault_entry(db, org_id=user.org_id, actor=user.user_id, entry_id=entry_id)
