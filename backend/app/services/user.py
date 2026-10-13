"""사용자 CRUD + 초대 서비스.

- ADMIN 전용 (라우터 가드: require_roles("ADMIN")).
- EXTERNAL_ACCOUNTANT 라우트는 FEATURE_EXTERNAL_ACCOUNTANT (D-282 Post-MVP) — 라우터에서 가드.

흐름:
  create_user:  즉시 활성 + 임시 비밀번호 (관리자가 직접 전달)
  invite_user:  비활성 + invitation_token (7일 만료) → 사용자가 accept_invitation 으로 활성화
  update_user:  role 변경 시 audit action = PERMISSION_CHANGE
  deactivate_user: soft delete (is_active=FALSE + deleted_at)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.security import hash_password, new_invitation_token
from app.domain.user import UserCreate, UserInvite, UserOut, UserUpdate


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _row_to_out(row: Any) -> UserOut:
    d = dict(row._mapping)
    d["invitation_pending"] = d.pop("invitation_token") is not None
    return UserOut(**d)


_SELECT = """
  SELECT id, email, display_name, role, is_active, last_login_at,
         invitation_token, created_at
    FROM app_user
"""


def list_users(db: Session, *, org_id: UUID, limit: int = 50, offset: int = 0) -> list[UserOut]:
    rows = db.execute(
        text(f"""
          {_SELECT}
           WHERE org_id = :org_id AND deleted_at IS NULL
           ORDER BY created_at DESC
           LIMIT :limit OFFSET :offset
        """),
        {"org_id": str(org_id), "limit": limit, "offset": offset},
    ).all()
    return [_row_to_out(r) for r in rows]


def get_user(db: Session, *, org_id: UUID, user_id: UUID) -> UserOut:
    row = db.execute(
        text(f"{_SELECT} WHERE org_id = :o AND id = :u AND deleted_at IS NULL"),
        {"o": str(org_id), "u": str(user_id)},
    ).first()
    if row is None:
        raise NotFoundError("사용자를 찾을 수 없습니다.")
    return _row_to_out(row)


def create_user(
    db: Session, *, org_id: UUID, actor: UUID, body: UserCreate, temporary_password: str
) -> UserOut:
    if temporary_password.startswith("plain:"):
        raise ValidationError("임시 비밀번호 형식 오류")
    new_user_id = new_id()
    db.execute(
        text("""
          INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
          VALUES (:id, :org, :email, :pwh, :dn, :role, TRUE)
        """),
        {
            "id": str(new_user_id),
            "org": str(org_id),
            "email": str(body.email),
            "pwh": hash_password(temporary_password),
            "dn": body.display_name,
            "role": body.role,
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="app_user",
        domain_row_id=new_user_id,
        action="CREATE",
        actor_user_id=actor,
        after={"email": str(body.email), "role": body.role},
    )
    db.commit()
    return get_user(db, org_id=org_id, user_id=new_user_id)


def invite_user(
    db: Session, *, org_id: UUID, actor: UUID, body: UserInvite
) -> tuple[UserOut, str]:
    """초대 이메일 발송용. 반환 token 은 한 번만 노출 (이메일 본문에만 포함, DB 저장은 raw).

    password_hash 는 placeholder — accept_invitation 에서 실제 해시로 교체.
    """
    new_user_id = new_id()
    token = new_invitation_token()
    expires = _now() + timedelta(days=7)
    db.execute(
        text("""
          INSERT INTO app_user (
            id, org_id, email, password_hash, display_name, role,
            is_active, invited_by, invitation_token, invitation_expires_at
          ) VALUES (
            :id, :org, :email, :pwh, :dn, :role, FALSE, :inv,
            :tok, :exp
          )
        """),
        {
            "id": str(new_user_id),
            "org": str(org_id),
            "email": str(body.email),
            "pwh": "$argon2id$placeholder$invite-pending",
            "dn": body.display_name,
            "role": body.role,
            "inv": str(actor),
            "tok": token,
            "exp": expires,
        },
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="app_user",
        domain_row_id=new_user_id,
        action="CREATE",
        actor_user_id=actor,
        after={"email": str(body.email), "role": body.role, "invited": True},
    )
    db.commit()
    return get_user(db, org_id=org_id, user_id=new_user_id), token


def update_user(
    db: Session, *, org_id: UUID, actor: UUID, user_id: UUID, body: UserUpdate
) -> UserOut:
    before = get_user(db, org_id=org_id, user_id=user_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return before

    sets = ", ".join([f"{k} = :{k}" for k in fields])
    params = {**fields, "u": str(user_id), "o": str(org_id), "now": _now()}
    db.execute(
        text(f"""
          UPDATE app_user SET {sets}, updated_at = :now, updated_by = :actor
            WHERE id = :u AND org_id = :o
        """),
        {**params, "actor": str(actor)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="app_user",
        domain_row_id=user_id,
        # role 변경은 권한 변경으로 별도 분류 — 보안 감사 시 필터 키.
        action="UPDATE" if "role" not in fields else "PERMISSION_CHANGE",
        actor_user_id=actor,
        before=before.model_dump(),
        after=fields,
    )
    db.commit()
    return get_user(db, org_id=org_id, user_id=user_id)


def deactivate_user(db: Session, *, org_id: UUID, actor: UUID, user_id: UUID) -> None:
    db.execute(
        text("""
          UPDATE app_user
             SET is_active = FALSE, deleted_at = :now, deleted_by = :actor
           WHERE id = :u AND org_id = :o AND deleted_at IS NULL
        """),
        {"now": _now(), "actor": str(actor), "u": str(user_id), "o": str(org_id)},
    )
    record_audit(
        db,
        org_id=org_id,
        domain_table="app_user",
        domain_row_id=user_id,
        action="DELETE",
        actor_user_id=actor,
    )
    db.commit()
