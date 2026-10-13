"""MCP 게이트웨이 — 서비스 계정 토큰으로 tool 을 부르는 HTTP 표면.

왜 FastAPI 안에 두나
-----------------
MCP 서버 프로세스(`mcp_server/`)는 **밖에 있고** 여기를 HTTP 로 부른다.

```
Claude / OpenClaw  ──stdio(MCP)──▶  mcp_server  ──HTTP+토큰──▶  이 라우터  ──▶  service
```

이 방향이 중요하다. MCP SDK 가 Seller OS 의 의존성이 되지 않으므로, SDK 를
지우거나 MCP 를 꺼도 Core 는 그대로 돈다(§59). 실제로 `app/` 안에서 `mcp`
패키지를 import 하는 곳은 한 군데도 없다.

인증
----
**서비스 계정 토큰만** 받는다. 사람 JWT 는 거부한다 — AI 에게 사람 토큰을 주면
감사에 "사람이 했다" 로 남는다.

꺼져 있으면 없는 것처럼
-------------------
`MCP_ENABLED` 가 false 면 404 다. "설정은 껐는데 부르면 동작한다" 는 상태를
만들지 않는다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.core.response import envelope
from app.deps import get_db, get_token_principal
from app.services.service_account import TokenPrincipal

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _require_enabled() -> None:
    """꺼져 있으면 **경로 자체가 없는 것처럼** 응답한다."""
    from app.mcp import is_enabled

    if not is_enabled():
        raise NotFoundError("MCP 접근이 켜져 있지 않습니다.")


class CallIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.get("/tools")
def list_tools(
    principal: TokenPrincipal = Depends(get_token_principal),
    db: Session = Depends(get_db),
) -> dict:
    """이 토큰이 쓸 수 있는 tool.

    목록을 좁히는 것은 편의다 — 보안은 호출 시점의 scope 확인이 한다.
    """
    _require_enabled()
    _ = db
    from app.mcp import tools as mcp_tools

    return envelope([
        {"name": t.name, "description": t.description,
         "kind": t.kind, "scope": t.scope, "input_schema": t.input_schema}
        for t in mcp_tools.visible_tools(principal)
    ])


@router.post("/call")
def call_tool(
    payload: CallIn,
    principal: TokenPrincipal = Depends(get_token_principal),
    db: Session = Depends(get_db),
) -> dict:
    """tool 실행.

    권한이 없으면 **403** 이다(401 아님) — 인증은 됐고 권한이 없는 것이다.
    """
    _require_enabled()
    from app.mcp import tools as mcp_tools

    try:
        result = mcp_tools.call(
            db, principal, name=payload.name, arguments=payload.arguments,
        )
    except KeyError as exc:
        raise NotFoundError(str(exc)) from exc
    except (ValueError, TypeError) as exc:
        # 인자 문제. 내부 예외를 그대로 노출하지 않는다.
        raise ValidationError(f"tool 인자가 올바르지 않습니다: {exc}") from exc

    return envelope({"tool": payload.name, "result": result})


@router.get("/whoami")
def whoami(
    principal: TokenPrincipal = Depends(get_token_principal),
) -> dict:
    """토큰 확인용. 에이전트가 자기 권한을 알아야 무엇을 시도할지 정한다."""
    _require_enabled()
    return envelope({
        "service_account": principal.service_account_name,
        "scopes": list(principal.scopes),
        # 실행 tool 은 없다는 사실을 명시한다 — 에이전트가 찾다 포기하지 않게.
        "note": "읽기와 제안만 가능합니다. 실행이 필요한 일은 자동화 정책이 합니다.",
    })
