"""대시보드 집계 (Phase 45).

이 파일이 고정하는 것
------------------
1. 진행 중인 달은 live 집계, 확정한 달은 snapshot — **source 가 값에 붙어 나간다**
2. 확정 후 raw 판매가 바뀌어도 대시보드의 그 달 숫자는 변하지 않는다
3. 정산 미연결은 0원이 아니라 `NOT_CONNECTED` 다
4. source 가 다른 두 달을 비교할 때 `comparable=False` 로 알린다
5. 판매처 분해 합계가 KPI 총매출과 어긋나지 않는다
6. 경고는 `operation_issue` 에서 오고 대시보드가 조건을 다시 계산하지 않는다
7. 워터폴에 실제 컬럼이 아닌 항목(예: 지어낸 세금)이 없다
8. 현금흐름 카드는 **선택한 달을 따라가지 않는다** — 손익과 현금을 섞지 않는다
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import dashboard as dash
from app.tests.fixtures.factories import (
    confirm_month,
    make_app_user,
    make_expense,
    make_expense_category_policy,
    make_organization,
    make_product,
    make_product_margin_actual,
    make_sales_order,
)

YM = "2026-04"
PREV_YM = "2026-03"


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _sale(
    db: Session, org: uuid.UUID, product: uuid.UUID, *,
    day: int, price: int, cost: int = 0, marketplace: str = "COUPANG",
    marketplace_fee: int = 0, year_month: str = YM,
) -> uuid.UUID:
    """판매 1건 + 그 건의 실마진 행. 대시보드는 두 테이블을 함께 읽는다."""
    y, m = int(year_month[:4]), int(year_month[5:7])
    so = make_sales_order(
        db, org_id=org, product_id=product, sale_date=date(y, m, day),
        sale_price=price, marketplace=marketplace,
    )
    make_product_margin_actual(
        db, org_id=org, product_id=product, sales_order_id=so,
        actual_revenue=price, actual_cost=cost,
        actual_marketplace_fee=marketplace_fee,
    )
    return so


def _month_row(db: Session, org: uuid.UUID, year_month: str) -> uuid.UUID:
    """확정 대상이 되려면 monthly_settlement 행이 있어야 한다."""
    ms_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO monthly_settlement
          (id, org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense,
           net_profit, carryover_offset)
        VALUES (:id, :org, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0, 0)
    """), {"id": str(ms_id), "org": str(org), "bk": f"MS-{ms_id}", "ym": year_month})
    return ms_id


# ──────────────────────────────────────────────────────────────────────────
# source 구분
# ──────────────────────────────────────────────────────────────────────────

def test_open_month_is_live_and_confirmed_month_is_snapshot(
    db: Session, org: uuid.UUID, actor: uuid.UUID
) -> None:
    """같은 대시보드 안에서도 달마다 source 가 다르다."""
    p = make_product(db, org_id=org)
    _sale(db, org, p, day=3, price=100_000, cost=60_000, year_month=YM)
    _month_row(db, org, YM)

    live = dash.month_figures(db, org_id=org, year_month=YM)
    assert live["source"] == dash.SOURCE_LIVE
    assert live["close_state"] == "OPEN"
    assert live["figures"]["gross_sales"] == 100_000
    assert live["profit_label"] == "잠정 순이익 (미확정)"

    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    confirmed = dash.month_figures(db, org_id=org, year_month=YM)
    assert confirmed["source"] == dash.SOURCE_SNAPSHOT
    assert confirmed["snapshot_generation"] == 1
    assert confirmed["figures"]["gross_sales"] == 100_000
    # 정산이 연결되지 않았으므로 그냥 "확정 순이익" 이라고 부르지 않는다.
    assert "정산 미연결" in confirmed["profit_label"]


def test_confirmed_month_does_not_move_when_raw_sales_change(
    db: Session, org: uuid.UUID, actor: uuid.UUID
) -> None:
    """확정한 달의 대시보드 숫자는 raw 데이터가 바뀌어도 그대로다.

    이게 깨지면 "4월 확정 순이익" 이 어제와 오늘 다르다 — 확정이 아니게 된다.
    """
    p = make_product(db, org_id=org)
    _sale(db, org, p, day=3, price=100_000, cost=60_000)
    _month_row(db, org, YM)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    before = dash.month_figures(db, org_id=org, year_month=YM)["figures"]["gross_sales"]

    # 확정 후 판매가 한 건 더 들어왔다(늦게 도착한 주문 등).
    _sale(db, org, p, day=20, price=555_000, cost=100_000)

    after = dash.month_figures(db, org_id=org, year_month=YM)
    assert after["figures"]["gross_sales"] == before == 100_000
    assert after["source"] == dash.SOURCE_SNAPSHOT


