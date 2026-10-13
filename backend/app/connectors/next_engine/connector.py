"""NEXT ENGINE 커넥터 등록 — **주문 읽기만** 할 수 있다고 선언한다.

왜 레지스트리에 넣나
-----------------
`connector` 레지스트리는 "이 외부 시스템으로 무엇을 할 수 있나" 의 단일
출처다. 예외 센터·연동 화면·`assert_supports` 가 전부 여기를 본다.

Phase 1 까지 NEXT ENGINE 은 레지스트리에 없었다. 그래서 주문 적재가 실제로
되는데도 화면은 "연동 미구현" 이라고 말했다. 실제 능력과 선언이 어긋나면
사용자는 화면을 믿지 않게 된다.

**`ORDER_IMPORT` 하나만 선언한다.**

```
ORDER_IMPORT      주문 읽기. 실제 적재까지 검증됨(docs/77·78)
CLAIM_IMPORT      READ 는 되지만 재무 반영이 없다 → 선언하지 않는다
PRODUCT_IMPORT    상품 카탈로그 동기화를 하지 않는다 → 선언하지 않는다
*_EXPORT          외부 쓰기를 하지 않는다 → 영원히 선언하지 않는다
```

되는 것보다 넓게 선언하면 화면이 거짓말을 하고, 좁게 선언하면 되는 기능이
막힌다. 둘 다 사용자가 확인할 방법이 없으므로 **검증된 것만** 적는다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.connectors.next_engine.client import (
    NEXT_ENGINE_MAX_WINDOW_DAYS,
    NextEngineOrderClient,
    NextEngineReadError,
)
from app.services.connector import ConnectionResult, UnsupportedCapabilityError

#: 외부 시스템 코드. `external_system.code` 와 같아야 한다.
SYSTEM_CODE = "NEXT_ENGINE"


class NextEngineConnector:
    """`SellerToolConnector` 계약의 NEXT ENGINE 구현. **읽기 전용.**"""

    system_code = SYSTEM_CODE

    def __init__(self, *, client: NextEngineOrderClient | None = None) -> None:
        # 테스트가 결정적 transport 를 넣는다. 실제 API 를 부르지 않기 위해서다.
        self._client = client or NextEngineOrderClient()

    def capabilities(self) -> frozenset[str]:
        return frozenset({"ORDER_IMPORT"})

    def test_connection(self, *, credentials: dict[str, Any]) -> ConnectionResult:
        """가장 좁은 실제 READ 로 확인한다.

        자격증명이 파일에 있다는 것과 벤더가 그걸 받아 준다는 것은 다르다.
        `credential_secret_ref` 가 채워져 있다고 `CONNECTED` 로 쓰지 않는 이유다.

        한 건짜리 창을 본다 — 연결 확인이 목적이므로 데이터를 끌어올 이유가 없다.
        """
        end = datetime.now()
        start = end - timedelta(days=1)
        try:
            self._client.fetch_order_page(
                api_key=str(credentials.get("api_key") or ""),
                start=start, end=end, status="04", page=1, size=1,
            )
        except NextEngineReadError:
            # 벤더 메시지를 그대로 싣지 않는다 — 쿼리 조건과 URL 이 들어 있다.
            return ConnectionResult(
                ok=False,
                message="NEXT ENGINE 주문 조회에 실패했습니다.",
                account_identifier=None,
            )
        return ConnectionResult(
            ok=True,
            message="NEXT ENGINE 주문 조회 정상.",
            # 벤더가 계정 식별자를 따로 주지 않는다. 없는 값을 지어내지 않는다.
            account_identifier=None,
        )

    def fetch_products(
        self, *, credentials: dict[str, Any], since: date | None = None,
    ) -> list[Any]:
        """상품 카탈로그 동기화는 하지 않는다.

        빈 목록을 돌려주면 "상품 0건" 으로 보이고, 그건 확인해 봤다는 뜻으로
        읽힌다. 못 하는 것과 없는 것은 다르다.
        """
        raise UnsupportedCapabilityError(
            "NEXT ENGINE 상품 동기화는 지원하지 않습니다.",
            detail={"system_code": SYSTEM_CODE, "required_capability": "PRODUCT_IMPORT"},
        )

    @property
    def max_window_days(self) -> int:
        """벤더가 한 요청에 받아 주는 최대 조회 폭. 실측값이다(docs/73)."""
        return NEXT_ENGINE_MAX_WINDOW_DAYS
