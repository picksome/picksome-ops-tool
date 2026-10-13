"""인증 서비스 — 비밀번호/JWT 발급, 초대 토큰 처리.

라우터(api/v1/auth.py)에서 호출. 모든 함수는 audit_log 1행을 남기고 commit.

3 endpoint:
  - authenticate:       email+password → access+refresh
  - refresh_session:    refresh_token → 새 access+refresh
  - accept_invitation:  초대 토큰 + 비밀번호 → 계정 활성화 + 토큰 발급

argon2 자동 rehash: 로그인 성공 시 파라미터가 구버전이면 즉시 새 해시로 교체."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import record_audit
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.domain.auth import TokenPair


def _now() -> datetime:
    return datetime.now(tz=UTC)


def authenticate(db: Session, *, email: str, password: str) -> TokenPair:
    """로그인. 실패 시 동일 메시지 (이메일 vs 비밀번호 구분 노출 금지 — 사용자 열거 방지)."""
    row = db.execute(
        text("""
          SELECT id, org_id, email, password_hash, role, is_active, deleted_at
            FROM app_user WHERE lower(email) = lower(:email)
        """),
        {"email": email},
    ).first()
    if row is None or row.deleted_at is not None:
        raise ForbiddenError("이메일 또는 비밀번호가 올바르지 않습니다.")
    if not row.is_active:
        raise ForbiddenError("비활성 계정입니다.")
    if not verify_password(password, row.password_hash):
        raise ForbiddenError("이메일 또는 비밀번호가 올바르지 않습니다.")

    if needs_rehash(row.password_hash):
        db.execute(
            text("UPDATE app_user SET password_hash = :h WHERE id = :id"),
            {"h": hash_password(password), "id": str(row.id)},
        )

    db.execute(
        text("UPDATE app_user SET last_login_at = :now WHERE id = :id"),
        {"now": _now(), "id": str(row.id)},
    )
    record_audit(
        db,
        org_id=row.org_id,
        domain_table="app_user",
        domain_row_id=row.id,
        action="LOGIN",
        actor_user_id=row.id,
    )
    db.commit()

    settings = get_settings()
    return TokenPair(
        access_token=create_access_token(user_id=row.id, org_id=row.org_id, role=row.role),
        refresh_token=create_refresh_token(user_id=row.id, org_id=row.org_id),
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
    )


def refresh_session(db: Session, *, refresh_token: str) -> TokenPair:
    """refresh_token → 새 access+refresh. role 은 DB 최신 값 사용 (권한 변경 즉시 반영)."""
    try:
        claims = decode_token(refresh_token)
    except ValueError as e:
        raise ForbiddenError(str(e)) from e
    if claims.get("type") != "refresh":
        raise ForbiddenError("리프레시 토큰이 아닙니다.")

    user_id = UUID(claims["sub"])
    org_id = UUID(claims["org_id"])
    row = db.execute(
        text("""
          SELECT role, is_active, deleted_at FROM app_user
            WHERE id = :id AND org_id = :org_id
        """),
        {"id": str(user_id), "org_id": str(org_id)},
    ).first()
    if row is None or row.deleted_at is not None or not row.is_active:
        raise ForbiddenError("계정 사용 불가")

    settings = get_settings()
    return TokenPair(
        access_token=create_access_token(user_id=user_id, org_id=org_id, role=row.role),
        refresh_token=create_refresh_token(user_id=user_id, org_id=org_id),
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
    )


def accept_invitation(
    db: Session, *, invitation_token: str, password: str, display_name: str
) -> TokenPair:
    """초대 수락: invitation_token 검증 → 비밀번호 설정 + is_active=True + 즉시 로그인 토큰 발급."""
    row = db.execute(
        text("""
          SELECT id, org_id, role, invitation_expires_at, deleted_at
            FROM app_user WHERE invitation_token = :tok
        """),
        {"tok": invitation_token},
    ).first()
    if row is None or row.deleted_at is not None:
        raise NotFoundError("초대 토큰이 유효하지 않습니다.")
    if row.invitation_expires_at and row.invitation_expires_at < _now():
        raise ValidationError("초대 토큰이 만료되었습니다.")

    db.execute(
        text("""
          UPDATE app_user
             SET password_hash = :h,
                 display_name = :n,
                 is_active = TRUE,
                 invitation_token = NULL,
                 invitation_expires_at = NULL,
                 last_login_at = :now,
                 updated_at = :now
           WHERE id = :id
        """),
        {
            "h": hash_password(password),
            "n": display_name,
            "now": _now(),
            "id": str(row.id),
        },
    )
    record_audit(
        db,
        org_id=row.org_id,
        domain_table="app_user",
        domain_row_id=row.id,
        action="STATUS_CHANGE",
        actor_user_id=row.id,
        after={"action": "INVITE_ACCEPTED"},
    )
    db.commit()

    settings = get_settings()
    return TokenPair(
        access_token=create_access_token(user_id=row.id, org_id=row.org_id, role=row.role),
        refresh_token=create_refresh_token(user_id=row.id, org_id=row.org_id),
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
    )
