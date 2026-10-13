"""Golden 검증 보조 — 원천 수집 · 독립 검산 · 보류.

이 파일이 지키는 것 하나가 나머지보다 중요하다
------------------------------------------
**독립 계산기가 production 마진 계산기를 부르지 않는다.**

부르는 순간 이 장치는 아무것도 지키지 못한다 — 계산이 어떻게 망가지든
기대값이 같이 망가져 늘 통과한다. 그 격리를 소스와 동작 양쪽에서 고정한다.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationError
from app.services import golden_evidence as ge
from app.services import verification_golden as vg
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
                           sale_date=SALE_DATE, sale_price=30000,
                           purchase_cost=12000)
    db.commit()
    return sid


@pytest.fixture
def fee_policy(db: Session, org) -> None:
    """이 조직의 쿠팡 수수료 정책 11%.

    정책이 없으면 수수료가 `UNKNOWN` 이 되는 것이 **올바른 동작**이라
    추정 경로를 보려면 정책을 심어야 한다.
    """
    db.execute(text("""
        INSERT INTO marketplace_fee_policy
               (org_id, marketplace, fee_type, rate, effective_from, version)
        VALUES (:o, 'COUPANG', 'RATE', 0.1100, '2025-01-01', 1)
    """), {"o": str(org)})
    db.commit()


@pytest.fixture
def claim_account(db: Session, org, actor) -> uuid.UUID:
    """클레임이 가리킬 외부 계정. `sales_claim` 이 NOT NULL 이다."""
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    return uuid.UUID(str(db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name, credential_kind, created_by, updated_by)
        VALUES (:o, :s, 'DEFAULT', 'NE', 'API_KEY', :a, :a) RETURNING id
    """), {"o": str(org), "s": str(sys_id), "a": str(actor)}).scalar()))


def _settlement(
    db: Session, *, org_id: uuid.UUID, sales_order_id: uuid.UUID,
    row_type: str, gross: int, fee: int, net: int, deductions: int = 0,
    order_no: str = "M-1",
) -> None:
    db.execute(text("""
        INSERT INTO marketplace_settlement_record
               (org_id, marketplace, settlement_period_start, settlement_period_end,
                marketplace_order_no, gross_revenue, marketplace_fee, ad_cost,
                return_cost, fulfillment_fee, other_deductions, net_settlement,
                matched_sales_order_id, matching_status, row_type, line_reference,
                import_file_hash, import_idempotency_key, source_row_fingerprint)
        VALUES (:o, 'COUPANG', :d, :d, :ono, :g, :f, 0, 0, 0, :ded, :n,
                :sid, 'MATCHED_EXACT', :rt, :lr, 'hash', :key, :fp)
    """), {"o": str(org_id), "d": SALE_DATE, "ono": order_no, "g": gross,
           "f": fee, "ded": deductions, "n": net, "sid": str(sales_order_id),
           "rt": row_type, "lr": f"{order_no}-{row_type}",
           "key": str(uuid.uuid4()), "fp": str(uuid.uuid4())})


def _margin(db: Session, org, order, *, revenue: int, cost: int, shipping: int,
            fee: int, ad: int, margin: int) -> None:
    product = db.execute(text(
        "SELECT product_id FROM sales_order WHERE id = :s"), {"s": str(order)}).scalar()
    db.execute(text("""
        INSERT INTO product_margin_actual
               (org_id, product_id, sales_order_id, actual_revenue, actual_cost,
                actual_shipping_cost, actual_marketplace_fee, actual_payment_fee,
                actual_ad_cost, actual_fulfillment_fee, actual_return_cost,
                actual_vat, actual_margin_amount, actual_margin_rate)
        VALUES (:o, :p, :s, :rev, :cost, :ship, :fee, 0, :ad, 0, 0, 0, :m, 0)
        ON CONFLICT (sales_order_id) DO UPDATE SET
            actual_revenue = EXCLUDED.actual_revenue,
            actual_cost = EXCLUDED.actual_cost,
            actual_shipping_cost = EXCLUDED.actual_shipping_cost,
            actual_marketplace_fee = EXCLUDED.actual_marketplace_fee,
            actual_ad_cost = EXCLUDED.actual_ad_cost,
            actual_margin_amount = EXCLUDED.actual_margin_amount
    """), {"o": str(org), "p": str(product), "s": str(order), "rev": revenue,
           "cost": cost, "ship": shipping, "fee": fee, "ad": ad, "m": margin})
    db.commit()


