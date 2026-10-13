"""인증 라우터 (docs/22_api_spec.md §2).

endpoints:
  POST /auth/login          — 이메일+비밀번호 → access/refresh
  POST /auth/refresh        — refresh token → 새 access/refresh
  POST /auth/logout         — audit LOGOUT 기록 (서버 측 token 만료는 없음, JWT stateless)
  POST /auth/invite/accept  — 초대 토큰 + 비밀번호 → 계정 활성화 + 토큰 발급
  GET  /auth/me             — 현재 사용자 정보

모든 mutation 은 services/auth.py 가 audit_log 기록 + commit 책임."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.response import envelope
from app.deps import CurrentUser, get_current_user, get_db
from app.domain.auth import (
    CurrentUserOut,
    InviteAcceptRequest,
    LoginRequest,
    RefreshRequest,
    TokenPair,
)
from app.services.auth import accept_invitation, authenticate, refresh_session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)) -> dict:
    tokens: TokenPair = authenticate(db, email=body.email, password=body.password)
    return envelope(tokens.model_dump())


@router.post("/refresh")
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> dict:
    tokens = refresh_session(db, refresh_token=body.refresh_token)
    return envelope(tokens.model_dump())


@router.post("/logout")
def logout(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="app_user",
        domain_row_id=user.user_id,
        action="LOGOUT",
        actor_user_id=user.user_id,
    )
    db.commit()
    return envelope({"logged_out": True})


@router.post("/invite/accept")
def invite_accept(body: InviteAcceptRequest, db: Session = Depends(get_db)) -> dict:
    tokens = accept_invitation(
        db,
        invitation_token=body.invitation_token,
        password=body.password,
        display_name=body.display_name,
    )
    return envelope(tokens.model_dump())


@router.get("/me")
def me(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    from sqlalchemy import text

    row = db.execute(
        text("""
          SELECT id, org_id, email, display_name, role, last_login_at
            FROM app_user WHERE id = :id
        """),
        {"id": str(user.user_id)},
    ).first()
    out = CurrentUserOut(**row._mapping)
    return envelope(out.model_dump())
