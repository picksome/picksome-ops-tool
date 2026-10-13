"""services/settlement.py 통합 테스트 — CLAUDE.md Settlement Model 핵심 경로.

순이익 = 매출 - 상품 직접비 - 사업 공통비, 우선분배 → 비율분배, 적자 carryover.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from app.core.exceptions import ConflictError, ReopenLimitExceededError
from app.domain.settlement import MonthlySettlementInput
from app.services.settlement import calculate_monthly_settlement, reopen_settlement
from app.tests.fixtures import factories as f


def _basic_sale(db, org_id, product_id, *, sale_date, sale_price, cost, mfee):
    so_id = f.make_sales_order(db, org_id=org_id, product_id=product_id, sale_date=sale_date, sale_price=sale_price)
    f.make_product_margin_actual(
        db, org_id=org_id, product_id=product_id, sales_order_id=so_id,
        actual_revenue=sale_price, actual_cost=cost, actual_marketplace_fee=mfee,
    )
    return so_id


def test_net_profit_and_ratio_distribution_no_remainder(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    _basic_sale(db, org_id, product_id, sale_date=date(2026, 6, 10), sale_price=100_000, cost=40_000, mfee=10_000)
    # revenue=100000, direct_cost=50000 -> net before expense = 50000
    cat_id = f.make_expense_category_policy(db, org_id=org_id)
    f.make_expense(db, org_id=org_id, category_id=cat_id, expense_date=date(2026, 6, 5),
                   gross_amount=10_000, supply_amount=10_000, vat_amount=0)
    # net_profit = 100000 - 50000 - 10000 = 40000

    p1 = f.make_partner(db, org_id=org_id, name="P1")
    p2 = f.make_partner(db, org_id=org_id, name="P2")
    f.make_partner_share_policy(db, org_id=org_id, partner_id=p1, share_ratio=Decimal("0.6"))
    f.make_partner_share_policy(db, org_id=org_id, partner_id=p2, share_ratio=Decimal("0.4"))
    f.make_settlement_policy(db, org_id=org_id)

    result = calculate_monthly_settlement(MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db)

    assert result.total_revenue == 100_000
    assert result.total_direct_cost == 50_000
    assert result.total_common_expense == 10_000
    assert result.net_profit == 40_000
    assert result.carryover_offset_applied == 0
    assert result.distributable_amount == 40_000
    assert sum(d.amount for d in result.distributions) == 40_000
    by_partner = {d.partner_id: d.amount for d in result.distributions}
    assert by_partner[p1] == 24_000
    assert by_partner[p2] == 16_000


def test_ratio_distribution_remainder_not_lost_to_rounding(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    _basic_sale(db, org_id, product_id, sale_date=date(2026, 6, 10), sale_price=10_000, cost=0, mfee=0)
    # net_profit = 10000 (no expenses)

    partners = [f.make_partner(db, org_id=org_id, name=f"P{i}") for i in range(3)]
    for pid in partners:
        f.make_partner_share_policy(db, org_id=org_id, partner_id=pid, share_ratio=Decimal("0.3333"))
    f.make_settlement_policy(db, org_id=org_id)

    result = calculate_monthly_settlement(MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db)

    assert result.distributable_amount == 10_000
    total_distributed = sum(d.amount for d in result.distributions)
    assert total_distributed == 10_000  # 1원도 유실/과다 배분되지 않음
    for d in result.distributions:
        proportional = Decimal(10_000) * d.ratio_used
        assert abs(Decimal(d.amount) - proportional) <= Decimal("1")


def test_priority_distribution_capped_by_distributable(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    _basic_sale(db, org_id, product_id, sale_date=date(2026, 6, 10), sale_price=5_000, cost=0, mfee=0)
    # net_profit = 5000 (no expenses)

    priority_partner = f.make_partner(db, org_id=org_id, name="Priority")
    ratio_partner = f.make_partner(db, org_id=org_id, name="Ratio")
    f.make_partner_share_policy(db, org_id=org_id, partner_id=ratio_partner, share_ratio=Decimal("1.0"))
    f.make_settlement_policy(
        db, org_id=org_id,
        priority_distribution=[{"partner_id": str(priority_partner), "fixed_amount": 100_000}],
    )

    result = calculate_monthly_settlement(MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db)

    priority_rows = [d for d in result.distributions if d.distribution_kind == "PRIORITY"]
    assert len(priority_rows) == 1
    # distributable(5000) 보다 큰 우선분배(100000) 요청 -> distributable 한도로 캡핑.
    assert priority_rows[0].amount == 5_000
    ratio_rows = [d for d in result.distributions if d.distribution_kind == "RATIO"]
    assert sum(r.amount for r in ratio_rows) == 0


def test_carryover_offset_applied_to_profit(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    _basic_sale(db, org_id, product_id, sale_date=date(2026, 6, 10), sale_price=30_000, cost=0, mfee=0)
    # net_profit = 30000

    db.execute(
        text(
            """
            INSERT INTO loss_carryover_ledger (org_id, event_at, event_kind, amount, balance_after)
            VALUES (:org, :at, 'LOSS_ADDED', -20000, 20000)
            """
        ),
        {"org": str(org_id), "at": date(2026, 5, 31)},
    )
    f.make_settlement_policy(db, org_id=org_id)

    result = calculate_monthly_settlement(MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db)

    assert result.net_profit == 30_000
    assert result.carryover_offset_applied == 20_000
    assert result.distributable_amount == 10_000


def test_deficit_month_distributes_nothing_and_warns(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    _basic_sale(db, org_id, product_id, sale_date=date(2026, 6, 10), sale_price=10_000, cost=50_000, mfee=0)
    # net_profit = 10000 - 50000 = -40000
    f.make_settlement_policy(db, org_id=org_id)

    result = calculate_monthly_settlement(MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db)

    assert result.net_profit == -40_000
    assert result.distributable_amount == 0
    assert any("적자" in w for w in result.warnings)


def test_ratio_sum_not_one_raises_conflict(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    _basic_sale(db, org_id, product_id, sale_date=date(2026, 6, 10), sale_price=10_000, cost=0, mfee=0)

    p1 = f.make_partner(db, org_id=org_id, name="P1")
    f.make_partner_share_policy(db, org_id=org_id, partner_id=p1, share_ratio=Decimal("0.5"))
    f.make_settlement_policy(db, org_id=org_id)

    with pytest.raises(ConflictError):
        calculate_monthly_settlement(MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db)


def test_revenue_does_not_multiply_sale_price_by_quantity(db):
    """회귀 방지: sale_price 는 단가가 아니라 라인 합계다 (M011).

    2026-07-30 실데이터 검증에서 `SUM(sale_price * quantity)` 때문에 매출이
    이중 계산되고 있었다 (2025-02 실제 1,256,200원 → 1,817,400원으로 부풀려짐).
    엑셀 `판매 장부` 의 `단순 수익` 이 `판매가+배송비 - 구매비용 - 구매 배송비`
    로 `판매 개수`를 곱하지 않는다는 게 근거다.
    """
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    f.make_sales_order(
        db, org_id=org_id, product_id=product_id,
        sale_date=date(2026, 6, 10), sale_price=30_000, quantity=3,
    )
    f.make_settlement_policy(db, org_id=org_id)

    result = calculate_monthly_settlement(
        MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db
    )

    # quantity 3 이지만 매출은 30,000 그대로여야 한다 (90,000 이면 이중 계산)
    assert result.total_revenue == 30_000


def test_same_marketplace_order_no_allows_multiple_lines(db):
    """회귀 방지: 한 마켓 주문에 여러 라인 허용 (M012).

    주문번호는 마켓이 발급한 값이라 재구매하면 새로 발급된다. 따라서 같은
    주문번호의 여러 행은 한 주문의 여러 라인(다품목 또는 분할배송)이다.
    예전 유니크 제약이 (org, marketplace, order_no) 라서 import 가
    2~3번째 라인을 조용히 버렸다 — 픽썸 실데이터 9행 / 146,440원 유실.
    """
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id)
    for i in range(3):
        f.make_sales_order(
            db, org_id=org_id, product_id=product_id,
            sale_date=date(2026, 6, 10 + i), sale_price=7_900,
            marketplace_order_no="17100128167282",
        )
    f.make_settlement_policy(db, org_id=org_id)

    result = calculate_monthly_settlement(
        MonthlySettlementInput(org_id=org_id, year_month="2026-06"), db
    )

    assert result.total_revenue == 23_700  # 3라인이 전부 살아있어야 한다


def test_reopen_settlement_enforces_limit_and_writes_audit(db):
    org_id = f.make_organization(db, settlement_reopen_limit=1)
    actor = f.make_app_user(db, org_id=org_id)
    ms_id = UUID(int=0)
    row = db.execute(
        text(
            """
            INSERT INTO monthly_settlement (org_id, business_key, year_month, status, reopen_count, confirmed_at, confirmed_by)
            VALUES (:org, 'MS-1', '2026-06', 'CONFIRMED', 0, now(), :actor)
            RETURNING id
            """
        ),
        {"org": str(org_id), "actor": str(actor)},
    ).first()
    ms_id = UUID(str(row[0]))

    reopen_settlement(ms_id, actor, "정정 필요", db)

    audit_row = db.execute(
        text("SELECT reopen_count_after FROM settlement_reopen_audit WHERE monthly_settlement_id = :id"),
        {"id": str(ms_id)},
    ).first()
    assert audit_row.reopen_count_after == 1

    # DB 상 reopen_count 는 1로 갱신되었지만 status 는 REOPENED 로 바뀌어 재-reopen 은
    # "reopen 불가 상태" ConflictError 로 막힘 (CONFIRMED/LOCKED 만 재오픈 대상).
    with pytest.raises(ConflictError):
        reopen_settlement(ms_id, actor, "또 정정", db)


def test_reopen_settlement_raises_reopen_limit_exceeded(db):
    org_id = f.make_organization(db, settlement_reopen_limit=2)
    actor = f.make_app_user(db, org_id=org_id)
    row = db.execute(
        text(
            """
            INSERT INTO monthly_settlement (
              org_id, business_key, year_month, status, reopen_count, confirmed_at, confirmed_by
            )
            VALUES (:org, 'MS-2', '2026-06', 'CONFIRMED', 2, now(), :actor)
            RETURNING id
            """
        ),
        {"org": str(org_id), "actor": str(actor)},
    ).first()
    ms_id = UUID(str(row[0]))

    with pytest.raises(ReopenLimitExceededError):
        reopen_settlement(ms_id, actor, "한도 초과 시도", db)
