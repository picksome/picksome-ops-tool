"""NEXT ENGINE (한국) 커넥터 — **읽기 전용**.

한국이다. 일본이 아니다
--------------------
```
한국  neapi.next-engine.co.kr    Api-Auth-Key 헤더    Hamee Korea OpenAPI
일본  api.next-engine.org        OAuth 2.0            developer.next-engine.com
```

브랜드 이름 말고 공통점이 없다. 검색하면 일본 문서가 먼저 나오고 주문·재고까지
전부 적혀 있어서 그대로 쓰고 싶어지는데, 인증 방식부터 달라 endpoint 가 같을
이유가 없다. **일본 문서를 근거로 이 패키지에 무언가를 추가하지 않는다**
(docs/72 8절).
"""

from app.connectors.next_engine.client import (
    NEXT_ENGINE_MAX_WINDOW_DAYS,
    NextEngineOrderClient,
    OrderPage,
)
from app.connectors.next_engine.normalize import (
    PII_FIELDS,
    ExternalOrderLine,
    normalize_order_line,
)

__all__ = [
    "NEXT_ENGINE_MAX_WINDOW_DAYS",
    "PII_FIELDS",
    "ExternalOrderLine",
    "NextEngineOrderClient",
    "OrderPage",
    "normalize_order_line",
]
