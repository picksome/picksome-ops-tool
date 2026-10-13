"""정산 실측을 손익에 반영한다 (docs/83 D1~D4).

승인된 결정
---------
```
D1  배송비매출을 주문 공헌이익에 포함한다
D2  판매자 부담 쿠폰을 매출 차감으로 본다
D3  매칭된 주문은 실제 수수료를 정책 추정보다 우선한다
D4  정산서와 장부 금액이 다르면 알리되 장부를 고치지 않는다
```

이 파일이 고정하는 것
------------------
1. 주문 하나가 정산 행 여럿으로 와도 **한 행만 골라 쓰지 않는다**
2. 정산서가 안 주는 항목(광고비 등)을 **0 으로 덮지 않는다** — 기존 결함
3. 배송비 **매출**과 배송비 **비용**을 섞지 않는다
4. 정산 적재가 손익을 **조용히 다시 쓰지 않는다** (재계산 필요 표시만)
5. 장부와 다르면 예외로 보이되 `sales_order` 는 그대로
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.margin import ActualMarginInput
from app.services import close_checks, exception_center
from app.services import settlement_components as sc
from app.services.margin import calculate_actual_margin
from app.tests.fixtures.factories import (
    make_app_user,
    make_marketplace_settlement_record,
    make_organization,
    make_product,
    make_sales_order,
)

YM = "2026-04"
SALE_DATE = date(2026, 4, 10)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _order(db, org, *, sale_price, quantity=1, purchase_cost=0,
           shipping_fee=0, ad_cost=0):
    product = make_product(db, org_id=org, cost_amount=1_000)
    return make_sales_order(
        db, org_id=org, product_id=product, sale_date=SALE_DATE,
        sale_price=sale_price, quantity=quantity, purchase_cost=purchase_cost,
        shipping_fee=shipping_fee, ad_cost=ad_cost, marketplace="COUPANG",
    )


def _settle(db, org, so_id, *, row_type, gross, coupon=0, fee=0,
            status="MATCHED_EXACT", line="L1"):
    """실제 파일과 같은 모양의 정산 행 하나를 주문에 붙인다."""
    rid = make_marketplace_settlement_record(
        db, org_id=org, gross_revenue=gross, marketplace_fee=fee,
        settlement_period_start=SALE_DATE, settlement_period_end=SALE_DATE,
        row_type=row_type, matching_status="UNMATCHED",
    )
    db.execute(text("""
        UPDATE marketplace_settlement_record
           SET matching_status = :st,
               matched_sales_order_id = CAST(:so AS UUID),
               other_deductions = :cp, net_settlement = :net, line_reference = :lr,
               match_reason = CASE WHEN :st IN ('MATCHED_MANUAL','EXCLUDED')
                                   THEN '테스트' ELSE NULL END
         WHERE id = :id
    """), {"st": status, "so": str(so_id) if so_id else None, "cp": coupon,
           "net": gross - coupon - fee, "lr": line, "id": str(rid)})
    return rid


def _margin(db, org, so_id):
    return calculate_actual_margin(
        ActualMarginInput(org_id=org, sales_order_id=so_id), db)


# ──────────────────────────────────────────────────────────────────────
# 1. D1 — 배송비매출을 포함한다
# ──────────────────────────────────────────────────────────────────────

def test_delivery_revenue_is_included(db: Session, org):
    """주문 하나 = 상품 행 + 배송료 행. 둘 다 매출이다."""
    so = _order(db, org, sale_price=27_000, purchase_cost=10_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=2_592, line="P")
    _settle(db, org, so, row_type="SHIPPING_FEE_BASE", gross=3_000, fee=99, line="S1")
    _settle(db, org, so, row_type="SHIPPING_FEE_EXTRA", gross=0, fee=0, line="S2")

    r = _margin(db, org, so)
    assert r.revenue == 27_000                       # 24,000 + 3,000
    assert r.marketplace_fee == 2_691                # 2,592 + 99
    assert r.settlement_snapshot["product_revenue"] == 24_000
    assert r.settlement_snapshot["delivery_revenue"] == 3_000


def test_single_settlement_row_is_never_used_alone(db: Session, org):
    """배송료 행만 골라 쓰면 매출이 0 이 된다 — 그런 경로가 없어야 한다."""
    so = _order(db, org, sale_price=27_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=2_592, line="P")
    ship = _settle(db, org, so, row_type="SHIPPING_FEE_BASE", gross=3_000,
                   fee=99, line="S1")

    # 배송료 행을 지목해도 주문 전체로 계산된다.
    r = calculate_actual_margin(
        ActualMarginInput(org_id=org, sales_order_id=so, settlement_record_id=ship), db)
    assert r.revenue == 27_000
    assert r.revenue != 3_000


# ──────────────────────────────────────────────────────────────────────
# 2. D2 — 판매자 쿠폰은 매출 차감
# ──────────────────────────────────────────────────────────────────────

def test_seller_coupon_reduces_revenue(db: Session, org):
    so = _order(db, org, sale_price=47_900, purchase_cost=24_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=47_900,
            coupon=5_000, fee=4_530, line="P")

    r = _margin(db, org, so)
    assert r.revenue == 42_900                       # 47,900 - 5,000
    assert r.settlement_snapshot["seller_coupon"] == 5_000
    assert r.settlement_snapshot["gross_revenue"] == 47_900


def test_coupon_is_not_silently_dropped(db: Session, org):
    """쿠폰을 무시하면 마진이 그만큼 부푼다 — 기존 결함의 회귀 방지."""
    so = _order(db, org, sale_price=10_000, purchase_cost=0)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=10_000,
            coupon=1_000, fee=0, line="P")
    assert _margin(db, org, so).margin_amount == 9_000   # 10,000 이 아니다


# ──────────────────────────────────────────────────────────────────────
# 3. D3 — 실측 수수료 우선, 그러나 안 준 것은 덮지 않는다
# ──────────────────────────────────────────────────────────────────────

def test_actual_fee_beats_policy_estimate(db: Session, org):
    so = _order(db, org, sale_price=24_000)
    unmatched = _margin(db, org, so)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=2_592, line="P")
    matched = _margin(db, org, so)

    assert matched.marketplace_fee == 2_592
    assert matched.settlement_snapshot["basis"] == "SETTLEMENT_ACTUAL"
    assert unmatched.settlement_snapshot["basis"] == "LEDGER_AND_POLICY"


def test_settlement_does_not_zero_out_ledger_ad_cost(db: Session, org):
    """**기존 결함의 회귀 방지.**

    예전에는 정산 레코드가 있으면 `ad_cost` 를 그 값으로 갈아끼웠다. 쿠팡
    정산서에는 광고비가 없어서 적재 시 0 이 들어가 있다 — 그대로 덮으면
    장부의 광고비가 사라지고 마진이 그만큼 부푼다.
    """
    so = _order(db, org, sale_price=24_000, ad_cost=5_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=0, line="P")

    r = _margin(db, org, so)
    assert r.ad_cost == 5_000
    assert r.margin_amount == 24_000 - 5_000


def test_shipping_cost_stays_a_cost(db: Session, org):
    """배송비 **매출**이 들어와도 배송비 **비용**은 그대로 차감된다."""
    so = _order(db, org, sale_price=27_000, shipping_fee=2_500)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=0, line="P")
    _settle(db, org, so, row_type="SHIPPING_FEE_BASE", gross=3_000, fee=0, line="S")

    r = _margin(db, org, so)
    assert r.shipping_cost == 2_500                  # 매입 배송비 = 비용
    assert r.revenue == 27_000                       # 배송비 매출은 매출
    assert r.margin_amount == 27_000 - 2_500


# ──────────────────────────────────────────────────────────────────────
# 4. 컴포넌트 read model
# ──────────────────────────────────────────────────────────────────────

def test_components_reconcile_with_vendor_total(db: Session, org):
    """순매출 - 실수수료 == 벤더 정산금액. 실측 67행에서 차이 0원이었다."""
    so = _order(db, org, sale_price=27_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000,
            coupon=1_000, fee=2_592, line="P")
    _settle(db, org, so, row_type="SHIPPING_FEE_BASE", gross=3_000, fee=99, line="S")

    c = sc.load_for_order(db, org_id=org, sales_order_id=so)
    assert c.reconciles()
    assert c.net_revenue == 26_000
    assert c.row_count == 2


def test_unresolved_rows_are_not_posted(db: Session, org):
    """AMBIGUOUS·UNMATCHED·EXCLUDED 는 손익 근거가 될 수 없다.

    스키마가 먼저 막는다 — 이 셋은 `matched_sales_order_id` 를 가질 수 없다
    (M055 `ck_msr_unmatched_has_no_order`). 붙을 주문이 없으니 컴포넌트도
    없고, 마진은 장부 + 정책 추정으로 남는다.
    """
    from sqlalchemy.exc import IntegrityError

    so = _order(db, org, sale_price=24_000)
    # savepoint 안에서 시도한다 — 통째로 rollback 하면 이 테스트의 조직·상품
    # 픽스처까지 사라진다.
    sp = db.begin_nested()
    with pytest.raises(IntegrityError):
        _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=0,
                status="AMBIGUOUS", line="P")
    sp.rollback()

    so2 = _order(db, org, sale_price=24_000)
    # 주문에 안 붙은 AMBIGUOUS 행이 있어도 그 주문의 손익 근거가 되지 않는다.
    _settle(db, org, None, row_type="PRODUCT_SALE", gross=24_000, fee=0,
            status="AMBIGUOUS", line="P")
    assert sc.load_for_order(db, org_id=org, sales_order_id=so2) is None
    assert _margin(db, org, so2).settlement_snapshot["basis"] == "LEDGER_AND_POLICY"


def test_manual_match_counts_as_evidence(db: Session, org):
    """사람이 승인을 거쳐 붙인 것도 손익 근거다 — 연결 근거이지 금액 성질이 아니다."""
    so = _order(db, org, sale_price=24_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=1_000,
            status="MATCHED_MANUAL", line="P")
    c = sc.load_for_order(db, org_id=org, sales_order_id=so)
    assert c is not None and c.marketplace_fee == 1_000


def test_batch_load_avoids_n_plus_one(db: Session, org):
    orders = []
    for i in range(5):
        so = _order(db, org, sale_price=10_000)
        _settle(db, org, so, row_type="PRODUCT_SALE", gross=10_000, fee=500,
                line=f"P{i}")
        orders.append(so)

    seen: list[str] = []
    from sqlalchemy import event
    engine = db.get_bind()

    def before(conn, cur, statement, params, ctx, many):
        if "marketplace_settlement_record" in statement and "GROUP BY" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        got = sc.load_for_orders(db, org_id=org, sales_order_ids=orders)
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert len(got) == 5
    assert len(seen) == 1, f"집계 조회가 {len(seen)}번 돌았다 (N+1)"


# ──────────────────────────────────────────────────────────────────────
# 5. D4 — 알리되 고치지 않는다
# ──────────────────────────────────────────────────────────────────────

def test_revenue_mismatch_is_surfaced_but_ledger_untouched(db: Session, org):
    """실측 2건과 같은 상황 — 장부 수량이 빠져 정산서가 더 크다."""
    so = _order(db, org, sale_price=25_500, quantity=1)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=51_000, fee=0, line="P")

    r = _margin(db, org, so)
    assert r.settlement_snapshot["revenue_diff_vs_ledger"] == 25_500
    assert any("장부 판매금액" in w for w in r.warnings)

    items = {i.code: i for i in close_checks.run_checks(db, org_id=org, year_month=YM)}
    mismatch = items["SETTLEMENT_REVENUE_MISMATCH"]
    assert mismatch.count == 1
    assert not mismatch.blocking          # 막지 않는다

    ex = [i for i in exception_center.build(db, org_id=org)["items"]
          if i["kind"] == "SETTLEMENT_REVENUE_MISMATCH"]
    assert len(ex) == 1

    # **판매 원본은 그대로다.**
    assert db.execute(text("SELECT sale_price FROM sales_order WHERE id = :i"),
                      {"i": str(so)}).scalar() == 25_500


def test_matching_revenue_raises_no_mismatch(db: Session, org):
    so = _order(db, org, sale_price=27_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=0, line="P")
    _settle(db, org, so, row_type="SHIPPING_FEE_BASE", gross=3_000, fee=0, line="S")

    codes = {i.code for i in close_checks.run_checks(db, org_id=org, year_month=YM)}
    assert "SETTLEMENT_REVENUE_MISMATCH" not in codes


def test_component_mismatch_with_vendor_total_is_warned(db: Session, org):
    """컴포넌트 합이 벤더 정산금액과 다르면 조용히 넘기지 않는다."""
    so = _order(db, org, sale_price=24_000)
    rid = _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=1_000, line="P")
    db.execute(text("""
        UPDATE marketplace_settlement_record SET net_settlement = 99_999 WHERE id = :i
    """), {"i": str(rid)})

    r = _margin(db, org, so)
    assert any("정산금액" in w for w in r.warnings)


# ──────────────────────────────────────────────────────────────────────
# 6. 적재가 손익을 조용히 다시 쓰지 않는다
# ──────────────────────────────────────────────────────────────────────

def test_import_marks_period_dirty_instead_of_recalculating(db: Session, org, actor):
    """정산 적재는 마진을 덮지 않고 **재계산 필요**만 표시한다."""
    from app.services.settlement_import import _mark_affected_periods

    so = _order(db, org, sale_price=24_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=2_592, line="P")

    before = db.execute(text("""
        SELECT count(*) FROM product_margin_actual WHERE sales_order_id = :s
    """), {"s": str(so)}).scalar()

    marked = _mark_affected_periods(
        db, org_id=org, source_name="test.xlsx", actor_user_id=actor)

    assert marked == ["2026-04"]
    # 마진 행을 만들지도 고치지도 않았다.
    assert db.execute(text("""
        SELECT count(*) FROM product_margin_actual WHERE sales_order_id = :s
    """), {"s": str(so)}).scalar() == before
    # 마감 검증이 이 상태를 막는다 — 낡은 숫자로 확정하지 않게.
    codes = {i.code: i for i in close_checks.run_checks(db, org_id=org, year_month=YM)}
    assert codes["PERIOD_RECALC_PENDING"].blocking


def test_repeat_import_does_not_redirty_a_recalculated_period(
        db: Session, org, actor):
    """**이번 배치가 넣은 행만** 본다.

    매칭된 행 전체를 보면 이미 재계산을 끝낸 달까지 다시 dirty 로 만들고,
    그러면 "재계산 필요" 가 영원히 안 꺼져 마감이 계속 막힌다.
    """
    from app.services.settlement_import import _mark_affected_periods

    so = _order(db, org, sale_price=24_000)
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000, fee=2_592, line="P")

    # 이 행들은 어떤 배치에도 속하지 않는다(재실행이라 새로 넣은 것이 없다).
    empty_batch = uuid.uuid4()
    assert _mark_affected_periods(
        db, org_id=org, source_name="again.xlsx",
        actor_user_id=actor, batch_id=empty_batch) == []
    # 재계산 필요 표시가 생기지 않았다.
    assert db.execute(text("""
        SELECT count(*) FROM period_recalc_flag
         WHERE org_id = :o AND resolved_at IS NULL
    """), {"o": str(org)}).scalar() == 0


def test_recalculation_actually_reaches_the_margin(db: Session, org, actor):
    """**표시만 하고 반영이 안 되는 막다른 길이 아니어야 한다.**

    `period_recalc` 워커는 원래 월 집계만 다시 했다(비용 변경엔 맞는 설계).
    정산은 주문별 마진 자체를 바꾸므로, 그대로 두면 플래그는 꺼지는데 실
    마진은 옛 추정값으로 남는다.
    """
    from app.services.margin import calculate_actual_margin, save_actual_margin
    from app.services.period_recalc import _refresh_settled_margins

    so = _order(db, org, sale_price=24_000, purchase_cost=5_000)
    # 정산서가 오기 전에 계산해 저장해 둔다 — 정책 추정값이 들어간다.
    inp = ActualMarginInput(org_id=org, sales_order_id=so)
    save_actual_margin(inp, calculate_actual_margin(inp, db), db)
    before = db.execute(text("""
        SELECT actual_revenue, actual_marketplace_fee, calculation_snapshot_json
          FROM product_margin_actual WHERE sales_order_id = :s
    """), {"s": str(so)}).mappings().one()
    assert before["calculation_snapshot_json"]["basis"] == "LEDGER_AND_POLICY"

    # 정산서가 붙는다 — 매출과 수수료가 실측으로 바뀐다.
    _settle(db, org, so, row_type="PRODUCT_SALE", gross=24_000,
            coupon=1_000, fee=2_592, line="P")
    _settle(db, org, so, row_type="SHIPPING_FEE_BASE", gross=3_000, fee=99, line="S")

    assert _refresh_settled_margins(db, org_id=org, year_month=YM) == 1

    after = db.execute(text("""
        SELECT actual_revenue, actual_marketplace_fee, calculation_snapshot_json
          FROM product_margin_actual WHERE sales_order_id = :s
    """), {"s": str(so)}).mappings().one()
    assert int(after["actual_revenue"]) == 26_000        # 24,000 + 3,000 - 1,000
    assert int(after["actual_marketplace_fee"]) == 2_691
    assert after["calculation_snapshot_json"]["basis"] == "SETTLEMENT_ACTUAL"
    assert int(after["actual_revenue"]) != int(before["actual_revenue"])


def test_refresh_skips_orders_without_settlement(db: Session, org):
    """정산이 안 붙은 주문은 건드리지 않는다 — 바뀐 입력이 없다."""
    from app.services.period_recalc import _refresh_settled_margins

    _order(db, org, sale_price=24_000)
    _order(db, org, sale_price=10_000)
    assert _refresh_settled_margins(db, org_id=org, year_month=YM) == 0
