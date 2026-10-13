"""정책 도메인 타입.

CLAUDE.md Generalization Principle: 모든 비즈니스 값은 정책 테이블화.
ERD 의 정책 12종 중 **계산에 사용되는** 항목만 dataclass 로 표현.

박제 원칙 (CLAUDE.md Calculation Rules):
- 모든 정책 row 는 `version` 을 가지며 `effective_from/effective_to` 범위로 시점 조회.
- 계산 결과는 사용된 policy_row_id + version 을 PolicySnapshotRef 로 기록 →
  과거 시점 재현 가능.

DB row → dataclass 변환은 services/policy.resolve_active_policies() 에서 담당.
ORM 모델을 직접 사용하지 않는 이유: 계산 로직이 SQLAlchemy 세션에 의존하지 않게 격리.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID


@dataclass(slots=True)
class MarketplaceFeePolicyRow:
    """마켓 수수료 정책 (RATE/FIXED/TIERED 3종 fee_type)."""

    id: UUID
    marketplace: str
    category_path: str | None  # 카테고리별 차등 수수료 (NULL = 전체 적용)
    fee_type: Literal["RATE", "FIXED", "TIERED"]
    rate: Decimal | None  # fee_type=RATE 일 때만 사용
    fixed_amount: int | None  # fee_type=FIXED
    tier_json: list[dict[str, Any]] | None  # fee_type=TIERED: [{min, max?, rate?, fixed?}]
    version: int
    reliability_status: str  # CONFIRMED / ESTIMATED / TODO — 신뢰도 표시 (경고 노출)
    effective_from: date | None = None
    source_type: str = "MANUAL"


@dataclass(slots=True)
class PaymentFeePolicyRow:
    """결제 수수료 정책 (PG 사 수수료, method 별)."""

    id: UUID
    method: str  # BUSINESS_CARD / NAVER_PAY / KAKAO_PAY / ACCOUNT_TRANSFER 등
    rate: Decimal
    version: int
    effective_from: date | None = None
    source_type: str = "MANUAL"
    reliability_status: str = "CONFIRMED"


@dataclass(slots=True)
class TaxPolicyRow:
    """세금 정책 (D-284 확장).

    - calculation_method: VAT 산출 방식 (e.g. STANDARD / SIMPLIFIED)
    - default_vat_inclusion: expense 입력 시 기본값 (INCLUDED/EXCLUDED/EXEMPT)
    - applies_to: 적용 범위 (REVENUE / EXPENSE / BOTH)
    - business_type: SOLE/JOINT/CORP/TEAM 별 차등 (D-280)
    """

    id: UUID
    tax_type: str
    business_type: str | None
    vat_rate: Decimal
    calculation_method: str
    default_vat_inclusion: str
    applies_to: str
    version: int
    effective_from: date | None = None
    source_type: str = "MANUAL"
    reliability_status: str = "CONFIRMED"


@dataclass(slots=True)
class FulfillmentFeePolicyRow:
    """풀필먼트 비용 (로켓그로스 등): 입고/출고/보관/반품처리."""

    id: UUID
    provider: str
    inbound_fee: int | None
    outbound_fee: int | None
    storage_fee_per_day: int | None
    return_handling_fee: int | None
    extra_json: dict[str, Any] | None  # 공급자별 특수 비용 (예: 검수료)
    version: int
    effective_from: date | None = None
    source_type: str = "MANUAL"
    reliability_status: str = "CONFIRMED"


@dataclass(slots=True)
class ReturnRiskPolicyRow:
    """반품 위험 비율 — 예상 마진 산정 시 차감 (sale_price * return_rate)."""

    id: UUID
    marketplace: str | None
    category_path: str | None
    return_rate: Decimal
    version: int
    effective_from: date | None = None
    source_type: str = "MANUAL"
    reliability_status: str = "CONFIRMED"


@dataclass(slots=True)
class AdCostPolicyRow:
    """광고비 정책. target_roas 또는 default_ad_ratio 중 사용 가능한 쪽 선택."""

    id: UUID
    marketplace: str
    category_path: str | None
    target_roas: Decimal | None  # 매출 대비 광고비 목표 (예: 5.0 = ROAS 500%)
    default_ad_ratio: Decimal | None  # sale_price * ratio (단순 비율)
    version: int
    effective_from: date | None = None
    source_type: str = "MANUAL"
    reliability_status: str = "CONFIRMED"


@dataclass(slots=True)
class TargetMarginPolicyRow:
    """목표 마진율 (price_sim 입력). warning_lower_rate 미만이면 경고."""

    id: UUID
    category_path: str | None
    margin_type: str | None  # 일반 / 로켓그로스 / 위탁 등
    target_rate: Decimal
    warning_lower_rate: Decimal | None
    version: int


@dataclass(slots=True)
class PartnerSharePolicyRow:
    """파트너 수익배분 비율 (D-280).

    - share_ratio: 0~1, 파트너 N명의 ratio 합 = 1.0
    - applies_year_month: 특정 월 override (NULL = 기본)
    """

    id: UUID
    partner_id: UUID
    share_ratio: Decimal
    applies_year_month: str | None
    effective_from: date
    version: int


@dataclass(slots=True)
class PolicyContext:
    """정책 조회 컨텍스트 — 어떤 정책을 적용할지 선택하는 기준값.

    at: 적용 시점 (effective_from <= at < effective_to). 과거 재현 시 과거 날짜.
    """

    org_id: UUID
    at: date
    marketplace: str | None = None
    category_path: str | None = None
    margin_type: str | None = None
    payment_method: str | None = None


@dataclass(slots=True)
class PolicySnapshotRef:
    """계산 결과와 함께 저장하는 박제 참조 (policy_table + row_id + version)."""

    policy_table: str
    policy_row_id: UUID
    version: int


@dataclass(slots=True)
class ResolvedPolicies:
    """resolve_active_policies() 결과 — 한 번의 계산에 쓸 정책 묶음.

    tax 만 필수 (기본 부가세). 그 외는 미적용(None) 가능 → 계산 함수가 0 처리.
    snapshot_refs/snapshot_values_json 는 결과 저장 시 함께 박제.
    """

    tax: TaxPolicyRow
    marketplace_fee: MarketplaceFeePolicyRow | None = None
    payment_fee: PaymentFeePolicyRow | None = None
    fulfillment: FulfillmentFeePolicyRow | None = None
    return_risk: ReturnRiskPolicyRow | None = None
    ad_cost: AdCostPolicyRow | None = None
    target_margin: TargetMarginPolicyRow | None = None
    snapshot_refs: list[PolicySnapshotRef] = field(default_factory=list)
    snapshot_values_json: dict[str, Any] = field(default_factory=dict)