# ══════════════════════════════════════════════════════════════════
# 1. 격리 — 이 파일의 존재 이유
# ══════════════════════════════════════════════════════════════════

def test_the_independent_calculator_does_not_import_the_production_one():
    """§42 — production 마진 계산기를 import 하면 검증이 무의미해진다.

    AST 로 확인한다. 문자열 검색은 주석에도 걸리지만, import 문은 실제
    의존이다.
    """
    tree = ast.parse(inspect.getsource(ge))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    banned = {
        "app.services.margin",
        "app.services.calculation",
        "app.services.settlement_components",
        "app.core.money",
    }
    assert not (imported & banned), (
        f"독립 계산기가 production 계산 모듈을 참조한다: {imported & banned}")


def test_the_independent_calculator_restates_the_rounding_rule():
    """반올림도 여기서 다시 적는다 — production 규칙이 바뀌면 차이로 드러나야 한다."""
    assert ge.krw(Decimal("0.5")) == 1      # HALF_UP
    assert ge.krw(Decimal("1.4")) == 1
    assert ge.krw(Decimal("1.5")) == 2
    assert ge.krw(10) == 10


def test_independent_arithmetic_is_plain_and_checkable():
    """산식이 사람이 검산할 수 있는 모양이어야 한다."""
    net = ge.independent_net_revenue(
        product_revenue=30000, delivery_revenue=3000, seller_coupon=2000)
    assert net == 31000

    margin = ge.independent_margin(
        net_revenue=31000, cost=12000, shipping_cost=3000,
        marketplace_fee=3485, ad_cost=0)
    assert margin == 31000 - 12000 - 3000 - 3485


def test_unpriced_components_are_not_silently_zeroed():
    """반품비·풀필먼트·결제수수료·부가세는 계산에 넣지 않는다.

    0 이라서가 아니라 **모르기 때문**이다. 넣어 버리면 그 사실이 지워진다.
    """
    assert set(ge.UNPRICED_COMPONENTS) == {
        "return_cost", "fulfillment_fee", "payment_fee", "vat"}


# ══════════════════════════════════════════════════════════════════
# 2. 원천 수집
# ══════════════════════════════════════════════════════════════════

def test_settlement_facts_carry_where_the_number_came_from(
    db: Session, org, actor, order,
):
    """§24 — 사용자가 엑셀을 열지 않아도 출처를 알 수 있어야 한다."""
    _settlement(db, org_id=org, sales_order_id=order,
                row_type="PRODUCT_SALE", gross=30000, fee=3300, net=26700)
    db.commit()

    built = ge.build(db, org_id=org, sales_order_id=order)

    fact = built["facts"]["product_revenue"]
    assert fact["value"] == 30000
    assert fact["source"] == "SETTLEMENT"
    assert fact["confidence"] == ge.ACTUAL
    assert fact["source_ref"]           # 파일·시트·행
    assert fact["source_date"]


def test_a_missing_settlement_is_unknown_not_zero(db: Session, org, actor, order):
    """정산서가 없으면 배송비·쿠폰은 **모르는 것**이다. 0 이 아니다."""
    built = ge.build(db, org_id=org, sales_order_id=order)

    assert built["facts"]["delivery_revenue"]["value"] is None
    assert built["facts"]["delivery_revenue"]["confidence"] == ge.UNKNOWN
    assert built["facts"]["seller_coupon"]["value"] is None
    # 계산은 0 으로 가정하되 **가정했다는 사실을 남긴다.**
    assert any("0으로 가정" in a for a in built["independent"]["assumptions"])


def test_an_estimated_fee_says_it_is_estimated(
    db: Session, org, actor, order, fee_policy,
):
    """§28 — 무엇을 승인하는지 사용자가 알아야 한다."""
    built = ge.build(db, org_id=org, sales_order_id=order)

    fee = built["facts"]["marketplace_fee"]
    assert fee["confidence"] == ge.ESTIMATED
    assert fee["source"] == "POLICY"
    assert "%" in (fee["source_ref"] or "")
    # 30,000 × 11% = 3,300 — 사람이 검산할 수 있는 숫자다.
    assert fee["value"] == 3300
    assert built["golden_confidence"] == ge.ESTIMATED


