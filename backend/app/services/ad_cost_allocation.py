"""광고비 월별 자동배분 — expenses(MARKETING) → sales_order.ad_cost.

배경 (CLAUDE.md Generalization/Calculation 원칙, 2026-07-29 설계 논의):
  광고비는 두 메커니즘이 서로 안 이어져 있었다.
    - ad_cost_policy(비율 추정)  → 마진계산기 "예상 마진"에만 반영 (사전 예측)
    - expenses(MARKETING 카테고리) → 사업 공통비 풀로 들어가 월정산 순이익에서만 차감 (상품별 귀속 없음)
  sales_order.ad_cost 컬럼(M001)은 이미 있었지만 채우는 로직이 없어 항상 0 이었다.

이 모듈이 하는 일:
  그 달 expense_category_policy.kind='MARKETING' 지출 합계를,
  그 달 sales_order 중 **아직 ad_cost 가 0 인 건들**의 매출 비중으로 나눠서 채운다.
  이미 ad_cost 가 있는 건(마켓 정산 import 등으로 실비가 들어온 건)은 건드리지 않는다
  — "실비 우선, 없는 건만 근사치로 채움" 원칙.

  이렇게 채워진 sales_order.ad_cost 는 services.margin.calculate_actual_margin() 이
  그대로 읽어 실마진(actual margin) 계산에 반영한다.

한계 (알려진 별도 gap, 이번 범위 아님):
  calculate_actual_margin() 의 계산 결과를 product_margin_actual 테이블에 영구
  저장하는 로직이 아직 없다 — services/settlement.py 의 direct_cost 집계는
  product_margin_actual 을 SELECT 하므로, 이 테이블이 채워지기 전까지는 이번
  배분이 월정산 숫자에 자동 반영되지 않는다. 별도 작업으로 필요.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.money import to_krw
from app.core.month_lock import assert_period_writable
from app.services.settlement import month_range


@dataclass(slots=True)
class AdCostAllocationResult:
    year_month: str
    total_marketing_expense: int
    orders_updated: int
    orders_skipped_existing: int


def allocate_ad_cost_for_period(
    org_id: UUID, year_month: str, db: Session
) -> AdCostAllocationResult:
    """그 달 MARKETING 지출을 ad_cost=0 인 sales_order 에 매출비중으로 배분.

    호출자가 commit 책임 (라우터에서 record_audit 과 함께 커밋).
    """
    period_start, period_end = month_range(year_month)
    # 광고비 배분은 sales_order.ad_cost 를 바꾼다 → 잠긴 월에는 실행할 수 없다.
    assert_period_writable(
        db, org_id=org_id, effective_date=period_start,
        operation="ALLOCATE_AD_COST", what="광고비 배분",
    )
    params = {"org": str(org_id), "s": period_start, "e": period_end}

    marketing_row = db.execute(
        text(
            """
            SELECT COALESCE(SUM(e.gross_amount), 0) AS total
              FROM expenses e
              JOIN expense_category_policy c ON c.id = e.category_id
             WHERE e.org_id = :org AND e.deleted_at IS NULL
               AND e.expense_date BETWEEN :s AND :e
               AND c.kind = 'MARKETING'
            """
        ),
        params,
    ).first()
    total_marketing = int(marketing_row.total)

    if total_marketing <= 0:
        return AdCostAllocationResult(year_month, 0, 0, 0)

    orders = db.execute(
        text(
            """
            -- sale_price 는 라인 합계라 quantity 를 곱하지 않는다
            SELECT id, sale_price AS order_revenue, ad_cost
              FROM sales_order
             WHERE org_id = :org AND deleted_at IS NULL
               AND sale_date BETWEEN :s AND :e
            """
        ),
        params,
    ).all()

    zero_ad_orders = [o for o in orders if int(o.ad_cost) == 0]
    zero_ad_revenue = sum(int(o.order_revenue) for o in zero_ad_orders)

    if not zero_ad_orders or zero_ad_revenue <= 0:
        return AdCostAllocationResult(
            year_month, total_marketing, 0, len(orders) - len(zero_ad_orders)
        )

    accumulated = 0
    for idx, o in enumerate(zero_ad_orders):
        if idx == len(zero_ad_orders) - 1:
            amount = total_marketing - accumulated  # 1원 단위 잔수는 마지막 건에 귀속
        else:
            amount = to_krw(
                Decimal(total_marketing) * Decimal(int(o.order_revenue)) / Decimal(zero_ad_revenue)
            )
            accumulated += amount
        db.execute(
            text("UPDATE sales_order SET ad_cost = :a, updated_at = now() WHERE id = :i"),
            {"a": amount, "i": str(o.id)},
        )

    return AdCostAllocationResult(
        year_month,
        total_marketing,
        len(zero_ad_orders),
        len(orders) - len(zero_ad_orders),
    )
