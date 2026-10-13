"""FastAPI 공통 의존성 (docs/22_api_spec.md §1-2).

제공 의존성:
  - get_db          : SessionLocal context (요청 종료 시 close)
  - get_current_user: Authorization: Bearer <JWT> → CurrentUser dataclass.
                       access token 만 허용 (refresh 토큰 차단), DB 에서 is_active/deleted_at 재확인.
  - get_request_id  : X-Request-Id header passthrough — audit_log.request_id 에 기록.

CurrentUser dataclass:
  user_id / org_id / role / email
  멀티테넌트 격리: 모든 service 가 org_id 를 WHERE 절에 포함하여 IDOR 방지.

권한 검증은 본 모듈이 아니라 core.rbac.require_roles 가 담당.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError
from app.core.security import decode_token
from app.db.session import SessionLocal

_bearer = HTTPBearer(auto_error=False)


@dataclass(slots=True)
class CurrentUser:
    user_id: UUID
    org_id: UUID
    role: str
    email: str | None = None


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_request_id(x_request_id: str | None = Header(default=None)) -> str:
    return x_request_id or str(uuid.uuid4())


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="AUTH_REQUIRED")
    try:
        claims = decode_token(creds.credentials)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e
    if claims.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="WRONG_TOKEN_TYPE")

    user_id = UUID(claims["sub"])
    org_id = UUID(claims["org_id"])
    role = claims["role"]

    row = db.execute(
        text("""
          SELECT email, is_active, deleted_at
            FROM app_user WHERE id = :id AND org_id = :org_id
        """),
        {"id": str(user_id), "org_id": str(org_id)},
    ).first()
    if row is None or row.deleted_at is not None or not row.is_active:
        raise ForbiddenError("계정 비활성화 또는 삭제됨")

    return CurrentUser(user_id=user_id, org_id=org_id, role=role, email=row.email)


# ──────────────────────────────────────────────────────────────────────────
# 서비스 계정 인증 (M044)
# ──────────────────────────────────────────────────────────────────────────

def get_token_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
):
    """API 토큰으로 인증된 **서비스 계정**.

    `get_current_user` 와 **다른 타입**을 돌려준다. 사람과 기계를 같은 타입으로
    만들면 "이 endpoint 를 기계가 부를 수 있나" 를 코드에서 알 수 없게 된다.

    사람 JWT 를 여기로 받지 않는다. 받아 주면 AI 에게 사람 토큰을 주는 길이
    열리고, 감사에 "김대표가 했다" 로 남는다.
    """
    from app.services.service_account import TokenAuthError, authenticate

    if creds is None or creds.scheme.lower() != "bearer":
        raise TokenAuthError("인증이 필요합니다.")

    from app.core.api_token import looks_like_token

    if not looks_like_token(creds.credentials):
        # 모양이 다르면 사람 JWT 다. 사유를 구체적으로 알려 준다 —
        # 이건 비밀이 아니라 사용법 문제고, 모르면 계속 헤맨다.
        raise TokenAuthError(
            "서비스 계정 토큰이 필요합니다. 사용자 로그인 토큰은 쓸 수 없습니다.",
        )
    return authenticate(db, creds.credentials)


def require_scope(scope: str):
    """scope 를 강제하는 의존성.

    ```
    @router.get("/x")
    def x(p = Depends(require_scope("products:read"))): ...
    ```

    화면에서 tool 을 숨기는 것으로 끝내지 않는다 — 직접 부르면 뚫린다.
    """

    def _dep(principal=Depends(get_token_principal)):
        principal.require(scope)
        return principal

    return _dep
