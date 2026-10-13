"""PICKSOME OPS TOOL 판매데이터 자동입력 MCP 서버 (docs/27 3-3 투트랙 중 '봇 push' 트랙).

이 MCP 서버는 백엔드(backend/app/api/v1/integration.py)의 봇 ingest API를
MCP 툴로 감싼 얇은 클라이언트다. DB에 직접 접근하지 않고, 전부 HTTP로 백엔드를 호출한다.

필요 환경변수 (전부 필수, 없으면 기동 시 에러):
  PICKSOME_API_BASE            예: http://localhost:8000/api/v1
  PICKSOME_INTEGRATION_ACCOUNT_ID   POST /api/v1/integrations 로 발급받은 계정 UUID
  PICKSOME_INTEGRATION_API_KEY      같은 발급 응답에서 받은 API Key (1회만 노출됨 — 분실 시 재발급)

발급 방법은 docs/28_mcp_bot_manual.md 참고 (이 서버를 쓰기 전에 먼저 계정을 만들어야 함,
프론트 화면: /automation/integrations).

실행: .venv/Scripts/python.exe server.py  (stdio transport, MCP 클라이언트가 stdin/stdout으로 통신)
"""

import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

API_BASE = os.environ.get("PICKSOME_API_BASE", "http://localhost:8000/api/v1")
ACCOUNT_ID = os.environ.get("PICKSOME_INTEGRATION_ACCOUNT_ID")
API_KEY = os.environ.get("PICKSOME_INTEGRATION_API_KEY")

if not ACCOUNT_ID or not API_KEY:
    print(
        "ERROR: PICKSOME_INTEGRATION_ACCOUNT_ID / PICKSOME_INTEGRATION_API_KEY 환경변수가 "
        "필요합니다. /automation/integrations 화면(또는 POST /integrations)에서 먼저 "
        "봇 계정을 발급받으세요. docs/28_mcp_bot_manual.md 참고.",
        file=sys.stderr,
    )
    raise SystemExit(1)

mcp = FastMCP("picksome-ops-bot")

_client = httpx.Client(
    base_url=API_BASE,
    headers={"X-Integration-Key": API_KEY},
    timeout=15.0,
)


class OrderItem(BaseModel):
    """sales_order 1건. 정규화 스키마 — 마켓 원본 형식이 아니라 이 형태로 맞춰서 보내야 함."""

    sale_date: str = Field(description="YYYY-MM-DD")
    marketplace: str = Field(description="판매채널 코드 — list_sale_channels 로 먼저 조회해서 정확한 값 확인")
    marketplace_order_no: str = Field(description="마켓 주문번호. 같은 마켓+주문번호는 중복 등록 거부됨")
    sku: str | None = Field(default=None, description="product.sku 매칭용 — 있으면 우선 사용")
    product_business_key: str | None = Field(
        default=None, description="sku 매칭 실패 시 대안 (product.business_key, 예: PROD-000001)"
    )
    product_snapshot_name: str | None = Field(default=None, description="상품명 스냅샷(매칭 실패해도 기록용)")
    quantity: int = Field(ge=1)
    sale_price: int = Field(ge=0, description="원 단위 정수(KRW), 소수/float 금지")
    ad_cost: int = Field(default=0, ge=0)
    shipping_fee: int = Field(default=0, ge=0)
    memo: str | None = None


class PushOrdersRequest(BaseModel):
    """push_sales_orders 입력 래퍼.

    주의: FastMCP(1.9.4)가 최상위 파라미터로 `list[OrderItem]`을 직접 받으면 스키마
    생성 중 내부적으로 오류가 나서(issubclass generic 이슈), 리스트를 모델로 한 번
    감싸서 넘긴다 — 최상위 파라미터는 항상 BaseModel 1개.
    """

    items: list[OrderItem] = Field(min_length=1, max_length=500)


def _raise_for_status(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        try:
            body = resp.json()
            message = body.get("error", {}).get("message", resp.text)
        except Exception:
            message = resp.text
        raise RuntimeError(f"PICKSOME API 오류 ({resp.status_code}): {message}")
    return resp.json()["data"]


@mcp.tool()
def list_sale_channels() -> list[dict[str, str]]:
    """이 조직에서 유효한 판매채널(마켓) 코드 목록을 조회한다.

    push_sales_orders 를 호출하기 전에 먼저 이걸로 marketplace 값이 정확한 코드인지
    확인하는 걸 권장한다 — 잘못된 코드를 넣으면 그 항목만 조용히 실패 처리된다
    (전체 요청이 막히지는 않음, push_sales_orders 결과의 failed_count 로 확인).
    """
    resp = _client.get(f"/integrations/{ACCOUNT_ID}/sale-channels")
    return _raise_for_status(resp)


@mcp.tool()
def push_sales_orders(request: PushOrdersRequest) -> dict[str, Any]:
    """판매 주문 목록을 PICKSOME OPS TOOL 에 자동 등록한다 (배치 1~500건).

    항목 단위로 처리되어 일부가 실패해도 나머지는 정상 등록된다(partial success).
    반환값의 status 가 'PARTIAL'이나 'FAILED'면 failed_count > 0 이라는 뜻 — 이 경우
    marketplace 코드가 잘못됐거나(list_sale_channels 로 재확인), 같은 marketplace_order_no 가
    이미 등록돼 있을 가능성이 높다(중복 재전송은 안전하게 실패 처리됨, 데이터가 두 번
    쌓이지 않음).
    """
    payload = {"items": [item.model_dump(exclude_none=True) for item in request.items]}
    resp = _client.post(f"/integrations/{ACCOUNT_ID}/ingest/orders", json=payload)
    return _raise_for_status(resp)


if __name__ == "__main__":
    mcp.run()
