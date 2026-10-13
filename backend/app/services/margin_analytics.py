"""실마진 집계 서비스 — 상품별/기간별 실제 마진 vs 예상 마진 비교.

`calculate_actual_margin` (services/margin.py) 은 sales_order 1건 단위 계산이라
리포트/대시보드처럼 기간 합산이 필요한 화면에는 맞지 않는다. 이 모듈은 동일한
MVP 근사 규칙(마켓 정산 미매칭 시 marketplace_fee/fulfillment_fee/return_cost/
payment_fee/vat = 0, cost = variant.cost_amount 우선 후 product.cost_amount)을
기간 합산 버전으로 재사용한다.

사용처: api/v1/reports.py 의 actual-margin / dashboard-summary / monthly-trend /
category-margin / margin-gap-top / margin-drop-alerts, io/excel_export/tax_reports.py
의 by_product_pl.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# 집계 단위: 상품 마스터에 연결됐으면 **product.id**, 아니면 상품명 스냅샷.
# 예전에는 GROUP BY 에 product_snapshot_name 이 무조건 들어가 있어서, 상품을
# 연결해도 판매장부 이름 표기가 조금만 달라지면 같은 상품이 여러 줄로 쪼개졌다
# (연결의 효과가 통째로 사라짐). 연결된 건은 이름을 묶음 키에서 뺀다.
_GROUP_KEY = "COALESCE(p.id::text, 'SNAPSHOT:' || COALESCE(so.product_snapshot_name, '?'))"

_ACTUAL_SQL = f"""
    SELECT {_GROUP_KEY} AS product_id,
           COALESCE(MAX(p.name_ko), MAX(so.product_snapshot_name), '상품 미매칭') AS product_name,
           COALESCE(MAX(p.sku), '') AS sku,
           MAX(p.category_path) AS category_path,
           -- sale_price / purchase_cost 는 라인 합계라 quantity 를 곱하지 않는다.
           -- cost_amount(상품 마스터 단가)로 대체할 때만 곱한다 (M011/M013).
           -- purchase_cost 는 NULL(미상)일 때만 대체 — 0 은 실제 0원이다 (M014).
           COALESCE(SUM(so.sale_price), 0) AS gross_revenue,
           -- **원가를 모르는 줄은 더하지 않는다.**
           --
           -- 예전에는 `COALESCE(p.cost_amount, 0)` 으로 0원을 더했다. 그러면
           -- 원가 미상 상품이 원가 0원짜리로 집계돼 **마진이 100% 로 보인다.**
           -- 상품별 손익표에서 그건 "이 상품이 제일 남는다" 로 읽힌다.
           --
           -- 아래 `unknown_cost_lines` 가 0 이 아니면 이 묶음의 원가·마진은
           -- 불완전하다. 호출자가 숫자를 내놓지 않고 "확인 불가" 로 표시한다.
           COALESCE(SUM(
             CASE WHEN so.purchase_cost IS NOT NULL THEN so.purchase_cost + so.shipping_fee
                  WHEN sv.cost_amount IS NOT NULL THEN sv.cost_amount * so.quantity
                  WHEN p.cost_amount IS NOT NULL THEN p.cost_amount * so.quantity
                  ELSE 0 END
           ), 0) AS known_cost,
           COUNT(*) FILTER (
             WHERE so.purchase_cost IS NULL
               AND sv.cost_amount IS NULL
               AND p.cost_amount IS NULL
           ) AS unknown_cost_lines,
           COALESCE(SUM(so.ad_cost), 0) AS total_ad_cost
      FROM sales_order so
      LEFT JOIN product p ON p.id = so.product_id
      LEFT JOIN product_variant sv ON sv.id = so.product_variant_id
     WHERE so.org_id = :o AND so.deleted_at IS NULL
       AND so.sale_date BETWEEN :s AND :e
     GROUP BY {_GROUP_KEY}
"""

_EXPECTED_SQL = """
    SELECT DISTINCT ON (product_id) product_id,
           computed_margin_amount, computed_margin_rate
      FROM product_margin_expected
     WHERE org_id = :o AND deleted_at IS NULL AND valid_from <= :e
     ORDER BY product_id, valid_from DESC, created_at DESC
"""


def aggregate_actual_margin(
    db: Session, *, org_id: UUID, start: date, end: date
) -> list[dict[str, Any]]:
    """기간 내 상품별 실 마진(대략치) + 최근 예상 마진 비교 리스트.

    실 마진 = 매출 - 원가 - 광고비 (정산 미매칭 시 마켓수수료/풀필먼트/반품비/VAT는
    calculate_actual_margin 과 동일하게 0 처리 — 새 근사가 아니라 기존 MVP 규칙 재사용).
    """
    actual_rows = db.execute(
        text(_ACTUAL_SQL), {"o": str(org_id), "s": start, "e": end}
    ).all()
    expected_rows = db.execute(text(_EXPECTED_SQL), {"o": str(org_id), "e": end}).all()
    expected_by_product = {str(r.product_id): r for r in expected_rows}

    items: list[dict[str, Any]] = []
    for r in actual_rows:
        revenue = int(r.gross_revenue)
        unknown_lines = int(r.unknown_cost_lines)
        cost_complete = unknown_lines == 0
        cost = int(r.known_cost) + int(r.total_ad_cost)

        # 원가를 모르는 줄이 하나라도 있으면 **마진을 숫자로 내놓지 않는다.**
        # 0 을 더해 계산하면 과대계상이고, 빼고 계산하면 과소계상이다. 둘 다
        # 틀린 숫자이므로 "모른다" 가 유일하게 정직한 답이다.
        if cost_complete and revenue > 0:
            actual_margin_amount: int | None = revenue - cost
            actual_margin_rate: float | None = float(
                (Decimal(revenue - cost) / Decimal(revenue))
                .quantize(Decimal("0.0001")))
        elif cost_complete:
            # 매출이 0 이면 비율은 정의되지 않는다. 금액은 알 수 있다.
            actual_margin_amount = revenue - cost
            actual_margin_rate = None
        else:
            actual_margin_amount = None
            actual_margin_rate = None

        exp = expected_by_product.get(r.product_id)
        expected_margin_amount = int(exp.computed_margin_amount) if exp else None
        expected_margin_rate = float(exp.computed_margin_rate) if exp else None

        # 실 마진을 모르면 차이도 모른다. 0 으로 두면 "예상과 같다" 로 읽힌다.
        delta_amount = (
            actual_margin_amount - expected_margin_amount
            if exp and actual_margin_amount is not None else None
        )
        delta_rate = (
            actual_margin_rate - expected_margin_rate
            if exp and actual_margin_rate is not None else None
        )

        items.append(
            {
                "product_id": str(r.product_id),
                "product_name": r.product_name,
                "sku": r.sku,
                "category_path": r.category_path,
                "gross_revenue": revenue,
                # 아는 것만 더한 값이다. `cost_complete=false` 면 실제 원가는
                # 이보다 크다 — 화면이 이 숫자를 총원가로 쓰면 안 된다.
                "total_cost": cost,
                "cost_complete": cost_complete,
                "unknown_cost_lines": unknown_lines,
                "actual_margin_amount": actual_margin_amount,
                "actual_margin_rate": actual_margin_rate,
                "expected_margin_amount": expected_margin_amount,
                "expected_margin_rate": expected_margin_rate,
                "delta_amount": delta_amount,
                "delta_rate": delta_rate,
            }
        )
    return items
