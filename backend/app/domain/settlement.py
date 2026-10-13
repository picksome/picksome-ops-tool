"""월별 정산 입출력 도메인.

CLAUDE.md Settlement Model:
    순이익 = 매출 - 상품 직접비 - 사업 공통비
    분배 = 우선분배(정액) → 잔여 × 비율(N명 합=1.0)
    적자월: carryover 누적 → 다음 흑자월에서 우선 차감

services/settlement.py 의 순수 함수가 사용. DB 의존 없음.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from app.domain.policy import PolicySnapshotRef


@dataclass(slots=True)
class MonthlySettlementInput:
    """월정산 계산 입력.

    - year_month: 'YYYY-MM' (Asia/Seoul 기준)
    - overrides: 임시 비율 변경 등 (UI 에서 시뮬레이션 시 사용)
    """

    org_id: UUID
    year_month: str
    overrides: dict[str, Any] | None = None


@dataclass(slots=True)
class PartnerDistributionRow:
    """파트너 1명의 분배내역.

    - PRIORITY: 우선분배 (정액 — settlement_policy.priority_distribution)
    - RATIO: 비율분배 (partner_share_policy.share_ratio 적용)
    """

    partner_id: UUID
    distribution_kind: Literal["PRIORITY", "RATIO"]
    amount: int
    ratio_used: Decimal | None = None
    note: str | None = None


@dataclass(slots=True)
class MonthlySettlementResult:
    """월정산 결과.

    - carryover_offset_applied: 이번 월에 적용된 누적 손실 차감액 (>=0)
    - distributable_amount: 분배 대상 = max(0, net_profit - carryover_offset)
    """

    year_month: str
    total_revenue: int
    total_direct_cost: int
    total_common_expense: int
    net_profit: int
    carryover_offset_applied: int
    distributable_amount: int
    distributions: list[PartnerDistributionRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    snapshot_refs: list[PolicySnapshotRef] = field(default_factory=list)
