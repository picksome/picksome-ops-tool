"""마진 계산 입출력 도메인.

services/calculation.py 의 순수 함수가 사용하는 dataclass.
DB 의존 없음 — 단위 테스트에서 직접 인스턴스화 가능.

docs/22_api_spec.md §3 (마진계산) ↔ services/margin.py (API 어댑터) ↔
services/calculation.py (순수 산식) 의 3단 분리.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.policy import PolicySnapshotRef, ResolvedPolicies


@dataclass(slots=True)
class ExpectedMarginInput:
    """예상 마진 계산 입력 (등록 전 시뮬레이션).

    - margin_type: 마진 산식 분류 (일반 / 로켓그로스 / 위탁 등) — 정책 매칭 키.
    - extras: 산식 외 추가 비용 (예: 패키징비) — calculation 에서 합산.
    - override_fields: 정책 무시하고 강제 값 사용 (audit 대상).
    """

    org_id: UUID
    margin_type: str
    marketplace: str
    cost_amount: int
    sale_price: int
    payment_method: str
    product_id: UUID | None = None  # save=True 시에만 필요 (계산 자체엔 미사용)
    extras: dict[str, Any] = field(default_factory=dict)
    override_fields: dict[str, Any] | None = None
    product_variant_id: UUID | None = None  # D-286 옵션 variant 단위 계산


@dataclass(slots=True)
class ExpectedMarginResult:
    """예상 마진 결과. 모든 금액 = int(원), 비율 = Decimal."""

    marketplace_fee: int
    payment_fee: int
    ad_cost: int
    fulfillment_fee: int
    return_risk_cost: int
    vat: int
    margin_amount: int
    margin_rate: Decimal
    applied_policies: ResolvedPolicies  # 박제용 — 결과 저장 시 snapshot_refs 추출
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ActualMarginInput:
    """실제 마진 계산 입력 — 판매 발생 후 정산서 기반."""

    org_id: UUID
    sales_order_id: UUID
    settlement_record_id: UUID | None = None  # 쿠팡 정산 매칭 시


@dataclass(slots=True)
class ActualMarginResult:
    """실제 마진. 예상 대비 delta 도 함께 산출 → 정확도 모니터링."""

    # 상품 마스터에 링크되지 않은 판매(엑셀 import 등)도 실 마진을 낼 수 있어야
    # 한다. 실 마진의 정체성은 sales_order 다 — M013 참조.
    product_id: UUID | None
    product_variant_id: UUID | None
    revenue: int
    cost: int
    shipping_cost: int  # 매입 배송비 — 원가와 분리 보관 (M013)
    marketplace_fee: int
    payment_fee: int
    ad_cost: int
    fulfillment_fee: int
    return_cost: int
    vat: int
    margin_amount: int
    margin_rate: Decimal
    matched_expected_id: UUID | None
    delta_margin_amount: int | None
    delta_margin_rate: Decimal | None
    snapshot_refs: list[PolicySnapshotRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 정산 컴포넌트 분해 (docs/83 D1~D3). 매칭된 주문에만 있다.
    #: 컬럼으로 쪼개지 않고 계산 snapshot 으로 박제한다 — 합계를 컬럼에 박으면
    #: `marketplace_settlement_record` 와 두 번째 truth 가 된다.
    settlement_snapshot: dict[str, Any] | None = None
    #: 대표 정산 행. `product_margin_actual.settlement_record_id` 가 단일 FK 라
    #: 여러 행을 가리킬 수 없어 기준점만 둔다(전체 목록은 snapshot 안에 있다).
    settlement_anchor_id: UUID | None = None


@dataclass(slots=True)
class PriceSimInput:
    """등록가 시뮬레이션 입력 (역산).

    target_rates 별로 "이 마진율을 달성하려면 얼마에 팔아야 하는가" 계산.
    """

    org_id: UUID
    margin_type: str
    marketplace: str
    cost_amount: int
    extras: dict[str, Any] = field(default_factory=dict)
    payment_method: str = "BUSINESS_CARD"
    target_rates: list[Decimal] = field(default_factory=list)


@dataclass(slots=True)
class PriceSimRow:
    """target_rate 1건의 시뮬레이션 결과 (권장가 + 검증용 예상 마진 재계산)."""

    target_rate: Decimal
    recommended_price: int
    expected_margin: ExpectedMarginResult
