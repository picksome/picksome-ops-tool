"""상품 소싱 후보 라우터 (docs/22_api_spec.md §4).

endpoints:
  GET    /sourcing               — 상태 필터 + 페이지네이션
  POST   /sourcing               — 등록 (CANDIDATE 상태로 시작)
  PATCH  /sourcing/{id}          — 부분 수정
  POST   /sourcing/{id}/approve  — 승인 (→ APPROVED)
  POST   /sourcing/{id}/reject   — 거절 (→ REJECTED, reason 필수)
  DELETE /sourcing/{id}          — 소프트 삭제

권한:
  read  (_read):  ADMIN / OPERATOR / SOURCING / READONLY
  write (_write): ADMIN / OPERATOR / SOURCING
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.sourcing import RejectRequest, SourcingCandidateCreate, SourcingCandidateUpdate
from app.services.sourcing import (
    approve_sourcing,
    create_sourcing,
    delete_sourcing,
    list_sourcing,
    reject_sourcing,
    update_sourcing,
)

router = APIRouter(prefix="/sourcing", tags=["sourcing"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR", "SOURCING")


@router.get("")
def list_(
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    result = list_sourcing(
        db,
        org_id=user.org_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return envelope(
        [i.model_dump() for i in result["items"]],
        meta={
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
        },
    )


@router.post("", status_code=201)
def create(
    body: SourcingCandidateCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = create_sourcing(db, org_id=user.org_id, actor=user.user_id, body=body)
    return envelope(out.model_dump())


@router.patch("/{item_id}")
def update(
    item_id: UUID,
    body: SourcingCandidateUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_sourcing(db, org_id=user.org_id, actor=user.user_id, item_id=item_id, body=body)
    return envelope(out.model_dump())


@router.post("/{item_id}/approve")
def approve(
    item_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = approve_sourcing(db, org_id=user.org_id, actor=user.user_id, item_id=item_id)
    return envelope(out.model_dump())


@router.post("/{item_id}/reject")
def reject(
    item_id: UUID,
    body: RejectRequest,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = reject_sourcing(
        db, org_id=user.org_id, actor=user.user_id, item_id=item_id, reason=body.reason
    )
    return envelope(out.model_dump())


@router.delete("/{item_id}", status_code=204, response_model=None)
def delete(
    item_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> None:
    delete_sourcing(db, org_id=user.org_id, actor=user.user_id, item_id=item_id)
