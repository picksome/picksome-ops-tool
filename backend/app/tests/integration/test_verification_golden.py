"""Golden Sample — 사람이 승인한 기준값과 회귀.

이 파일이 지키는 것 하나
--------------------
**현재 계산 결과를 기준값으로 만들 수 있는 경로가 없어야 한다.**

그 경로가 하나라도 있으면 회귀 검사는 계산이 어떻게 망가지든 통과한다.
아래 테스트들은 그 경로가 없다는 것과, 기준값이 사람 손을 거쳐야만
저장된다는 것을 고정한다.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services import verification_golden as golden
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_sales_order,
)

SALE_DATE = date(2026, 1, 15)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def order(db: Session, org) -> uuid.UUID:
    product = make_product(db, org_id=org, cost_amount=4000)
    sid = make_sales_order(db, org_id=org, product_id=product,
                           sale_date=SALE_DATE, sale_price=10000,
                           purchase_cost=4000)
    db.commit()
    return sid


def _case(db: Session, org, actor, order, case_type: str = "ESTIMATED_ONLY") -> uuid.UUID:
    golden.register_candidates(
        db, org_id=org, actor_user_id=actor,
        candidates=[{"sales_order_id": str(order), "case_type": case_type,
                     "selection_reason": "테스트"}])
    return uuid.UUID(str(db.execute(text("""
        SELECT id FROM verification_golden_case
         WHERE org_id = :o AND sales_order_id = :s
    """), {"o": str(org), "s": str(order)}).scalar()))


def _margin(db: Session, org, order, *, revenue: int, fee: int, cost: int,
            margin: int) -> None:
    product = db.execute(text(
        "SELECT product_id FROM sales_order WHERE id = :s"), {"s": str(order)}).scalar()
    db.execute(text("""
        INSERT INTO product_margin_actual
               (org_id, product_id, sales_order_id, actual_revenue, actual_cost,
                actual_shipping_cost, actual_marketplace_fee, actual_payment_fee,
                actual_ad_cost, actual_fulfillment_fee, actual_return_cost,
                actual_vat, actual_margin_amount, actual_margin_rate)
        VALUES (:o, :p, :s, :rev, :cost, 0, :fee, 0, 0, 0, 0, 0, :m, 0)
        ON CONFLICT (sales_order_id) DO UPDATE SET
            actual_revenue = EXCLUDED.actual_revenue,
            actual_cost = EXCLUDED.actual_cost,
            actual_marketplace_fee = EXCLUDED.actual_marketplace_fee,
            actual_margin_amount = EXCLUDED.actual_margin_amount
    """), {"o": str(org), "p": str(product), "s": str(order),
           "rev": revenue, "cost": cost, "fee": fee, "m": margin})
    db.commit()


# ── 1) 자동 승인 경로가 없다 ───────────────────────────────────────────

def test_registering_a_candidate_never_stores_an_expected_value(
    db: Session, org, actor, order,
):
    """후보 등록은 기준값을 만들지 않는다. `CANDIDATE` 로만 남는다."""
    _case(db, org, actor, order)

    row = db.execute(text("""
        SELECT status, expected_json, approved_at, revision
          FROM verification_golden_case WHERE org_id = :o
    """), {"o": str(org)}).mappings().one()

    assert row["status"] == golden.CANDIDATE
    assert row["expected_json"] is None
    assert row["approved_at"] is None
    assert row["revision"] == 0


def test_the_module_has_no_path_that_copies_current_results_into_expected():
    """§25 — 계산 결과를 기준값으로 베끼는 코드가 있으면 안 된다.

    문자열 검사는 조잡하지만, 이 규칙은 **한 줄만 추가돼도 무너진다.**
    나중에 편의 기능으로 되살아나는 것을 막는 최소한의 울타리다.
    """
    source = inspect.getsource(golden)
    banned = ("actual_margin_amount AS expected", "expected_json = pma",
              "auto_approve", "expected=current", "from_current_result")
    for token in banned:
        assert token not in source

    # 승인 함수는 호출자가 준 값만 쓴다 — DB 에서 읽어 채우지 않는다.
    approve_src = inspect.getsource(golden.approve)
    assert "product_margin_actual" not in approve_src


def test_approving_without_values_is_refused(db: Session, org, actor, order):
    case_id = _case(db, org, actor, order)

    with pytest.raises(ValidationError):
        golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                       expected={}, source_evidence="정산서")


def test_approving_without_saying_what_was_checked_is_refused(
    db: Session, org, actor, order,
):
    """무엇을 보고 확인했는지 없으면 나중에 그 판단을 되짚을 수 없다."""
    case_id = _case(db, org, actor, order)

    with pytest.raises(ValidationError):
        golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                       expected={"margin_amount": 6000}, source_evidence="   ")


def test_unknown_expected_stays_unknown(db: Session, org, actor, order):
    """모르는 항목을 0 으로 바꾸지 않는다 — 그러면 0 이 기준이 된다."""
    case_id = _case(db, org, actor, order)

    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 6000, "cost": None},
                   source_evidence="정산서 2026-01.xlsx")

    stored = db.execute(text("""
        SELECT expected_json FROM verification_golden_case WHERE id = :i
    """), {"i": str(case_id)}).scalar()
    assert stored["cost"] is None
    assert stored["margin_amount"] == 6000


# ── 2) 회귀 ────────────────────────────────────────────────────────────

def test_regression_passes_when_the_calculation_matches_the_approved_value(
    db: Session, org, actor, order,
):
    case_id = _case(db, org, actor, order)
    _margin(db, org, order, revenue=10000, fee=1000, cost=4000, margin=5000)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"net_revenue": 10000, "marketplace_fee": 1000,
                             "cost": 4000, "margin_amount": 5000},
                   source_evidence="정산서 2026-01.xlsx")

    result = golden.regression(db, org_id=org)

    assert result["counts"][golden.PASS] == 1
    assert result["status"] == golden.PASS


def test_regression_fails_when_the_calculation_drifts(db: Session, org, actor, order):
    """계산이 바뀌면 잡아야 한다. 이게 이 장치의 유일한 목적이다."""
    case_id = _case(db, org, actor, order)
    _margin(db, org, order, revenue=10000, fee=1000, cost=4000, margin=5000)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 5000},
                   source_evidence="정산서 2026-01.xlsx")

    # 코드가 바뀌어 마진이 달라졌다고 가정.
    _margin(db, org, order, revenue=10000, fee=1500, cost=4000, margin=4500)

    result = golden.regression(db, org_id=org)

    assert result["counts"][golden.FAIL] == 1
    assert result["status"] == golden.FAIL
    failing = result["cases"][0]["checks"][0]
    assert failing["expected"] == 5000
    assert failing["actual"] == 4500
    assert failing["difference"] == -500


def test_a_one_won_drift_is_not_tolerated(db: Session, org, actor, order):
    case_id = _case(db, org, actor, order)
    _margin(db, org, order, revenue=10000, fee=1000, cost=4000, margin=5000)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 5000}, source_evidence="정산서")
    _margin(db, org, order, revenue=10000, fee=1000, cost=4000, margin=4999)

    assert golden.regression(db, org_id=org)["counts"][golden.FAIL] == 1


def test_a_case_without_a_margin_row_is_not_comparable_not_passing(
    db: Session, org, actor, order,
):
    """§30 — 비교할 수 없는 것을 통과로 세지 않는다."""
    case_id = _case(db, org, actor, order)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 5000}, source_evidence="정산서")

    result = golden.regression(db, org_id=org)

    assert result["counts"][golden.NOT_COMPARABLE] == 1
    assert result["counts"][golden.PASS] == 0


def test_fields_the_human_did_not_check_are_not_compared(
    db: Session, org, actor, order,
):
    """사람이 확인하지 않은 항목까지 비교하면 없는 실패가 만들어진다."""
    case_id = _case(db, org, actor, order)
    _margin(db, org, order, revenue=10000, fee=9999, cost=4000, margin=5000)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 5000},   # 수수료는 확인하지 않았다
                   source_evidence="정산서")

    result = golden.regression(db, org_id=org)

    assert result["counts"][golden.PASS] == 1
    assert [c["field"] for c in result["cases"][0]["checks"]] == ["margin_amount"]


# ── 3) 기준값 변경 ─────────────────────────────────────────────────────

def test_changing_an_approved_baseline_requires_a_reason(
    db: Session, org, actor, order,
):
    """§31 — 조용히 덮으면 계산이 틀렸을 때 기준을 고쳐 통과시킬 수 있다."""
    case_id = _case(db, org, actor, order)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 5000}, source_evidence="정산서")

    with pytest.raises(ValidationError):
        golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                       expected={"margin_amount": 4000}, source_evidence="정산서")

    result = golden.approve(
        db, org_id=org, case_id=case_id, actor_user_id=actor,
        expected={"margin_amount": 4000}, source_evidence="정산서 재확인",
        change_reason="배송비 매출 포함 결정이 바뀌었다")
    assert result["revision"] == 2


def test_baseline_changes_are_audited(db: Session, org, actor, order):
    case_id = _case(db, org, actor, order)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 5000}, source_evidence="정산서")

    n = db.execute(text("""
        SELECT count(*) FROM audit_log
         WHERE domain_table = 'verification_golden_case' AND domain_row_id = :i
    """), {"i": str(case_id)}).scalar()
    assert n >= 1


# ── 4) 후보 선정 ───────────────────────────────────────────────────────

def test_candidates_are_not_persisted_by_proposing(db: Session, org, actor, order):
    """제안은 저장이 아니다."""
    golden.propose_candidates(db, org_id=org, limit=20)

    assert db.execute(text(
        "SELECT count(*) FROM verification_golden_case WHERE org_id = :o"),
        {"o": str(org)}).scalar() == 0


def test_candidates_carry_what_to_check(db: Session, org, actor, order):
    """§65 — 무엇을 열어 무엇을 봐야 하는지 없으면 사용자가 확인할 수 없다."""
    # 정책 추정 후보로 잡히려면 마진 행이 있어야 한다 — 아직 계산도 안 된
    # 주문을 "확인하세요" 라고 내밀 수는 없다.
    _margin(db, org, order, revenue=10000, fee=1000, cost=4000, margin=5000)

    picks = golden.propose_candidates(db, org_id=org, limit=20)

    assert picks
    for p in picks:
        assert p["selection_reason"]
        assert p["check_source"]
        assert p["check_amounts"]


def test_candidates_never_cross_org(db: Session, org, actor, order):
    other = make_organization(db)
    db.commit()

    assert golden.propose_candidates(db, org_id=other, limit=20) == []


def test_regression_never_crosses_org(db: Session, org, actor, order):
    case_id = _case(db, org, actor, order)
    _margin(db, org, order, revenue=10000, fee=1000, cost=4000, margin=5000)
    golden.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
                   expected={"margin_amount": 5000}, source_evidence="정산서")
    other = make_organization(db)
    db.commit()

    assert golden.regression(db, org_id=other)["approved_cases"] == 0