def test_a_missing_fee_policy_is_unknown_not_zero(db: Session, org, actor, order):
    """정책이 없으면 **모르는 것**이다. 0원으로 계산해 마진을 부풀리지 않는다."""
    built = ge.build(db, org_id=org, sales_order_id=order)

    fee = built["facts"]["marketplace_fee"]
    assert fee["value"] is None
    assert fee["confidence"] == ge.UNKNOWN
    assert built["independent"]["calculable"] is False
    assert built["evidence_state"] == "SOURCE_EVIDENCE_INCOMPLETE"


def test_settlement_backed_case_is_actual_confidence(db: Session, org, actor, order):
    _settlement(db, org_id=org, sales_order_id=order,
                row_type="PRODUCT_SALE", gross=30000, fee=3300, net=26700)
    db.commit()

    built = ge.build(db, org_id=org, sales_order_id=order)

    assert built["golden_confidence"] == ge.ACTUAL


def test_missing_cost_blocks_independent_calculation(db: Session, org, actor):
    """원가를 모르면 마진을 셀 수 없다. 0 으로 세지 않는다."""
    product = make_product(db, org_id=org, cost_amount=None)
    sid = make_sales_order(db, org_id=org, product_id=product,
                           sale_date=SALE_DATE, sale_price=30000,
                           purchase_cost=None)
    db.commit()

    built = ge.build(db, org_id=org, sales_order_id=sid)

    assert built["facts"]["cost"]["value"] is None
    assert built["independent"]["calculable"] is False
    assert built["evidence_state"] == "SOURCE_EVIDENCE_INCOMPLETE"
    assert built["blockers"]


# ══════════════════════════════════════════════════════════════════
# 3. 비교
# ══════════════════════════════════════════════════════════════════

def test_zero_difference_when_seller_os_agrees(db: Session, org, actor, order):
    """독립 검산과 저장된 계산이 같으면 차이 0."""
    _settlement(db, org_id=org, sales_order_id=order, row_type="PRODUCT_SALE",
                gross=30000, fee=3300, net=26700)
    _settlement(db, org_id=org, sales_order_id=order, row_type="SHIPPING_FEE_BASE",
                gross=3000, fee=0, net=3000)
    db.commit()
    # 순매출 33,000 · 원가 12,000 · 매입배송비 0 · 수수료 3,300 → 마진 17,700
    _margin(db, org, order, revenue=33000, cost=12000, shipping=0,
            fee=3300, ad=0, margin=17700)

    built = ge.build(db, org_id=org, sales_order_id=order)

    assert built["independent"]["net_revenue"] == 33000
    assert built["independent"]["margin"] == 17700
    assert built["comparison"]["all_match"] is True
    assert built["comparison"]["mismatched"] == 0


def test_a_one_won_drift_is_shown_not_hidden(db: Session, org, actor, order):
    """§17 — 원 단위로 정확히 본다."""
    _settlement(db, org_id=org, sales_order_id=order, row_type="PRODUCT_SALE",
                gross=30000, fee=3300, net=26700)
    db.commit()
    _margin(db, org, order, revenue=30000, cost=12000, shipping=0,
            fee=3300, ad=0, margin=14699)   # 1원 어긋남

    built = ge.build(db, org_id=org, sales_order_id=order)

    assert built["comparison"]["all_match"] is False
    bad = [i for i in built["comparison"]["items"] if not i["match"]]
    assert len(bad) == 1
    assert bad[0]["field"] == "margin"
    assert bad[0]["difference"] == 1


def test_vendor_settlement_check_uses_only_vendor_numbers(
    db: Session, org, actor, order,
):
    """§8 — 정산서 안의 관계다. 우리 계산이 끼어들지 않는다."""
    _settlement(db, org_id=org, sales_order_id=order, row_type="PRODUCT_SALE",
                gross=30000, fee=3300, net=26700)
    db.commit()

    built = ge.build(db, org_id=org, sales_order_id=order)

    check = built["independent"]["vendor_check"]
    assert check["reconstructed"] == 26700
    assert check["vendor_net_settlement"] == 26700
    assert check["difference"] == 0


def test_an_uncalculated_order_is_not_comparable(db: Session, org, actor, order):
    built = ge.build(db, org_id=org, sales_order_id=order)

    assert built["comparison"]["comparable"] is False
    assert built["margin_calculated"] is False


# ══════════════════════════════════════════════════════════════════
# 4. 클레임 · 체크리스트
# ══════════════════════════════════════════════════════════════════

