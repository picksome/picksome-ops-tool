"""셀러툴 커넥터 계약 (M038/M039).

실제 API 를 호출하지 않는다
------------------------
이 모듈에는 NEXT ENGINE 도 윈들리도 없다. **계약만** 있다.
API 문서를 기억으로 추측해 구현하면, 실제 응답이 다를 때 조용히 틀린 값이
Core 로 들어온다. credential 과 실제 응답 샘플이 생긴 뒤에 구현한다.

커넥터는 DB 를 건드리지 않는다
--------------------------
```
Connector      외부 API → DTO          (vendor 지식이 여기서 끝난다)
Sync Service   DTO → 검증 → 매핑 → Core (vendor 를 모른다)
```

커넥터가 업무 테이블을 직접 쓰면 vendor 로직이 Core 로 샌다. 그러면 셀러툴을
하나 더 붙일 때마다 상품·주문 서비스에 분기가 늘어난다.

지원하지 않는 것을 성공으로 만들지 않는다
-----------------------------------
capability 가 `False` 면 `UnsupportedCapability` 를 올린다. 빈 리스트를
돌려주면 "가져왔는데 0건" 과 "가져올 수 없다" 가 구분되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from app.core.exceptions import DomainError

# ── capability ────────────────────────────────────────────────
#
# 커넥터가 **코드로 선언한다.** DB 가 진실이 아니다 — 계정 설정으로 켤 수 있는
# 것은 "쓸지 말지" 이지 "할 수 있는지" 가 아니다.
CAPABILITIES = (
    "PRODUCT_IMPORT",
    "PRODUCT_EXPORT",
    "SOURCE_INFO_IMPORT",
    "SOURCE_URL_IMPORT",
    "SOURCE_COST_IMPORT",
    "ORDER_IMPORT",
    "CLAIM_IMPORT",
    "INVENTORY_IMPORT",
    "PURCHASE_IMPORT",
    "PRICE_IMPORT",
    "PRICE_EXPORT",
)

# sync_type → 그 동기화에 필요한 capability.
SYNC_TYPE_CAPABILITY = {
    "PRODUCTS": "PRODUCT_IMPORT",
    "ORDERS": "ORDER_IMPORT",
    # 클레임은 주문과 다른 endpoint 이고 따로 켠다(M057). 주문을 읽을 수
    # 있다고 취소·반품까지 읽을 수 있는 것은 아니다.
    "CLAIMS": "CLAIM_IMPORT",
    "INVENTORY": "INVENTORY_IMPORT",
    "SOURCES": "SOURCE_INFO_IMPORT",
    "PURCHASES": "PURCHASE_IMPORT",
}


class ConnectorUnavailableError(DomainError):
    """이 외부 시스템의 커넥터가 아직 구현되지 않았다.

    422 가 아니라 501 이다 — 입력이 잘못된 게 아니라 우리가 아직 못 하는 것이다.
    """

    code = "CONNECTOR_UNAVAILABLE"
    http_status = 501


class UnsupportedCapabilityError(DomainError):
    """커넥터는 있지만 그 기능을 지원하지 않는다."""

    code = "UNSUPPORTED_CAPABILITY"
    http_status = 422


# ── DTO ───────────────────────────────────────────────────────
#
# 외부에서 온 값의 모양. **업무 테이블과 다르다** — 여기서 Core 로 넘어갈 때
# 정규화·검증을 거친다. 외부 JSON 을 그대로 업무 테이블에 넣지 않는다.


@dataclass(slots=True)
class ExternalProduct:
    """외부 시스템이 말하는 상품 하나.

    필드가 적은 이유: **확인된 것만 둔다.** 실제 API 응답을 보기 전에
    "아마 이런 필드가 있을 것" 으로 늘리면 매핑이 추측이 된다.
    """

    external_id: str
    name: str | None = None
    #: 공급처 상품 URL. 셀러툴이 소싱 정보를 갖고 있는 경우.
    source_url: str | None = None
    #: 매입가. **이 값이 곧 상품 원가가 되지 않는다** — 원가 version 후보일 뿐이다.
    source_cost: Decimal | None = None
    source_cost_currency: str | None = None
    #: 외부가 아는 공급처 이름. 우리 `supplier_site` 와 자동으로 잇지 않는다.
    supplier_name: str | None = None
    observed_at: date | None = None
    #: 정규화 전 원본. 디버깅용이며 **업무 진실이 아니다.**
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConnectionResult:
    ok: bool
    message: str
    #: 외부가 알려 준 계정 식별자(회사코드 등). 비밀이 아니다.
    account_identifier: str | None = None


class SellerToolConnector(Protocol):
    """셀러툴 하나에 대한 읽기 계약.

    `fetch_*` 는 **가져오기만** 한다. 저장은 sync 서비스가 한다.
    """

    system_code: str

    def capabilities(self) -> frozenset[str]: ...

    def test_connection(self, *, credentials: dict[str, Any]) -> ConnectionResult: ...

    def fetch_products(
        self, *, credentials: dict[str, Any], since: date | None = None
    ) -> list[ExternalProduct]: ...


# ── registry ──────────────────────────────────────────────────

_REGISTRY: dict[str, SellerToolConnector] = {}


def register(connector: SellerToolConnector, *, allow_in_production: bool = True) -> None:
    """커넥터 등록.

    `allow_in_production=False` 는 **테스트 전용 커넥터**를 위한 것이다.
    운영에서 가짜 커넥터가 성공을 돌려주면 사용자는 동기화가 되고 있다고 믿는다.
    """
    if not allow_in_production and _is_production():
        raise RuntimeError(
            f"테스트 전용 커넥터를 운영에 등록할 수 없습니다: {connector.system_code}"
        )
    _REGISTRY[connector.system_code] = connector


def unregister(system_code: str) -> None:
    """테스트가 끝나며 정리할 때 쓴다."""
    _REGISTRY.pop(system_code, None)


def get_connector(system_code: str) -> SellerToolConnector:
    connector = _REGISTRY.get(system_code)
    if connector is None:
        raise ConnectorUnavailableError(
            f"{system_code} 연동은 아직 구현되지 않았습니다.",
            detail={"system_code": system_code, "implemented": sorted(_REGISTRY)},
        )
    return connector


def has_connector(system_code: str) -> bool:
    return system_code in _REGISTRY


def capabilities_of(system_code: str) -> dict[str, bool]:
    """전 capability 를 True/False 로 채워 돌려준다.

    지원하는 것만 돌려주면 화면이 "목록에 없음" 과 "지원 안 함" 을 구분하지
    못한다. 구현이 없으면 전부 False 다 — 그 자체가 정확한 답이다.
    """
    supported: frozenset[str] = frozenset()
    if has_connector(system_code):
        supported = get_connector(system_code).capabilities()
    return {c: c in supported for c in CAPABILITIES}


def assert_supports(system_code: str, sync_type: str) -> None:
    needed = SYNC_TYPE_CAPABILITY.get(sync_type)
    if needed is None:
        raise UnsupportedCapabilityError(f"알 수 없는 동기화 유형입니다: {sync_type}")
    if needed not in get_connector(system_code).capabilities():
        raise UnsupportedCapabilityError(
            f"{system_code} 는 {sync_type} 동기화를 지원하지 않습니다.",
            detail={"required_capability": needed},
        )


def _is_production() -> bool:
    from app.config import get_settings

    # `APP_ENV` 는 dev / staging / prod 다(app/config.py). 앞글자만 보지 않는다 —
    # "production" 과 "prod" 를 둘 다 받아야 설정 표기가 달라도 안전하다.
    return str(getattr(get_settings(), "APP_ENV", "")).lower() in ("prod", "production")
