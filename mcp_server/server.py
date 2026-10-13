"""Seller OS MCP 서버 — 표준 MCP stdio 전송.

이 프로세스가 하는 일은 **번역**뿐이다.

```
Claude ──MCP(stdio)──▶ 여기 ──HTTP+토큰──▶ Seller OS ──▶ 기존 service
```

여기에 업무 로직이 없다
-------------------
tool 목록도 스키마도 Seller OS 가 준다. 여기서 목록을 하드코딩하면 서버에
tool 이 하나 늘 때마다 이 파일을 고쳐야 하고, 둘이 어긋나는 순간 AI 는 없는
tool 을 부르거나 있는 tool 을 못 본다.

SQL 도 계산도 없다. 마진을 여기서 다시 계산하면 화면과 다른 숫자가 나온다.

전용 프로토콜을 MCP 라고 부르지 않는다
---------------------------------
공식 `mcp` SDK 의 `Server` + `stdio_server` 를 그대로 쓴다. 우리가 만든
JSON 규약에 MCP 라는 이름을 붙이지 않는다.

인증
----
`SELLER_OS_TOKEN` 은 서비스 계정 토큰이다. 사람 로그인 토큰은 서버가 거부한다.
이 값을 로그에 찍지 않는다 — 오류 메시지에도 넣지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp import types

SELLER_OS_URL = os.environ.get("SELLER_OS_URL", "http://127.0.0.1:8000").rstrip("/")
SELLER_OS_TOKEN = os.environ.get("SELLER_OS_TOKEN", "")
TIMEOUT_SECONDS = float(os.environ.get("SELLER_OS_TIMEOUT", "30"))


class SellerOsError(RuntimeError):
    """Seller OS 가 거부했다. **토큰을 메시지에 넣지 않는다.**"""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{SELLER_OS_URL}/api/v1",
        headers={"Authorization": f"Bearer {SELLER_OS_TOKEN}"},
        timeout=TIMEOUT_SECONDS,
    )


def _explain(status: int, body: str) -> str:
    """사용자가 읽고 무엇을 해야 할지 알 수 있는 문장.

    상태 코드만 돌려주면 에이전트가 "실패했다" 만 반복한다.
    """
    if status == 401:
        return ("Seller OS 인증에 실패했습니다. SELLER_OS_TOKEN 이 유효한 "
                "서비스 계정 토큰인지 확인하세요.")
    if status == 403:
        return ("이 토큰에는 그 작업 권한이 없습니다. Seller OS 설정 → "
                "AI · API 에서 권한을 확인하세요.")
    if status == 404:
        return ("Seller OS 에서 MCP 접근이 꺼져 있거나 없는 tool 입니다. "
                "설정 → AI · API 에서 MCP 를 켰는지 확인하세요.")
    return f"Seller OS 응답 오류 ({status}): {body[:300]}"


async def _get(path: str) -> Any:
    async with _client() as client:
        res = await client.get(path)
    if res.status_code >= 400:
        raise SellerOsError(_explain(res.status_code, res.text))
    return res.json().get("data")


async def _post(path: str, payload: dict[str, Any]) -> Any:
    async with _client() as client:
        res = await client.post(path, json=payload)
    if res.status_code >= 400:
        raise SellerOsError(_explain(res.status_code, res.text))
    return res.json().get("data")


async def on_list_tools(
    ctx: Any, params: Any,
) -> types.ListToolsResult:
    """tool 목록은 **서버가 준다.** 여기서 만들지 않는다."""
    _ = (ctx, params)
    tools = await _get("/mcp/tools")
    # ⚠️ Python SDK 의 필드는 **snake_case** 다(`input_schema`). camelCase 는
    # 전선 위 alias 일 뿐이라, `inputSchema=` 로 넘기면 조용히 무시되고
    # 클라이언트는 인자 없는 tool 을 보게 된다 — 실접속 스모크가 이걸 잡았다.
    return types.ListToolsResult(tools=[
        types.Tool(
            name=t["name"],
            description=t["description"],
            input_schema=t["input_schema"],
        )
        for t in (tools or [])
    ])


async def on_call_tool(
    ctx: Any, params: types.CallToolRequestParams,
) -> types.CallToolResult:
    """호출을 그대로 넘긴다.

    권한 판정은 Seller OS 가 한다 — 여기서 미리 걸러도 보안이 되지 않고,
    걸러 버리면 서버가 준 이유를 사용자가 못 본다.
    """
    _ = ctx
    try:
        data = await _post("/mcp/call", {
            "name": params.name,
            "arguments": dict(params.arguments or {}),
        })
    except SellerOsError as exc:
        # 실패를 성공으로 만들지 않는다. 에이전트가 "됐다" 고 말하면 안 된다.
        # `is_error` 다(camelCase 아님). 틀리면 실패가 **성공으로** 전달돼
        # 에이전트가 "됐다" 고 보고한다 — 스모크가 이걸 잡았다.
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(exc))],
            is_error=True,
        )

    return types.CallToolResult(content=[types.TextContent(
        type="text",
        text=json.dumps(data.get("result"), ensure_ascii=False, indent=1,
                        default=str),
    )])


async def main() -> None:
    if not SELLER_OS_TOKEN:
        # 토큰 없이 뜨면 모든 호출이 401 이 되고, 사용자는 이유를 모른다.
        print("SELLER_OS_TOKEN 이 설정되지 않았습니다. "
              "Seller OS 설정 → AI · API 에서 토큰을 발급하세요.",
              file=sys.stderr)
        raise SystemExit(2)

    server: Server[None] = Server(
        "seller-os",
        version="0.1.0",
        title="Seller OS",
        instructions=(
            "셀러 운영 데이터를 읽고 변경을 제안한다. "
            "숫자는 이미 계산된 값이니 다시 계산하지 말고 그대로 인용한다. "
            "원가·가격 변경은 직접 반영되지 않고 승인 요청으로만 만들어진다."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
