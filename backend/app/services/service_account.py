"""서비스 계정 · API 토큰 (M044).

사람 계정과 무엇이 다른가
---------------------
```
app_user          로그인한다 · role 이 있다 · 세션이 만료된다
service_account   로그인하지 않는다 · role 이 없다 · 토큰 scope 만 있다
```

role 을 주지 않은 것이 핵심이다. "ADMIN 서비스 계정" 을 만들 수 있게 하면
사람 계정과 같아지고, AI 에이전트가 조직의 모든 것을 할 수 있게 된다.
할 수 있는 일은 **토큰마다** scope 로 정한다.

토큰 원문은 한 번만 존재한다
------------------------
```
발급 → 응답에 원문 1회 → 서버에는 해시만
```

다시 보여주는 경로를 만들지 않았다. 잊었으면 새로 발급하고 옛 것을 폐기한다 —
되읽기 경로가 있으면 그게 가장 약한 고리가 된다(자격증명과 같은 판단, docs/60).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.api_token import (
    InvalidScopeError,
    generate_token,
    hash_token,
    token_matches,
    validate_scopes,
)
from app.core.audit import record_audit
from app.core.exceptions import ConflictError, DomainError, NotFoundError, ValidationError


class TokenAuthError(DomainError):
    """토큰이 없거나 유효하지 않다.

    **이유를 자세히 알려주지 않는다.** "만료됨" 과 "없는 토큰" 을 구분해 주면
    유효한 토큰을 찾는 데 쓸 수 있다. 로그에는 남기고 응답은 하나로 한다.
    """

    code = "INVALID_TOKEN"
    http_status = 401


class ScopeDeniedError(DomainError):
    """토큰에 그 권한이 없다.

    401 이 아니라 **403** 이다 — 인증은 됐고 권한이 없는 것이다. 둘을 섞으면
    클라이언트가 토큰을 다시 발급받으려 한다.
    """

    code = "SCOPE_DENIED"
    http_status = 403


# ──────────────────────────────────────────────────────────────────────────
# 서비스 계정
# ──────────────────────────────────────────────────────────────────────────

def create_account(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    name: str, description: str | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise ValidationError("서비스 계정 이름이 필요합니다.")

    exists = db.execute(text("""
        SELECT 1 FROM service_account
         WHERE org_id = :o AND name = :n AND deleted_at IS NULL
    """), {"o": str(org_id), "n": name.strip()}).first()
    if exists:
        raise ConflictError(f"이미 있는 서비스 계정 이름입니다: {name}")

    sa_id = db.execute(text("""
        INSERT INTO service_account (org_id, name, description, created_by, updated_by)
        VALUES (:o, :n, :d, :a, :a) RETURNING id
    """), {"o": str(org_id), "n": name.strip(), "d": description,
           "a": str(actor_user_id)}).scalar_one()

    record_audit(
        db, org_id=org_id, domain_table="service_account",
        domain_row_id=UUID(str(sa_id)), action="CREATE", actor_user_id=actor_user_id,
        after={"name": name.strip()},
    )
    db.commit()
    return get_account(db, org_id=org_id, account_id=UUID(str(sa_id)))


def get_account(db: Session, *, org_id: UUID, account_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT id, name, description, status, last_used_at, created_at
          FROM service_account
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(account_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("서비스 계정을 찾을 수 없습니다.")

    d = dict(row)
    d["id"] = str(d["id"])
    d["tokens"] = list_tokens(db, org_id=org_id, account_id=account_id)
    # 살아 있는 토큰이 없으면 이 계정은 아무것도 할 수 없다 — 화면이 말해야 한다.
    d["active_token_count"] = sum(1 for t in d["tokens"] if t["active"])
    return d


def list_accounts(db: Session, *, org_id: UUID) -> list[dict[str, Any]]:
    ids = db.execute(text("""
        SELECT id FROM service_account
         WHERE org_id = :o AND deleted_at IS NULL ORDER BY created_at
    """), {"o": str(org_id)}).scalars().all()
    return [get_account(db, org_id=org_id, account_id=UUID(str(i))) for i in ids]


def set_account_status(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID, status: str,
) -> dict[str, Any]:
    """계정을 끄면 **토큰이 살아 있어도** 인증되지 않는다.

    토큰을 하나씩 폐기하는 것보다 빠른 차단 수단이 필요하다 — 에이전트가
    이상하게 동작할 때 먼저 멈추고 나서 원인을 본다.
    """
    if status not in ("ACTIVE", "DISABLED"):
        raise ValidationError(f"알 수 없는 상태입니다: {status}")
    before = get_account(db, org_id=org_id, account_id=account_id)

    db.execute(text("""
        UPDATE service_account SET status = :s, updated_at = now(), updated_by = :a
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"s": status, "i": str(account_id), "o": str(org_id),
           "a": str(actor_user_id)})
    record_audit(
        db, org_id=org_id, domain_table="service_account", domain_row_id=account_id,
        action="STATUS_CHANGE", actor_user_id=actor_user_id,
        before={"status": before["status"]}, after={"status": status},
    )
    db.commit()
    return get_account(db, org_id=org_id, account_id=account_id)


