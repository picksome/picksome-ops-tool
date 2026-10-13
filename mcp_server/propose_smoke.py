"""MCP 실제 protocol 로 발주 초안을 제안하고 DB 를 확인한다.

내부 함수를 부르지 않는다. stdio 왕복을 거친 결과만 본다.

`smoke_client.py` 가 읽기 tool 을 훑는다면, 이 스크립트는 **쓰기 경로 하나**를
끝까지 본다 — 제안이 DRAFT 로만 끝나는가, 주체가 사람으로 위장되지 않는가,
원가 권한 없는 토큰에 금액이 새지 않는가.

서버에 `MCP_ENABLED=true` 가 필요하다. 꺼져 있으면 게이트웨이가 404 다 —
그게 MCP 를 선택 기능으로 둔 설계다.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
from uuid import UUID

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from sqlalchemy import text  # noqa: E402

# backend 를 sys.path 에 넣은 **뒤**여야 import 된다 — E402 는 의도적이다.
from app.db.session import SessionLocal  # noqa: E402

SERVER = pathlib.Path(__file__).resolve().parent / "server.py"


def text_of(result) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        parts.append(getattr(item, "text", "") or "")
    return "\n".join(parts)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--token-file", required=True,
                    help="토큰 원문 파일. 명령행에 값을 직접 넣지 않는다")
    ap.add_argument("--org", required=True, help="검증 대상 org UUID")
    args = ap.parse_args()

    org = UUID(args.org)
    db = SessionLocal()
    supplier = db.execute(text("""
        SELECT id, name FROM supplier_site
         WHERE org_id = :o AND deleted_at IS NULL ORDER BY name LIMIT 1
    """), {"o": str(org)}).mappings().one()
    product = db.execute(text("""
        SELECT id, name_ko FROM product
         WHERE org_id = :o AND deleted_at IS NULL LIMIT 1
    """), {"o": str(org)}).mappings().one()
    before = db.execute(text(
        "SELECT COUNT(*) FROM purchase_order WHERE org_id = :o"),
        {"o": str(org)}).scalar_one()

    token = pathlib.Path(args.token_file).read_text(encoding="utf-8").strip()
    params = StdioServerParameters(
        command=sys.executable, args=[str(SERVER)],
        env={**os.environ, "SELLER_OS_URL": args.url,
             "SELLER_OS_TOKEN": token},
    )

    failures = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── 사유 없는 제안은 거부돼야 한다 ──────────────
            res = await session.call_tool("propose_purchase_order", {
                "supplier_site_id": str(supplier["id"]),
                "lines": [{"product_id": str(product["id"]),
                           "quantity": 5, "unit_price": 1000}],
                "reason": "   ",
            })
            if not getattr(res, "is_error", False):
                failures.append("사유 없는 제안이 통과했다")
            else:
                print("[propose] 사유 없음 → 거부됨 (정상)")

            # ── 단가 없는 제안도 거부 ──────────────────────
            res = await session.call_tool("propose_purchase_order", {
                "supplier_site_id": str(supplier["id"]),
                "lines": [{"product_id": str(product["id"]), "quantity": 5}],
                "reason": "단가 누락 확인",
            })
            if not getattr(res, "is_error", False):
                failures.append("단가 없는 제안이 통과했다")
            else:
                print("[propose] 단가 없음 → 거부됨 (정상)")

            # ── 정상 제안 ─────────────────────────────────
            res = await session.call_tool("propose_purchase_order", {
                "supplier_site_id": str(supplier["id"]),
                "lines": [{"product_id": str(product["id"]),
                           "quantity": 5, "unit_price": 1000}],
                "reason": "MCP protocol 검증",
            })
            body = text_of(res)
            if getattr(res, "is_error", False):
                failures.append(f"제안 실패: {body[:300]}")
                print("[propose] 실패:", body[:300])
            else:
                payload = json.loads(body)
                print(f"[propose] ok — {payload['business_key']} "
                      f"status={payload['status']}")
                if payload["status"] != "DRAFT":
                    failures.append(f"DRAFT 가 아니다: {payload['status']}")

                row = db.execute(text("""
                    SELECT status, created_by, created_by_service_account,
                           automation_run_id
                      FROM purchase_order WHERE id = :i
                """), {"i": payload["purchase_order_id"]}).mappings().one()
                print(f"[DB] status={row['status']} "
                      f"created_by={row['created_by']} "
                      f"service_account={row['created_by_service_account']}")
                if row["created_by"] is not None:
                    failures.append("사람 actor 로 저장됐다")
                if row["created_by_service_account"] is None:
                    failures.append("서비스 계정이 기록되지 않았다")

                # 감사에도 사람으로 위장하지 않았는지
                # 감사 행은 **하나**여야 한다. 주체 없는 행이 함께 남으면
                # "누가 만들었나" 에 두 답이 생긴다.
                rows = db.execute(text("""
                    SELECT actor_type, actor_user_id, actor_service_account_id
                      FROM audit_log
                     WHERE domain_table = 'purchase_order'
                       AND domain_row_id = :i
                """), {"i": payload["purchase_order_id"]}).mappings().all()
                if len(rows) != 1:
                    failures.append(f"감사 행이 {len(rows)}개다 (1개여야 함)")
                audit = rows[0] if rows else None
                if audit is None:
                    failures.append("감사 기록이 없다")
                else:
                    print(f"[audit] actor_type={audit['actor_type']} "
                          f"user={audit['actor_user_id']}")
                    if audit["actor_type"] != "SERVICE_ACCOUNT":
                        failures.append(
                            f"actor_type 이 {audit['actor_type']} 이다")
                    if audit["actor_user_id"] is not None:
                        failures.append("감사에 사람 id 가 찍혔다")

            # ── 원가 가림: 발주 상세를 다시 읽는다 ──────────
            #
            # 이 토큰에는 cost:read 가 없다. 방금 만든 발주의 금액이 보이면
            # 제안 경로로 원가가 새는 것이다.
            if not failures:
                got = await session.call_tool("get_purchase_order", {
                    "order_id": payload["purchase_order_id"]})
                detail = text_of(got)
                for leak in ("unit_price", "line_total", "total_amount"):
                    if leak in detail:
                        failures.append(f"cost:read 없이 {leak} 노출")
                if "unit_price" not in detail:
                    print("[redaction] cost:read 없음 → 금액 가려짐 (정상)")

    after = db.execute(text(
        "SELECT COUNT(*) FROM purchase_order WHERE org_id = :o"),
        {"o": str(org)}).scalar_one()
    print(f"[DB] 발주 {before} → {after}")

    if failures:
        print("\n실패:")
        for f in failures:
            print(" -", f)
        return 1
    print("\n전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
