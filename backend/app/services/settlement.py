"""월별 정산 서비스.

CLAUDE.md Settlement Model 구현. 매출이 아닌 **순이익** 기준 분배가 원칙.

핵심 산식 (docs/22_api_spec.md §4-4):
  revenue       = Σ sales_order.sale_price (org+period)
                  ※ sale_price 는 엑셀 `판매가+배송비` 를 그대로 옮긴 **라인 합계**다.
                    단가가 아니므로 quantity 를 곱하지 않는다.
  direct_cost   = Σ product_margin_actual (cost+shipping+mfee+pfee+ad+fulfill+ret)
  common_exp    = Σ expenses (is_settlement_deductible AND is_business_use)
  net_profit    = revenue - direct_cost - common_exp
  offset        = min(net_profit, carryover_balance) if net_profit > 0 else 0
  distributable = net_profit - offset            (적자월이면 0, 손실은 carryover 누적)
  → priority distribution (정액)
  → ratio distribution (잔액 × share_ratio, 잔수는 마지막 파트너에게 귀속)

D-285 REOPEN: 한도 기본 3회 (org.settlement_reopen_limit 또는 ms.reopen_limit_override).
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ReopenLimitExceededError
from app.core.money import to_krw
from app.domain.settlement import (
    MonthlySettlementInput,
    MonthlySettlementResult,
    PartnerDistributionRow,
)


def month_range(year_month: str) -> tuple[date, date]:
    """'YYYY-MM' → (first_day, last_day) inclusive."""
    y, m = year_month.split("-")
    yi, mi = int(y), int(m)
    last_day = calendar.monthrange(yi, mi)[1]
    return date(yi, mi, 1), date(yi, mi, last_day)


def calculate_monthly_settlement(
    input: MonthlySettlementInput, db: Session
) -> MonthlySettlementResult:
    """월정산 계산 (preview/저장 공용). 저장은 services/settlement_crud.py.

    의사코드:
        1. month_range → period_start/end (Asia/Seoul 기준)
        2. revenue (sales_order) / direct_cost (margin_actual) / common_exp (expenses)
        3. net_profit = revenue - direct_cost - common_exp
        4. carryover_balance 조회 → 흑자월이면 min() 차감
        5. settlement_policy.priority_distribution_json → PRIORITY 분배
        6. partner_share_policy → RATIO 분배 (합=1.0 검증, 마지막에 잔수 귀속)
    """
    period_start, period_end = month_range(input.year_month)
    warnings: list[str] = []

    # 1) 매출 — sale_price 는 라인 합계이므로 quantity 를 곱하지 않는다
    revenue_row = db.execute(
        text(
            """
            SELECT COALESCE(SUM(sale_price), 0) AS r
              FROM sales_order
             WHERE org_id = :org AND deleted_at IS NULL
               AND sale_date BETWEEN :s AND :e
            """
        ),
        {"org": str(input.org_id), "s": period_start, "e": period_end},
    ).first()
    revenue = int(revenue_row.r)

    # 2) 상품 직접비 (실 마진 합)
    direct_row = db.execute(
        text(
            """
            -- CLAUDE.md 상품 직접비 = 원가 + 배송비 + 마켓수수료 + 광고비
            --                        + 반품비 + 풀필먼트비.
            -- actual_vat 는 제외 — 부가세는 공통비/세무 쪽에서 별도로 다룬다.
            SELECT COALESCE(SUM(
                actual_cost + actual_shipping_cost
              + actual_marketplace_fee + actual_payment_fee
              + actual_ad_cost + actual_fulfillment_fee + actual_return_cost
            ), 0) AS c
              FROM product_margin_actual pma
              JOIN sales_order so ON so.id = pma.sales_order_id
             WHERE so.org_id = :org AND so.deleted_at IS NULL
               AND so.sale_date BETWEEN :s AND :e
            """
        ),
        {"org": str(input.org_id), "s": period_start, "e": period_end},
    ).first()
    direct_cost = int(direct_row.c)

    # 실 마진 누락 sales_order 카운트
    miss_row = db.execute(
        text(
            """
            SELECT COUNT(*) AS n FROM sales_order so
             WHERE so.org_id = :org AND so.deleted_at IS NULL
               AND so.sale_date BETWEEN :s AND :e
               AND NOT EXISTS (SELECT 1 FROM product_margin_actual pma WHERE pma.sales_order_id = so.id)
            """
        ),
        {"org": str(input.org_id), "s": period_start, "e": period_end},
    ).first()
    if int(miss_row.n) > 0:
        warnings.append(f"실 마진 미계산 sales_order {miss_row.n}건 — direct_cost 가 누락됨")

    # 3) 공통 사업비
    common_row = db.execute(
        text(
            """
            SELECT COALESCE(SUM(gross_amount), 0) AS c
              FROM expenses
             WHERE org_id = :org AND deleted_at IS NULL
               AND expense_date BETWEEN :s AND :e
               AND is_settlement_deductible = TRUE
               AND is_business_use = TRUE
            """
        ),
        {"org": str(input.org_id), "s": period_start, "e": period_end},
    ).first()
    common_expense = int(common_row.c)

    net_profit = revenue - direct_cost - common_expense

    # 4) carryover offset
    bal_row = db.execute(
        text(
            """
            SELECT COALESCE((
              SELECT balance_after FROM loss_carryover_ledger
               WHERE org_id = :org AND event_at <= :asof AND deleted_at IS NULL
               ORDER BY event_at DESC, id DESC LIMIT 1
            ), 0) AS bal
            """
        ),
        {"org": str(input.org_id), "asof": period_end},
    ).first()
    carryover_balance = int(bal_row.bal)

    offset_applied = 0
    if net_profit > 0 and carryover_balance > 0:
        offset_applied = min(net_profit, carryover_balance)
        distributable = net_profit - offset_applied
    elif net_profit < 0:
        distributable = 0
        warnings.append(f"적자 {abs(net_profit)}원 — carryover 에 추가 예정")
    else:
        distributable = net_profit

    # 5) priority + 6) ratio
    distributions: list[PartnerDistributionRow] = []
    priority_total = 0

    settle_policy = db.execute(
        text(
            """
            SELECT priority_distribution_json FROM settlement_policy
             WHERE org_id = :org AND is_active = TRUE AND deleted_at IS NULL
               AND effective_from <= :asof AND (effective_to IS NULL OR effective_to >= :asof)
             ORDER BY effective_from DESC LIMIT 1
            """
        ),
        {"org": str(input.org_id), "asof": period_end},
    ).first()
    pri_entries = list(settle_policy.priority_distribution_json or []) if settle_policy else []

    for entry in pri_entries:
        amount = min(int(entry.get("fixed_amount", 0)), max(distributable - priority_total, 0))
        if amount <= 0:
            continue
        distributions.append(
            PartnerDistributionRow(
                partner_id=UUID(str(entry["partner_id"])),
                distribution_kind="PRIORITY",
                amount=amount,
            )
        )
        priority_total += amount

    remaining = distributable - priority_total

    # ratio distribution
    ratio_rows = db.execute(
        text(
            """
            SELECT partner_id, share_ratio FROM partner_share_policy
             WHERE org_id = :org AND is_active = TRUE AND deleted_at IS NULL
               AND effective_from <= :asof AND (effective_to IS NULL OR effective_to >= :asof)
               AND (applies_year_month IS NULL OR applies_year_month = :ym)
             ORDER BY (applies_year_month IS NOT NULL) DESC, effective_from DESC
            """
        ),
        {"org": str(input.org_id), "asof": period_end, "ym": input.year_month},
    ).all()

    # ORDER BY applies_year_month NOT NULL DESC: 월 override row 가 default 보다 먼저 옴.
    # → 같은 partner 의 첫 hit (= override) 만 채택, 이후 default 는 무시.
    seen: dict[UUID, Decimal] = {}
    for r in ratio_rows:
        pid = UUID(str(r.partner_id))
        if pid not in seen:
            seen[pid] = Decimal(str(r.share_ratio))

    # 비율 합 검증 — 0.05% 오차 허용 (정책 입력 시 반올림 여유).
    total_ratio = sum(seen.values(), Decimal(0))
    if seen and (total_ratio - Decimal(1)).copy_abs() > Decimal("0.0005"):
        raise ConflictError(
            f"partner_share_policy 합계 = {total_ratio} (1.0 아님) — 정산 거부",
            detail={"sum": str(total_ratio)},
        )

    # 1원 단위 잔수는 마지막 파트너에게 귀속 (Σ amount = remaining 보장).
    ratio_distributions: list[PartnerDistributionRow] = []
    accumulated = 0
    items = list(seen.items())
    for idx, (pid, ratio) in enumerate(items):
        if idx == len(items) - 1:
            amount = remaining - accumulated
        else:
            amount = to_krw(Decimal(remaining) * ratio)
            accumulated += amount
        ratio_distributions.append(
            PartnerDistributionRow(
                partner_id=pid,
                distribution_kind="RATIO",
                amount=amount,
                ratio_used=ratio,
            )
        )
    distributions.extend(ratio_distributions)

    return MonthlySettlementResult(
        year_month=input.year_month,
        total_revenue=revenue,
        total_direct_cost=direct_cost,
        total_common_expense=common_expense,
        net_profit=net_profit,
        carryover_offset_applied=offset_applied,
        distributable_amount=distributable,
        distributions=distributions,
        warnings=warnings,
    )


def reopen_settlement(
    settlement_id: UUID,
    actor_user_id: UUID,
    reason: str,
    db: Session,
) -> None:
    """D-285: REOPEN 횟수 제한 검증 + settlement_reopen_audit 기록.

    의사코드:
        effective_limit = ms.reopen_limit_override ?? organization.settlement_reopen_limit
        if reopen_count >= effective_limit: REOPEN_LIMIT_EXCEEDED (409)
        else: status=REOPENED, reopen_count+1, audit 1행 INSERT
    """
    row = db.execute(
        text(
            """
            SELECT ms.id, ms.org_id, ms.status, ms.reopen_count,
                   COALESCE(ms.reopen_limit_override, o.settlement_reopen_limit) AS effective_limit
              FROM monthly_settlement ms
              JOIN organization o ON o.id = ms.org_id
             WHERE ms.id = :id
            """
        ),
        {"id": str(settlement_id)},
    ).first()
    if row is None:
        raise ConflictError("settlement not found")
    if row.status not in ("CONFIRMED", "LOCKED"):
        raise ConflictError(f"reopen 불가 상태: {row.status}")
    if int(row.reopen_count) >= int(row.effective_limit):
        raise ReopenLimitExceededError(
            f"REOPEN 한도 {row.effective_limit}회 초과",
            detail={"reopen_count": int(row.reopen_count), "limit": int(row.effective_limit)},
        )

    new_count = int(row.reopen_count) + 1
    db.execute(
        text(
            """
            UPDATE monthly_settlement
               SET status = 'REOPENED', reopen_count = :n,
                   last_reopened_at = now(), last_reopened_by = :uid,
                   last_reopen_reason = :reason
             WHERE id = :id
            """
        ),
        {"n": new_count, "uid": str(actor_user_id), "reason": reason, "id": str(settlement_id)},
    )
    db.execute(
        text(
            """
            INSERT INTO settlement_reopen_audit (
              monthly_settlement_id, reopened_by, reason, previous_status, reopen_count_after
            ) VALUES (:sid, :uid, :reason, :prev, :n)
            """
        ),
        {
            "sid": str(settlement_id),
            "uid": str(actor_user_id),
            "reason": reason,
            "prev": row.status,
            "n": new_count,
        },
    )
