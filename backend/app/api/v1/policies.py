"""정책 CRUD 라우터 (CLAUDE.md Generalization Principle, docs/22_api_spec.md §4).

11개 정책 종류 공통 (kind path param 으로 분기):
  marketplace_fee / payment_fee / tax / fulfillment_fee / return_risk /
  ad_cost / target_margin / settlement / partner_share /
  expense_category / evidence_type

라이프사이클:
    DRAFT ──approve──> ACTIVE ──deprecate──> DEPRECATED
                          │
                          └─ effective_from / effective_to + version

endpoints:
  GET  /policies/{kind}                      — 목록 (only_active 필터)
  GET  /policies/{kind}/active               — 특정 기준일 활성 정책 (at=YYYY-MM-DD)
  GET  /policies/{kind}/{id}                 — 상세
  POST /policies/{kind}                      — DRAFT 생성
  PATCH /policies/{kind}/{id}                — DRAFT 수정 (ACTIVE 이후 immutable)
  POST /policies/{kind}/{id}/approve         — DRAFT → ACTIVE
  POST /policies/{kind}/{id}/deprecate       — ACTIVE → DEPRECATED (effective_to set)
  GET  /policies/{kind}/{id}/history         — 버전 이력

권한:
  - 쓰기(_write): ADMIN/OPERATOR
  - 읽기(_read): + ACCOUNTING/READONLY

SQL injection 방지: kind 는 service.policy_crud._TABLES 화이트리스트로 검증."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.policy_crud import (
    PolicyApprove,
    PolicyCreate,
    PolicyDeprecate,
    PolicyKind,
    PolicyUpdate,
)
from app.services.policy_crud import (
    approve_policy,
    create_policy,
    deprecate_policy,
    get_active_policies,
    get_policy,
    list_history,
    list_policies,
    update_policy,
)

router = APIRouter(prefix="/policies", tags=["policies"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR")


@router.get("/{kind}")
def list_(
    kind: PolicyKind,
    only_active: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_policies(
        db, org_id=user.org_id, kind=kind,
        only_active=only_active, limit=limit, offset=offset,
    )
    return envelope(
        [i.model_dump() for i in items],
        meta={"limit": limit, "offset": offset, "total": total, "kind": kind},
    )


@router.get("/{kind}/active")
def active(
    kind: PolicyKind,
    at: date | None = Query(default=None, description="기준일 (기본 = 오늘)"),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items = get_active_policies(db, org_id=user.org_id, kind=kind, at=at)
    return envelope([i.model_dump() for i in items], meta={"kind": kind, "at": str(at or date.today())})


@router.get("/{kind}/{policy_id}")
def detail(
    kind: PolicyKind,
    policy_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(get_policy(db, org_id=user.org_id, kind=kind, policy_id=policy_id).model_dump())


@router.post("/{kind}", status_code=201)
def create(
    kind: PolicyKind,
    body: PolicyCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = create_policy(db, org_id=user.org_id, actor=user.user_id, kind=kind, body=body)
    return envelope(out.model_dump())


@router.patch("/{kind}/{policy_id}")
def update(
    kind: PolicyKind,
    policy_id: UUID,
    body: PolicyUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_policy(
        db, org_id=user.org_id, actor=user.user_id, kind=kind, policy_id=policy_id, body=body,
    )
    return envelope(out.model_dump())


@router.post("/{kind}/{policy_id}/approve")
def approve(
    kind: PolicyKind,
    policy_id: UUID,
    body: PolicyApprove,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = approve_policy(
        db, org_id=user.org_id, actor=user.user_id, kind=kind, policy_id=policy_id, body=body,
    )
    return envelope(out.model_dump())


@router.post("/{kind}/{policy_id}/deprecate")
def deprecate(
    kind: PolicyKind,
    policy_id: UUID,
    body: PolicyDeprecate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = deprecate_policy(
        db, org_id=user.org_id, actor=user.user_id, kind=kind, policy_id=policy_id, body=body,
    )
    return envelope(out.model_dump())


@router.get("/{kind}/{policy_id}/history")
def history(
    kind: PolicyKind,
    policy_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items = list_history(db, org_id=user.org_id, kind=kind, policy_id=policy_id)
    return envelope([i.model_dump() for i in items])
