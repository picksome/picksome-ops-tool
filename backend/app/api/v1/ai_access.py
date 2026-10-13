"""AI · API 접근 — 서비스 계정 · 토큰 (M044).

권한
----
```
전부 ADMIN
```

토큰 발급은 조직의 데이터를 외부 프로그램에 여는 일이다. 계정 이름을 고치는
것과 같은 무게로 다루지 않는다.

여기서 나가는 것과 나가지 않는 것
-----------------------------
```
나간다     계정 · 접두사 · 권한 · 만료 · 마지막 사용
나가지 않는다  토큰 원문(발급 응답 1회 제외) · 해시
```
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import service_account as sa

router = APIRouter(prefix="/ai-access", tags=["ai-access"])

# 토큰은 조직 데이터를 외부에 여는 열쇠다. 관리자만 다룬다.
_admin = require_roles("ADMIN")


class AccountIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)


class StatusIn(BaseModel):
    status: str


class TokenIn(BaseModel):
    """토큰 문자열을 받지 않는다 — **우리가 만든다.**

    사용자가 값을 정하게 하면 엔트로피를 보장할 수 없고, 그러면 빠른 해시를
    쓰는 근거(M044)가 무너진다.
    """

    scopes: list[str] = Field(..., min_length=1)
    label: str | None = Field(None, max_length=100)
    expires_at: datetime | None = None


@router.get("/scopes")
def list_scopes(
    user: CurrentUser = Depends(_admin),
) -> dict:
    """발급 가능한 권한 + **발급 불가 권한과 그 이유**.

    발급 불가 목록을 숨기면 "왜 지급 실행 권한이 없지" 를 계속 묻게 된다.
    """
    from app.core.api_token import FORBIDDEN_SCOPES, PROPOSE_SCOPES, READ_SCOPES

    _ = user
    return envelope({
        "read": list(READ_SCOPES),
        "propose": list(PROPOSE_SCOPES),
        "forbidden": [
            {"scope": s,
             "reason": "돈이 나가거나 확정된 기록을 되돌리는 일은 사람이 화면에서 합니다."}
            for s in FORBIDDEN_SCOPES
        ],
    })


@router.get("/service-accounts")
def list_accounts(
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    rows = sa.list_accounts(db, org_id=user.org_id)
    return envelope(rows, {"total": len(rows)})


@router.post("/service-accounts", status_code=201)
def create_account(
    payload: AccountIn,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(sa.create_account(
        db, org_id=user.org_id, actor_user_id=user.user_id, **payload.model_dump(),
    ))


@router.get("/service-accounts/{account_id}")
def get_account(
    account_id: UUID,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(sa.get_account(db, org_id=user.org_id, account_id=account_id))


@router.patch("/service-accounts/{account_id}/status")
def set_status(
    account_id: UUID,
    payload: StatusIn,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    """계정을 끄면 **토큰이 살아 있어도** 막힌다. 빠른 차단 수단이다."""
    return envelope(sa.set_account_status(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        account_id=account_id, status=payload.status,
    ))


@router.post("/service-accounts/{account_id}/tokens", status_code=201)
def issue_token(
    account_id: UUID,
    payload: TokenIn,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    """토큰 발급.

    ⚠️ **응답의 `token` 은 지금 한 번만 존재한다.** 서버에는 해시만 남고,
    다시 조회하는 경로는 만들지 않았다.
    """
    return envelope(sa.issue_token(
        db, org_id=user.org_id, actor_user_id=user.user_id, account_id=account_id,
        scopes=payload.scopes, label=payload.label, expires_at=payload.expires_at,
    ))


@router.delete("/tokens/{token_id}")
def revoke_token(
    token_id: UUID,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    """즉시 사용 불가. 행은 남는다 — 누가 언제 무엇을 했는지가 남아야 한다."""
    return envelope(sa.revoke_token(
        db, org_id=user.org_id, actor_user_id=user.user_id, token_id=token_id,
    ))


@router.get("/mcp-status")
def mcp_status(
    user: CurrentUser = Depends(_admin),
) -> dict:
    """MCP 서버 사용 가능 여부.

    MCP 는 **선택 모듈**이다. 꺼져 있어도 Seller OS 자동화는 그대로 돈다 —
    화면이 그 사실을 말해야 사용자가 "AI 를 안 쓰면 반쪽이구나" 로 오해하지
    않는다.
    """
    from app.config import get_settings

    _ = user
    settings = get_settings()
    enabled = bool(getattr(settings, "MCP_ENABLED", False))
    return envelope({
        "enabled": enabled,
        "note": (
            "AI 에이전트가 Seller OS 를 읽을 수 있습니다."
            if enabled else
            "꺼져 있습니다. 자동화·리포트는 그대로 동작하고, 외부 AI 접근만 없습니다."
        ),
    })
