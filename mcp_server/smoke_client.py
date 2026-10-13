"""MCP 실접속 스모크 — **진짜 protocol client**.

무엇을 증명하려는 건가
-------------------
"tool 함수를 직접 호출해 봤다" 는 MCP 가 동작한다는 증거가 아니다. 실제
클라이언트가 붙어서 handshake 하고 tool 목록을 받아 호출까지 되어야 한다.

그래서 이 스크립트는 공식 `mcp` SDK 의 클라이언트로 **stdio transport** 를 통해
`server.py` 를 띄우고 표준 흐름을 그대로 탄다.

```
smoke_client ──stdio(MCP)──▶ server.py ──HTTP+토큰──▶ Seller OS ──▶ service
   initialize
   tools/list
   tools/call
```

어디서도 Seller OS 의 내부 함수를 직접 부르지 않는다.

쓰는 법
------
```
python mcp_server/smoke_client.py --url http://127.0.0.1:8155 --token-file <경로>
```

토큰 원문을 인자로 받지 않는다 — 명령행은 프로세스 목록과 셸 히스토리에 남는다.
출력에도 찍지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = pathlib.Path(__file__).with_name("server.py")


def _text_of(result) -> str:
    """CallToolResult → 사람이 읽는 문자열."""
    parts = []
    for block in getattr(result, "content", []) or []:
        parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts)


async def run(url: str, token: str, *, expect_denied: list[str]) -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env={**os.environ, "SELLER_OS_URL": url, "SELLER_OS_TOKEN": token},
    )

    failures: list[str] = []

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # ── initialize ────────────────────────────────────
            init = await session.initialize()
            server_name = getattr(getattr(init, "serverInfo", None), "name", "?")
            version = getattr(init, "protocolVersion", "?")
            print(f"[initialize] ok — server={server_name} protocol={version}")

            # ── tools/list ────────────────────────────────────
            listed = await session.list_tools()
            names = [t.name for t in listed.tools]
            print(f"[tools/list] {len(names)}개: {', '.join(names)}")
            if not names:
                failures.append("tools/list 가 비었다")

            # 스키마가 실제로 붙어 있는지 — 이름만 오면 클라이언트가 인자를
            # 만들 수 없다.
            for tool in listed.tools:
                if not getattr(tool, "input_schema", None):
                    failures.append(f"{tool.name} 에 inputSchema 가 없다")

            # ── tools/call ────────────────────────────────────
            for name, args in (
                ("search_products", {"limit": 3}),
                ("get_sales_summary", {}),
                ("get_daily_report", {}),
            ):
                if name not in names:
                    failures.append(f"{name} 이 목록에 없다")
                    continue
                res = await session.call_tool(name, args)
                body = _text_of(res)
                flag = "ERROR" if getattr(res, "is_error", False) else "ok"
                print(f"[tools/call] {name}: {flag} ({len(body)}자)")
                if getattr(res, "is_error", False):
                    failures.append(f"{name} 실패: {body[:200]}")

            # get_product 는 실제 id 가 필요하다 — 앞의 검색 결과를 쓴다.
            if "search_products" in names and "get_product" in names:
                found = await session.call_tool("search_products", {"limit": 1})
                try:
                    items = json.loads(_text_of(found)).get("items", [])
                except json.JSONDecodeError:
                    items = []
                if items:
                    pid = items[0]["id"]
                    res = await session.call_tool("get_product", {"product_id": pid})
                    ok = not getattr(res, "is_error", False)
                    print(f"[tools/call] get_product: {'ok' if ok else 'ERROR'}")
                    if not ok:
                        failures.append("get_product 실패")
                    # 원가 권한이 없으면 가려져야 한다.
                    if "cost_amount" in _text_of(res) and "권한 없음" not in _text_of(res):
                        failures.append("cost:read 없이 원가가 노출됐다")
                else:
                    print("[tools/call] get_product: 건너뜀 (상품 없음)")

            # ── 재고 · 발주 ───────────────────────────────────
            #
            # 내부 함수를 부르지 않는다. 여기서 도는 것은 전부 실제 stdio
            # MCP 왕복이다 — 서버에 tool 을 등록하는 것을 빠뜨려도 단위
            # 테스트는 통과하지만 이건 실패한다.
            inventory_ok = "get_inventory_status" in names
            if inventory_ok:
                res = await session.call_tool("get_inventory_status", {"limit": 5})
                body = _text_of(res)
                if getattr(res, "is_error", False):
                    failures.append(f"get_inventory_status 실패: {body[:200]}")
                else:
                    print(f"[tools/call] get_inventory_status: ok ({len(body)}자)")
                    # 모른다는 사실이 데이터에 남아 있어야 한다. 없으면 AI 가
                    # "예약 0개" 로 요약한다.
                    if "NOT_CONNECTED" not in body:
                        failures.append(
                            "reserved_status(NOT_CONNECTED) 가 응답에 없다")

                    # 원장 이력 — 상품 하나를 골라 부른다.
                    try:
                        items = json.loads(body).get("items", [])
                    except json.JSONDecodeError:
                        items = []
                    if items and "list_inventory_movements" in names:
                        res = await session.call_tool(
                            "list_inventory_movements",
                            {"product_id": items[0]["product_id"], "limit": 5})
                        ok = not getattr(res, "is_error", False)
                        print(f"[tools/call] list_inventory_movements: "
                              f"{'ok' if ok else 'ERROR'}")
                        if not ok:
                            failures.append("list_inventory_movements 실패")
                        elif "unit_cost" in _text_of(res):
                            # cost:read 없는 토큰으로 도는 스모크다.
                            failures.append(
                                "cost:read 없이 취득 단가가 노출됐다")
                    else:
                        print("[tools/call] list_inventory_movements: 건너뜀 "
                              "(재고 없음)")

            # 재주문 — **기간을 안 주면 계산하지 않아야 한다.**
            if "list_reorder_candidates" in names:
                res = await session.call_tool("list_reorder_candidates", {})
                body = _text_of(res)
                if getattr(res, "is_error", False):
                    failures.append(f"list_reorder_candidates 실패: {body[:200]}")
                elif "NOT_CONFIGURED" not in body:
                    # 기본값을 만들어 계산해 버리면 AI 가 그 숫자를 사람에게
                    # 전달하고, 사람은 근거 없는 값을 믿는다.
                    failures.append(
                        "기간 없이 부른 재주문이 NOT_CONFIGURED 가 아니다")
                else:
                    print("[tools/call] list_reorder_candidates: "
                          "기간 없으면 계산 안 함 (정상)")

                res = await session.call_tool(
                    "list_reorder_candidates",
                    {"velocity_days": 30, "coverage_days": 30})
                ok = not getattr(res, "is_error", False)
                print(f"[tools/call] list_reorder_candidates(30/30): "
                      f"{'ok' if ok else 'ERROR'}")
                if not ok:
                    failures.append("기간을 준 재주문 호출 실패")

            if "list_order_exceptions" in names:
                res = await session.call_tool("list_order_exceptions", {})
                ok = not getattr(res, "is_error", False)
                print(f"[tools/call] list_order_exceptions: "
                      f"{'ok' if ok else 'ERROR'}")
                if not ok:
                    failures.append("list_order_exceptions 실패")

            # ── 실행 tool 이 존재하지 않는다 ──────────────────
            #
            # 없는 것을 확인하는 것도 검증이다. 나중에 누가 추가하면 여기서 걸린다.
            for forbidden in ("place_purchase_order", "pay_supplier",
                              "purchase_submit", "execute_purchase_order"):
                if forbidden in names:
                    failures.append(f"{forbidden} tool 이 존재한다 (금지)")
            print("[tools/list] 발주 실행 tool 없음 (정상)")

            # ── scope 거부 ────────────────────────────────────
            #
            # 목록에서 숨기는 것은 보안이 아니다. **이름을 알고 불러도** 서버가
            # 막아야 한다.
            for name in expect_denied:
                if name in names:
                    failures.append(f"{name} 이 권한 없이 목록에 보인다")
                res = await session.call_tool(name, {})
                if not getattr(res, "is_error", False):
                    failures.append(f"{name} 이 권한 없이 실행됐다")
                else:
                    print(f"[scope] {name}: 거부됨 (정상)")

    if failures:
        print("\n실패:")
        for f in failures:
            print(" -", f)
        return 1
    print("\n전부 통과")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--token-file", required=True,
                    help="토큰 원문 파일. 명령행에 값을 직접 넣지 않는다")
    ap.add_argument("--expect-denied", default="get_cashflow",
                    help="권한이 없어 거부되어야 하는 tool (쉼표 구분)")
    args = ap.parse_args()

    token = pathlib.Path(args.token_file).read_text(encoding="utf-8").strip()
    denied = [t.strip() for t in args.expect_denied.split(",") if t.strip()]
    return asyncio.run(run(args.url, token, expect_denied=denied))


if __name__ == "__main__":
    sys.exit(main())