def test_a_claim_order_is_not_presented_as_final(
    db: Session, org, actor, order, claim_account,
):
    """§26 — 환불 손익이 미검증인데 확정으로 보이면 안 된다."""
    db.execute(text("""
        INSERT INTO sales_claim (org_id, external_account_id, sales_order_id,
               claim_type, claim_status, financial_effect, claim_quantity,
               external_claim_id)
        VALUES (:o, :acc, :s, 'RETURN', 'COMPLETED', 'EFFECTIVE', 1, :x)
    """), {"o": str(org), "acc": str(claim_account), "s": str(order),
           "x": str(uuid.uuid4())})
    db.commit()

    built = ge.build(db, org_id=org, sales_order_id=order)

    assert built["claims"]
    assert built["refund_financial"] == "NOT_VERIFIED"
    assert any("확정" in w for w in built["warnings"])


def test_the_checklist_is_short_and_specific(db: Session, org, actor, order):
    """§46 — 3~5개만. 전부 나열하면 아무도 안 본다."""
    _settlement(db, org_id=org, sales_order_id=order, row_type="PRODUCT_SALE",
                gross=30000, fee=3300, net=26700)
    db.commit()

    built = ge.build(db, org_id=org, sales_order_id=order)

    assert 3 <= len(built["checklist"]) <= 5
    for item in built["checklist"]:
        assert item["label"]
        assert item["detail"]


def test_evidence_carries_no_personal_information(db: Session, org, actor, order):
    """§41 — 검증에 구매자 정보가 필요 없다."""
    built = ge.build(db, org_id=org, sales_order_id=order)

    blob = str(built).lower()
    for forbidden in ("recipient", "phone", "postcode", "수취인", "전화", "배송지"):
        assert forbidden not in blob


def test_evidence_never_crosses_org(db: Session, org, actor, order):
    from app.core.exceptions import NotFoundError

    other = make_organization(db)
    db.commit()

    with pytest.raises(NotFoundError):
        ge.build(db, org_id=other, sales_order_id=order)


# ══════════════════════════════════════════════════════════════════
# 5. 보류
# ══════════════════════════════════════════════════════════════════

def _case(db: Session, org, actor, order) -> uuid.UUID:
    vg.register_candidates(
        db, org_id=org, actor_user_id=actor,
        candidates=[{"sales_order_id": str(order), "case_type": "ESTIMATED_ONLY",
                     "selection_reason": "테스트"}])
    return uuid.UUID(str(db.execute(text("""
        SELECT id FROM verification_golden_case WHERE org_id = :o
    """), {"o": str(org)}).scalar()))


def test_defer_is_not_a_failure(db: Session, org, actor, order):
    """§21 — 보류를 계산 오류로 세지 않는다."""
    case_id = _case(db, org, actor, order)

    result = vg.defer(db, org_id=org, case_id=case_id, actor_user_id=actor,
                      reason="COST_UNCERTAIN", note="매입 기록 확인 필요")

    assert result["status"] == vg.DEFERRED
    # 회귀는 승인된 것만 본다 — 보류는 어디에도 안 센다.
    reg = vg.regression(db, org_id=org)
    assert reg["approved_cases"] == 0
    assert reg["counts"][vg.FAIL] == 0


def test_defer_requires_a_known_reason(db: Session, org, actor, order):
    case_id = _case(db, org, actor, order)

    with pytest.raises(ValidationError):
        vg.defer(db, org_id=org, case_id=case_id, actor_user_id=actor,
                 reason="MADE_UP")


def test_an_approved_case_cannot_be_quietly_deferred(db: Session, org, actor, order):
    """승인된 기준을 보류로 되돌리면 회귀가 조용히 약해진다."""
    case_id = _case(db, org, actor, order)
    vg.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
               expected={"margin_amount": 5000}, source_evidence="정산서")

    with pytest.raises(ConflictError):
        vg.defer(db, org_id=org, case_id=case_id, actor_user_id=actor,
                 reason="OTHER")


def test_approval_clears_the_defer_marks(db: Session, org, actor, order):
    """두 상태를 동시에 들 수 없다."""
    case_id = _case(db, org, actor, order)
    vg.defer(db, org_id=org, case_id=case_id, actor_user_id=actor,
             reason="FEE_ESTIMATED")
    vg.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
               expected={"margin_amount": 5000}, source_evidence="정산서")

    row = db.execute(text("""
        SELECT status, defer_reason, deferred_at FROM verification_golden_case
         WHERE id = :i
    """), {"i": str(case_id)}).mappings().one()
    assert row["status"] == vg.APPROVED
    assert row["defer_reason"] is None
    assert row["deferred_at"] is None