# ──────────────────────────────────────────────────────────────────────────
# 토큰
# ──────────────────────────────────────────────────────────────────────────

def issue_token(
    db: Session, *, org_id: UUID, actor_user_id: UUID, account_id: UUID,
    scopes: list[str], label: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """토큰 발급. **원문은 이 반환값에만 있다.**

    호출자는 사용자에게 한 번 보여주고 버린다. 감사에도 남기지 않는다 —
    접두사와 권한 목록만 남는다.
    """
    get_account(db, org_id=org_id, account_id=account_id)
    try:
        clean_scopes = validate_scopes(scopes)
    except InvalidScopeError as exc:
        raise ValidationError(str(exc)) from exc

    token, prefix, digest = generate_token()
    token_id = db.execute(text("""
        INSERT INTO api_token
               (org_id, service_account_id, token_prefix, token_hash,
                label, scopes, expires_at, created_by)
        VALUES (:o, :sa, :p, :h, :l, :sc, :e, :a) RETURNING id
    """), {"o": str(org_id), "sa": str(account_id), "p": prefix, "h": digest,
           "l": label, "sc": clean_scopes, "e": expires_at,
           "a": str(actor_user_id)}).scalar_one()

    record_audit(
        db, org_id=org_id, domain_table="api_token", domain_row_id=UUID(str(token_id)),
        action="CREATE", actor_user_id=actor_user_id,
        # 원문도 해시도 남기지 않는다. 무엇을 줬는지만.
        after={"token_prefix": prefix, "scopes": clean_scopes,
               "expires_at": expires_at.isoformat() if expires_at else None},
        reason="API_TOKEN_ISSUED",
    )
    db.commit()

    return {
        "id": str(token_id),
        "token_prefix": prefix,
        "scopes": clean_scopes,
        "label": label,
        "expires_at": expires_at.isoformat() if expires_at else None,
        # ⚠️ 지금 이 순간에만 존재한다.
        "token": token,
        "warning": "이 값은 다시 볼 수 없습니다. 지금 안전한 곳에 옮기세요.",
    }


def list_tokens(
    db: Session, *, org_id: UUID, account_id: UUID,
) -> list[dict[str, Any]]:
    """토큰 목록. **해시도 원문도 나가지 않는다.**"""
    rows = db.execute(text("""
        SELECT id, token_prefix, label, scopes, created_at,
               expires_at, last_used_at, revoked_at
          FROM api_token
         WHERE org_id = :o AND service_account_id = :sa
         ORDER BY created_at DESC
    """), {"o": str(org_id), "sa": str(account_id)}).mappings().all()

    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["scopes"] = list(d["scopes"] or [])
        # 살아 있는가 = 폐기 안 됨 + 만료 안 됨. 화면이 둘을 구분할 수 있게
        # 원인도 함께 준다.
        d["revoked"] = d["revoked_at"] is not None
        d["expired"] = (
            d["expires_at"] is not None
            and d["expires_at"] < datetime.now(d["expires_at"].tzinfo)
        )
        d["active"] = not d["revoked"] and not d["expired"]
        out.append(d)
    return out


def revoke_token(
    db: Session, *, org_id: UUID, actor_user_id: UUID, token_id: UUID,
) -> dict[str, Any]:
    """즉시 사용 불가. 행은 지우지 않는다 — 누가 언제 무엇을 했는지가 남아야 한다."""
    row = db.execute(text("""
        SELECT id, service_account_id, revoked_at FROM api_token
         WHERE id = :i AND org_id = :o
    """), {"i": str(token_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("토큰을 찾을 수 없습니다.")

    if row["revoked_at"] is None:
        db.execute(text("""
            UPDATE api_token SET revoked_at = now(), revoked_by = :a
             WHERE id = :i AND org_id = :o
        """), {"i": str(token_id), "o": str(org_id), "a": str(actor_user_id)})
        record_audit(
            db, org_id=org_id, domain_table="api_token", domain_row_id=token_id,
            action="DELETE", actor_user_id=actor_user_id, reason="API_TOKEN_REVOKED",
        )
        db.commit()

    return {"id": str(token_id), "revoked": True}


# ──────────────────────────────────────────────────────────────────────────
# 인증
# ──────────────────────────────────────────────────────────────────────────

class TokenPrincipal:
    """토큰으로 인증된 주체.

    `CurrentUser` 와 **다른 타입**이다. 사람과 기계를 같은 타입으로 만들면
    "이 코드가 사람만 부를 수 있는가" 를 코드에서 알 수 없게 된다.
    """

    __slots__ = ("org_id", "service_account_id", "token_id",
                 "service_account_name", "scopes")

    def __init__(
        self, *, org_id: UUID, service_account_id: UUID, token_id: UUID,
        service_account_name: str, scopes: tuple[str, ...],
    ) -> None:
        self.org_id = org_id
        self.service_account_id = service_account_id
        self.token_id = token_id
        self.service_account_name = service_account_name
        self.scopes = scopes

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def require(self, scope: str) -> None:
        """없으면 403. 화면에서 숨기는 것으로 끝내지 않는다."""
        if scope not in self.scopes:
            raise ScopeDeniedError(
                f"이 토큰에는 {scope} 권한이 없습니다.",
                detail={"required_scope": scope, "granted": list(self.scopes)},
            )

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<TokenPrincipal {self.service_account_name} "
                f"scopes={len(self.scopes)}>")


def authenticate(db: Session, token: str) -> TokenPrincipal:
    """토큰 → 주체.

    접두사로 후보를 찾고 **해시 전체를 상수 시간으로** 비교한다. 접두사만으로
    인증하면 접두사만 맞히면 되는 셈이다.

    실패 사유를 응답에 구분해 주지 않는다 — "만료됨" 과 "없음" 이 다르면
    유효한 토큰을 찾는 데 쓸 수 있다.
    """
    digest = hash_token(token)
    row = db.execute(text("""
        SELECT t.id, t.org_id, t.service_account_id, t.token_hash, t.scopes,
               t.expires_at, t.revoked_at,
               s.name AS account_name, s.status AS account_status,
               s.deleted_at AS account_deleted_at
          FROM api_token t
          JOIN service_account s ON s.id = t.service_account_id
         WHERE t.token_hash = :h
    """), {"h": digest}).mappings().first()

    if row is None or not token_matches(token, row["token_hash"]):
        raise TokenAuthError("인증에 실패했습니다.")
    if row["revoked_at"] is not None:
        raise TokenAuthError("인증에 실패했습니다.")
    if row["expires_at"] is not None:
        from datetime import datetime as _dt

        if row["expires_at"] < _dt.now(row["expires_at"].tzinfo):
            raise TokenAuthError("인증에 실패했습니다.")
    # 계정을 끄면 토큰이 살아 있어도 통과하지 못한다 — 빠른 차단 수단이다.
    if row["account_status"] != "ACTIVE" or row["account_deleted_at"] is not None:
        raise TokenAuthError("인증에 실패했습니다.")

    # 마지막 사용 시각. 실패한 시도로는 갱신하지 않는다 — "언제 마지막으로
    # 정상 동작했나" 를 알아야 한다.
    db.execute(text("""
        UPDATE api_token SET last_used_at = now() WHERE id = :i
    """), {"i": str(row["id"])})
    db.execute(text("""
        UPDATE service_account SET last_used_at = now() WHERE id = :i
    """), {"i": str(row["service_account_id"])})
    db.commit()

    return TokenPrincipal(
        org_id=UUID(str(row["org_id"])),
        service_account_id=UUID(str(row["service_account_id"])),
        token_id=UUID(str(row["id"])),
        service_account_name=row["account_name"],
        scopes=tuple(row["scopes"] or ()),
    )


def record_agent_audit(
    db: Session, *, principal: TokenPrincipal, domain_table: str,
    domain_row_id: UUID, action: str, initiated_by_user_id: UUID | None = None,
    before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """서비스 계정이 한 일을 남긴다.

    `actor_user_id` 는 비운다 — 사람이 한 것이 아니다. 대신 `actor_type` 과
    계정 id 를 남겨서 "누가 했나" 에 정확히 답할 수 있게 한다.
    """
    db.execute(text("""
        INSERT INTO audit_log
               (org_id, domain_table, domain_row_id, action, actor_user_id,
                actor_type, actor_service_account_id, initiated_by_user_id,
                before_json, after_json, reason)
        VALUES (:o, :t, :r, CAST(:a AS enum_audit_action), NULL,
                'SERVICE_ACCOUNT', :sa, :init,
                CAST(:b AS JSONB), CAST(:af AS JSONB), :why)
    """), {
        "o": str(principal.org_id), "t": domain_table, "r": str(domain_row_id),
        "a": action, "sa": str(principal.service_account_id),
        "init": str(initiated_by_user_id) if initiated_by_user_id else None,
        "b": _json(before), "af": _json(after), "why": reason,
    })


def _json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
