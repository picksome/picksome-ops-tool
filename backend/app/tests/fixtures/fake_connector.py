"""테스트 전용 커넥터.

**운영 registry 에 등록될 수 없다.** `register(..., allow_in_production=False)`
가 `APP_ENV=prod` 에서 예외를 올린다. 운영에서 가짜 커넥터가 성공을 돌려주면
사용자는 동기화가 되고 있다고 믿는다 — 그게 이 프로젝트에서 가장 피하려는 것이다.

이것은 "외부 API 성공 mock" 이 아니라 **계약 검증용 stub** 이다. 차이는
어디에 등록되느냐다: 테스트 안에서만, 그리고 테스트가 끝나면 해제된다.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Any

from app.services import connector as conn

TEST_SYSTEM_CODE = "TEST_TOOL"


class FakeSellerToolConnector:
    """정해진 상품 목록을 돌려주는 커넥터."""

    system_code = TEST_SYSTEM_CODE

    def __init__(
        self,
        products: list[conn.ExternalProduct] | None = None,
        *,
        supported: frozenset[str] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._products = products or []
        self._supported = supported if supported is not None else frozenset({
            "PRODUCT_IMPORT", "SOURCE_COST_IMPORT",
        })
        self._fail_with = fail_with

    def capabilities(self) -> frozenset[str]:
        return self._supported

    def test_connection(self, *, credentials: dict[str, Any]) -> conn.ConnectionResult:
        _ = credentials
        return conn.ConnectionResult(ok=True, message="테스트 커넥터", account_identifier="TEST-1")

    def fetch_products(
        self, *, credentials: dict[str, Any], since: date | None = None
    ) -> list[conn.ExternalProduct]:
        _ = (credentials, since)
        if self._fail_with is not None:
            raise self._fail_with
        return list(self._products)


@contextmanager
def use_fake_connector(connector: FakeSellerToolConnector):
    """테스트 동안만 등록하고 끝나면 지운다.

    지우지 않으면 다음 테스트가 "커넥터 없음" 을 검증할 때 통과해 버린다 —
    실행 순서에 결과가 좌우된다.
    """
    conn.register(connector, allow_in_production=False)
    try:
        yield connector
    finally:
        conn.unregister(connector.system_code)