# ══════════════════════════════════════════════════════════════════
# 6. 승인 근거 박제
# ══════════════════════════════════════════════════════════════════

def test_the_approved_evidence_is_frozen(db: Session, org, actor, order):
    """§15·§44 — 원천이 나중에 바뀌어도 승인 당시 근거는 그대로여야 한다."""
    case_id = _case(db, org, actor, order)
    snapshot = ge.build(db, org_id=org, sales_order_id=order)

    vg.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
               expected={"margin_amount": 5000}, source_evidence="정산서",
               evidence_snapshot=snapshot)

    stored = db.execute(text("""
        SELECT approved_evidence_json FROM verification_golden_case WHERE id = :i
    """), {"i": str(case_id)}).scalar()
    frozen_cost = stored["facts"]["cost"]["value"]

    # 원천을 바꾼다.
    db.execute(text("UPDATE sales_order SET purchase_cost = 99999 WHERE id = :s"),
               {"s": str(order)})
    db.commit()

    still = db.execute(text("""
        SELECT approved_evidence_json FROM verification_golden_case WHERE id = :i
    """), {"i": str(case_id)}).scalar()
    assert still["facts"]["cost"]["value"] == frozen_cost
    # 그리고 지금 원천은 달라졌다 — 박제가 따라가지 않았다는 뜻이다.
    assert ge.build(db, org_id=org, sales_order_id=order)["facts"]["cost"]["value"] == 99999


def test_the_frozen_evidence_is_not_used_for_regression(db: Session, org, actor, order):
    """회귀 기준은 `expected_json` 하나뿐이다. 근거는 읽기 전용이다."""
    case_id = _case(db, org, actor, order)
    snapshot = ge.build(db, org_id=org, sales_order_id=order)
    vg.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
               expected={"margin_amount": 5000}, source_evidence="정산서",
               evidence_snapshot=snapshot)
    _margin(db, org, order, revenue=30000, cost=12000, shipping=0,
            fee=3300, ad=0, margin=4999)

    reg = vg.regression(db, org_id=org)

    # expected 5000 vs 현재 4999 → FAIL. 박제된 근거가 개입하지 않는다.
    assert reg["counts"][vg.FAIL] == 1


# ══════════════════════════════════════════════════════════════════
# 7. 최소 추천 세트
# ══════════════════════════════════════════════════════════════════

def test_the_recommended_set_covers_distinct_paths(db: Session, org, actor):
    """§32 — 같은 경로를 반복하지 않는다. 유형당 하나씩."""
    candidates = [
        {"sales_order_id": str(uuid.uuid4()), "case_type": "SETTLEMENT_EXACT",
         "business_key": "A", "has_settlement": True},
        {"sales_order_id": str(uuid.uuid4()), "case_type": "SETTLEMENT_EXACT",
         "business_key": "B", "has_settlement": True},
        {"sales_order_id": str(uuid.uuid4()), "case_type": "ESTIMATED_ONLY",
         "business_key": "C", "has_settlement": False},
    ]

    rec = vg.recommend_minimum_set(db, org_id=org, candidates=candidates)

    types = [r["case_type"] for r in rec["recommended"]]
    assert len(types) == len(set(types))          # 유형 중복 없음
    assert "SETTLEMENT_EXACT" in types
    assert "ESTIMATED_ONLY" in types
    for r in rec["recommended"]:
        assert r["covers"]                        # 왜 골랐는지


def test_missing_case_types_are_reported_not_invented(db: Session, org):
    """§4 — 실제로 없는 유형을 만들어 채우지 않는다."""
    rec = vg.recommend_minimum_set(db, org_id=org, candidates=[])

    assert rec["recommended"] == []
    assert len(rec["uncovered"]) == len(vg.MINIMUM_COVERAGE)


def test_coverage_shows_which_paths_are_still_open(db: Session, org, actor, order):
    """§31 — "6/16 승인" 만으로는 무엇이 안 덮였는지 모른다."""
    case_id = _case(db, org, actor, order)
    vg.approve(db, org_id=org, case_id=case_id, actor_user_id=actor,
               expected={"margin_amount": 5000}, source_evidence="정산서")

    cov = vg.coverage(db, org_id=org)

    covered = [c for c in cov["by_case_type"] if c["covered"]]
    assert [c["case_type"] for c in covered] == ["ESTIMATED_ONLY"]
    assert cov["covered"] == 1
    assert cov["of"] == len(vg.MINIMUM_COVERAGE)