def test_trend_point_carries_its_own_source(
    db: Session, org: uuid.UUID, actor: uuid.UUID
) -> None:
    """트렌드 선 하나에 두 source 가 섞이면, 각 점이 자기 출처를 말해야 한다."""
    p = make_product(db, org_id=org)
    _sale(db, org, p, day=5, price=200_000, cost=100_000, year_month=PREV_YM)
    _sale(db, org, p, day=5, price=300_000, cost=100_000, year_month=YM)
    _month_row(db, org, PREV_YM)
    confirm_month(db, org_id=org, year_month=PREV_YM, actor_user_id=actor)

    trend = dash.build_trend(db, org_id=org, end_year_month=YM, months=2)
    by_ym = {t["year_month"]: t for t in trend}

    assert by_ym[PREV_YM]["source"] == dash.SOURCE_SNAPSHOT
    assert by_ym[YM]["source"] == dash.SOURCE_LIVE


def test_comparison_flags_mixed_sources(
    db: Session, org: uuid.UUID, actor: uuid.UUID
) -> None:
    """진행월(live)을 확정월(snapshot)과 나란히 놓을 때 그 사실을 밝힌다."""
    p = make_product(db, org_id=org)
    _sale(db, org, p, day=5, price=200_000, cost=100_000, year_month=PREV_YM)
    _sale(db, org, p, day=5, price=300_000, cost=100_000, year_month=YM)
    _month_row(db, org, PREV_YM)
    confirm_month(db, org_id=org, year_month=PREV_YM, actor_user_id=actor)

    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=2)
    assert data["comparison"]["comparable"] is False
    assert data["comparison"]["note"]

    for kpi in data["kpis"]:
        if kpi["key"] == "gross_sales":
            assert kpi["comparable"] is False
            assert kpi["source"] == dash.SOURCE_LIVE
            assert kpi["previous_source"] == dash.SOURCE_SNAPSHOT


# ──────────────────────────────────────────────────────────────────────────
# 없는 값과 0원의 구분
# ──────────────────────────────────────────────────────────────────────────

def test_unconnected_settlement_is_null_not_zero(
    db: Session, org: uuid.UUID
) -> None:
    """정산이 연결되지 않은 달의 정산액은 0원이 아니라 '모른다' 다."""
    p = make_product(db, org_id=org)
    _sale(db, org, p, day=3, price=100_000, cost=60_000)

    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)
    kpi = next(k for k in data["kpis"] if k["key"] == "settlement_amount")

    assert kpi["value"] is None
    assert kpi["availability"] == dash.NOT_CONNECTED
    assert data["period"]["settlement_connected"] is False
    assert data["period"]["profit_basis"] == "SALES_AND_EXPENSE_ONLY"


def test_margin_rate_is_null_when_there_are_no_sales(
    db: Session, org: uuid.UUID
) -> None:
    """매출이 0인 달의 마진율은 0%가 아니다 — 계산할 수 없는 값이다."""
    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)
    kpi = next(k for k in data["kpis"] if k["key"] == "margin_rate")

    assert kpi["value"] is None
    assert kpi["availability"] == dash.NOT_CONNECTED


# ──────────────────────────────────────────────────────────────────────────
# 정합성
# ──────────────────────────────────────────────────────────────────────────

def test_marketplace_breakdown_sums_to_gross_sales(
    db: Session, org: uuid.UUID
) -> None:
    """판매처별 매출 합 = KPI 총매출. 어긋나면 두 숫자 중 하나는 거짓말이다."""
    p = make_product(db, org_id=org)
    _sale(db, org, p, day=3, price=100_000, cost=60_000, marketplace="COUPANG")
    _sale(db, org, p, day=4, price=50_000, cost=20_000, marketplace="SMARTSTORE")
    _sale(db, org, p, day=5, price=30_000, cost=10_000, marketplace="COUPANG")

    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)
    gross = next(k for k in data["kpis"] if k["key"] == "gross_sales")["value"]

    assert gross == 180_000
    assert sum(m["gross_sales"] for m in data["by_marketplace"]) == gross
    assert {m["marketplace"] for m in data["by_marketplace"]} == {"COUPANG", "SMARTSTORE"}


