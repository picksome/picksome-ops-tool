"""MCP 접근 계층 — **선택 모듈**.

의존 방향
--------
```
MCP  →  Seller OS Service        (허용)
Seller OS Service  →  MCP        (금지)
```

Core 의 어떤 모듈도 이 패키지를 import 하지 않는다. 이 디렉터리를 통째로
지워도 주문 수집·마진 감시·정산 대사·일일 리포트는 그대로 돈다. 그 사실을
`test_mcp_optional.py` 가 고정한다.

왜 이 방향을 지키나
----------------
AI 접근이 Core 의 필수 의존성이 되면, AI 를 안 쓰는 고객에게도 AI 장애가
전파된다. Seller OS 는 AI 없이 완결되어야 하고, AI 는 그 위에 얹히는
편의여야 한다.

여기서 계산하지 않는다
------------------
tool 은 기존 service 를 부른다. SQL 도 마진 계산식도 여기 없다. MCP 안에서
숫자를 다시 만들면 화면과 AI 가 서로 다른 답을 내고, 그때 어느 쪽이 맞는지
아무도 모른다.
"""

from __future__ import annotations


def is_enabled() -> bool:
    """MCP 를 켰는가.

    꺼져 있으면 서버도 tool 목록도 노출되지 않는다. "설정은 껐는데 tool 은
    보인다" 는 상태를 만들지 않는다.
    """
    from app.config import get_settings

    return bool(getattr(get_settings(), "MCP_ENABLED", False))