def test_waterfall_only_contains_real_columns(
    db: Session, org: uuid.UUID
) -> None:
    """워터폴 항목은 전부 집계 가능한 컬럼이다.

    예전 mock 은 '예상 세금' 을 매출의 3%로 지어냈다. 그런 컬럼은 없다.
    """
    p = make_product(db, org_id=org)
    _sale(db, org, p, day=3, price=100_000, cost=60_000, marketplace_fee=10_000)

    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)
    steps = data["waterfall"]

    assert steps[0]["key"] == "gross_sales" and steps[0]["kind"] == "start"
    assert steps[-1]["key"] == "net_profit" and steps[-1]["kind"] == "total"

    figures = dash.month_figures(db, org_id=org, year_month=YM)["figures"]
    for step in steps:
        assert step["key"] in figures, f"{step['key']} 는 집계 컬럼이 아니다"

    deductions = sum(s["amount"] for s in steps if s["kind"] == "deduction")
    assert steps[0]["amount"] - deductions == steps[-1]["amount"]


def test_expense_breakdown_uses_same_filter_as_figures(
    db: Session, org: uuid.UUID
) -> None:
    """비용 구성 합 = KPI 사업비용. 정산 비대상 비용은 양쪽 모두에서 빠진다."""
    cat = make_expense_category_policy(db, org_id=org, name="광고선전비")
    make_expense(
        db, org_id=org, category_id=cat, expense_date=date(2026, 4, 10),
        gross_amount=110_000, supply_amount=100_000, vat_amount=10_000,
    )
    # 정산 대상이 아닌 비용 — 어느 쪽에도 잡히면 안 된다.
    make_expense(
        db, org_id=org, category_id=cat, expense_date=date(2026, 4, 11),
        gross_amount=999_000, supply_amount=999_000, vat_amount=0,
        is_settlement_deductible=False,
    )

    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)
    kpi = next(k for k in data["kpis"] if k["key"] == "business_expenses")

    assert kpi["value"] == 110_000
    assert sum(c["amount"] for c in data["expense_breakdown"]) == 110_000


# ──────────────────────────────────────────────────────────────────────────
# 경고
# ──────────────────────────────────────────────────────────────────────────

def test_warnings_come_from_operation_issues(
    db: Session, org: uuid.UUID, actor: uuid.UUID
) -> None:
    """대시보드는 이슈 조건을 다시 계산하지 않고 이슈 센터 결과를 읽는다."""
    from app.services import operation_issue

    operation_issue.create_manual_issue(
        db, org_id=org, actor_user_id=actor,
        issue_type="MANUAL_CHECK", severity="BLOCKING", title="확인 필요",
    )

    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)
    w = data["warnings"]

    assert w["blocking"] == 1
    assert w["open_total"] == 1
    assert any(t["issue_type"] == "MANUAL_CHECK" for t in w["by_type"])


def test_never_scanned_is_distinguished_from_no_issues(
    db: Session, org: uuid.UUID
) -> None:
    """스캔한 적 없는 것과 문제가 없는 것은 다르다."""
    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)

    assert data["warnings"]["open_total"] == 0
    assert data["warnings"]["never_scanned"] is True


# ──────────────────────────────────────────────────────────────────────────
# 조직 격리
# ──────────────────────────────────────────────────────────────────────────

def test_dashboard_does_not_leak_other_org_numbers(db: Session, org: uuid.UUID) -> None:
    other = make_organization(db)
    other_product = make_product(db, org_id=other)
    _sale(db, other, other_product, day=3, price=9_999_000, cost=0)

    data = dash.build_dashboard(db, org_id=org, year_month=YM, trend_months=1)

    assert next(k for k in data["kpis"] if k["key"] == "gross_sales")["value"] == 0
    assert data["by_marketplace"] == []
    assert data["margin_distribution"] == []


def test_available_months_includes_current_month(db: Session, org: uuid.UUID) -> None:
    """데이터가 하나도 없어도 이번 달은 선택할 수 있어야 한다."""
    months = dash.available_months(db, org_id=org)
    assert dash.current_year_month() in months


# ──────────────────────────────────────────────────────────────────────────
# 현금흐름 카드 (Phase 7)
#
# docs/54 §1: 손익과 현금흐름을 합치지 않는다. 대시보드의 나머지는 "그 달에
# 얼마를 벌었나" 를 묻고, 이 카드는 "앞으로 통장이 버티나" 를 묻는다.
# ──────────────────────────────────────────────────────────────────────────

def test_cashflow_card_does_not_follow_the_selected_month(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """과거 달을 보는 중에도 현금흐름은 오늘부터 앞을 본다.

    선택 월을 따라가게 만들면 "4월 현금흐름" 처럼 보이는데, 그 달의 현금 사실은
    지금 어디에도 없다(은행 연동 없음). 기준을 `basis` 로 명시한다.
    """
    card = dash.build_dashboard(db, org_id=org, year_month=YM, viewer_role="ADMIN")["cashflow"]

    assert card["basis"] == "오늘부터 30일"
    assert card["range"]["from"] >= date.today().isoformat()
    assert card["range"]["from"] > YM  # 선택 월 범위가 아니다


def test_cashflow_card_reports_unknown_balance_rather_than_zero(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """기준 잔액이 없으면 잔액은 `None` 이다. 0 이면 "통장이 비었다" 로 읽힌다."""
    card = dash.build_dashboard(db, org_id=org, year_month=YM, viewer_role="ADMIN")["cashflow"]

    assert card["balance_available"] is False
    assert card["projected_ending_balance"] is None
    # 합계는 0 이 맞다 — 유입·유출이 실제로 없다. "모른다" 와 구분된다.
    assert card["inflow"]["actual"] == 0


def test_cashflow_card_never_counts_revenue_as_cash(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """매출이 있어도 현금 유입은 0이다 — 이 대시보드에서 가장 위험한 착각이다."""
    product = make_product(db, org_id=org)
    _sale(db, org, product, day=5, price=30_000_000, cost=10_000_000)

    board = dash.build_dashboard(db, org_id=org, year_month=YM, viewer_role="ADMIN")
    # 손익 쪽에는 매출이 잡힌다.
    assert board["waterfall"][0]["amount"] == 30_000_000
    # 현금 쪽에는 아무것도 없다.
    assert board["cashflow"]["inflow"] == {"expected": 0, "confirmed": 0, "actual": 0}


def test_cashflow_card_flags_the_missing_inflow_source(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """설명 없는 "실제 유입 0원" 은 장부가 틀린 것처럼 보인다."""
    card = dash.build_dashboard(db, org_id=org, year_month=YM, viewer_role="ADMIN")["cashflow"]
    assert card["actual_inflow_source_available"] is False


def test_the_cashflow_card_is_withheld_from_roles_that_cannot_see_cashflow(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """대시보드가 `/cashflow` 의 뒷문이 되면 안 된다.

    대시보드는 OPERATOR·READONLY 도 읽는다. 카드를 그냥 넣으면 `/cashflow` 가
    403 인 사람에게 예상 잔액·미지급액이 그대로 나간다 — 권한을 라우터 한 곳에서만
    막은 셈이 되고, 그건 막지 않은 것과 같다.
    """
    for role in ("OPERATOR", "READONLY", "SOURCING", "EXTERNAL_ACCOUNTANT"):
        board = dash.build_dashboard(db, org_id=org, year_month=YM, viewer_role=role)
        assert "cashflow" not in board, f"{role} 에게 현금흐름 카드가 나갔습니다."

    for role in ("ADMIN", "ACCOUNTING"):
        assert "cashflow" in dash.build_dashboard(
            db, org_id=org, year_month=YM, viewer_role=role
        )


def test_an_unknown_role_gets_no_cashflow_card(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """역할을 모르면 안 준다 — 모를 때 주는 쪽으로 기울면 그게 곧 구멍이 된다."""
    assert "cashflow" not in dash.build_dashboard(db, org_id=org, year_month=YM)
