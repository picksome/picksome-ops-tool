"""services/margin.py 통합 테스트 — 정책 조회 + 계산 + 영구화(snapshot 박제) 통합 경로."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.exceptions import NotFoundError, ValidationError
from app.domain.margin import ActualMarginInput, ExpectedMarginInput
from app.services.margin import (
    calculate_actual_margin,
    calculate_expected_margin,
    save_actual_margin,
    save_expected_margin,
)
from app.tests.fixtures import factories as f

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _seed_policies(db, org_id):
    f.make_tax_policy(db, org_id=org_id, vat_rate=Decimal("0.10"))
    f.make_marketplace_fee_policy(db, org_id=org_id, rate=Decimal("0.1080"))
    f.make_payment_fee_policy(db, org_id=org_id, rate=Decimal("0.0250"))
    f.make_return_risk_policy(db, org_id=org_id, return_rate=Decimal("0.03"))
    f.make_ad_cost_policy(db, org_id=org_id, default_ad_ratio=Decimal("0.05"))


def test_calculate_and_save_expected_margin_persists_snapshot(db):
    org_id = f.make_organization(db)
    _seed_policies(db, org_id)
    product_id = f.make_product(db, org_id=org_id, cost_amount=10_000)

    inp = ExpectedMarginInput(
        org_id=org_id, product_id=product_id, margin_type="GENERAL_RESELL",
        marketplace="COUPANG", cost_amount=10_000, sale_price=20_000,
        payment_method="BUSINESS_CARD", extras={},
    )
    result = calculate_expected_margin(inp, db, at=date(2026, 6, 1))
    assert result.margin_amount == 3_956  # 기존 순수함수 테스트와 동일 케이스

    new_id = save_expected_margin(inp, result, db, valid_from=date(2026, 6, 1))

    row = db.execute(
        text("SELECT snapshot_id, computed_margin_amount FROM product_margin_expected WHERE id = :id"),
        {"id": str(new_id)},
    ).first()
    assert str(row.snapshot_id) != ZERO_UUID
    assert int(row.computed_margin_amount) == 3_956

    ref_count = db.execute(
        text("SELECT COUNT(*) AS c FROM policy_snapshot_ref WHERE snapshot_id = :sid"),
        {"sid": str(row.snapshot_id)},
    ).first()
    assert ref_count.c >= 1


def test_calculate_actual_margin_without_settlement_record(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id, cost_amount=7_000)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 5),
        sale_price=20_000, quantity=1, ad_cost=1_000,
    )

    result = calculate_actual_margin(ActualMarginInput(org_id=org_id, sales_order_id=so_id), db)

    assert result.revenue == 20_000
    assert result.cost == 7_000  # purchase_cost 없음 -> product.cost_amount × qty
    # 정산서도 없고 이 org 에 마켓 수수료 정책도 없어 추정 불가 -> 0
    assert result.marketplace_fee == 0
    assert result.ad_cost == 1_000  # sales_order.ad_cost 사용
    assert result.margin_amount == 20_000 - 7_000 - 1_000
    assert result.matched_expected_id is None


def test_actual_margin_prefers_purchase_cost_and_subtracts_shipping(db):
    """회귀 방지: 판매장부의 실제 매입액이 상품 마스터 단가보다 우선한다 (M013).

    purchase_cost 는 sale_price 와 마찬가지로 라인 합계라 quantity 를 곱하지
    않는다. 매입 배송비도 상품 직접비이므로 마진에서 빠져야 한다.
    """
    org_id = f.make_organization(db)
    # 상품 마스터 단가는 일부러 다른 값 — purchase_cost 가 이기는지 보려고
    product_id = f.make_product(db, org_id=org_id, cost_amount=9_999)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 5),
        sale_price=20_000, quantity=3, purchase_cost=8_000, shipping_fee=3_000,
    )

    result = calculate_actual_margin(ActualMarginInput(org_id=org_id, sales_order_id=so_id), db)

    assert result.cost == 8_000  # 9_999×3 도 아니고 8_000×3 도 아니다
    assert result.shipping_cost == 3_000
    assert result.margin_amount == 20_000 - 8_000 - 3_000


def test_actual_margin_treats_zero_purchase_cost_as_real_zero(db):
    """회귀 방지: purchase_cost=0 은 "미입력"이 아니라 "실제 0원"이다 (M014).

    취소/반품/증정으로 거래가 성립하지 않은 행이다. 여기에 상품 마스터 원가를
    끼워 넣으면 팔지도 않은 주문에 원가만 계상된다. 픽썸 실데이터에서 0원 15건
    중 14건이 매출도 0이었고, 이걸 fallback 시켰더니 엑셀 대조 일치가
    15/19 → 12/19 로 떨어졌다.
    """
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id, cost_amount=9_999)

    zero_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 5),
        sale_price=0, purchase_cost=0,
    )
    unknown_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 6),
        sale_price=20_000, purchase_cost=None,
    )

    zero = calculate_actual_margin(ActualMarginInput(org_id=org_id, sales_order_id=zero_id), db)
    unknown = calculate_actual_margin(ActualMarginInput(org_id=org_id, sales_order_id=unknown_id), db)

    assert zero.cost == 0  # 0 을 존중 — 9,999 로 대체하면 안 된다
    assert unknown.cost == 9_999  # NULL 일 때만 상품 마스터 단가로 추정


def test_actual_margin_without_product_link_still_computes(db):
    """회귀 방지: 상품 마스터에 링크되지 않은 판매도 실 마진이 나와야 한다 (M013).

    엑셀 import 된 판매장부는 product_id 가 전부 NULL 이다(상품명 스냅샷만 있음).
    예전에는 product_margin_actual.product_id 가 NOT NULL 이라 한 건도 저장할 수
    없었고, _match_expected 가 str(None) 을 UUID 로 보내 DataError 를 냈다.
    """
    org_id = f.make_organization(db)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=None, sale_date=date(2026, 6, 5),
        sale_price=15_000, purchase_cost=5_000, shipping_fee=2_500,
    )
    inp = ActualMarginInput(org_id=org_id, sales_order_id=so_id)

    result = calculate_actual_margin(inp, db)
    assert result.product_id is None
    assert result.margin_amount == 15_000 - 5_000 - 2_500

    # 저장까지 되어야 월정산 direct_cost 에 잡힌다
    save_actual_margin(inp, result, db)
    row = db.execute(
        text(
            "SELECT actual_cost, actual_shipping_cost, product_id"
            "  FROM product_margin_actual WHERE sales_order_id = :sid"
        ),
        {"sid": str(so_id)},
    ).first()
    assert int(row.actual_cost) == 5_000
    assert int(row.actual_shipping_cost) == 2_500
    assert row.product_id is None


def test_calculate_actual_margin_matches_expected_and_computes_delta(db):
    org_id = f.make_organization(db)
    _seed_policies(db, org_id)
    product_id = f.make_product(db, org_id=org_id, cost_amount=10_000)

    inp = ExpectedMarginInput(
        org_id=org_id, product_id=product_id, margin_type="GENERAL_RESELL",
        marketplace="COUPANG", cost_amount=10_000, sale_price=20_000,
        payment_method="BUSINESS_CARD", extras={},
    )
    expected_result = calculate_expected_margin(inp, db, at=date(2026, 6, 1))
    save_expected_margin(inp, expected_result, db, valid_from=date(2026, 6, 1))

    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 5),
        sale_price=20_000, quantity=1,
    )
    actual = calculate_actual_margin(ActualMarginInput(org_id=org_id, sales_order_id=so_id), db)

    assert actual.matched_expected_id is not None
    # 실 마진(수수료/광고비 미반영, MVP 0) vs 예상 마진(정책 반영) 차이가 delta 로 잡힘.
    assert actual.delta_margin_amount == actual.margin_amount - expected_result.margin_amount


def _match_settlement(db, org_id, so_id, *, product_revenue, delivery_revenue,
                      coupon, fee):
    """주문 하나에 실제 파일과 같은 모양(상품 + 배송료)으로 정산 행을 붙인다.

    금액은 벤더 항등식(순매출 - 수수료 = 정산금액)을 지키게 나눈다 —
    안 지키면 `reconciles()` 가 거짓이 되어 경고가 붙는다.
    """
    rows = [("PRODUCT_SALE", product_revenue, coupon, fee)]
    if delivery_revenue:
        rows.append(("SHIPPING_FEE_BASE", delivery_revenue, 0, 0))
    ids = []
    for i, (row_type, gross, cp, fee_) in enumerate(rows):
        rid = f.make_marketplace_settlement_record(
            db, org_id=org_id, gross_revenue=gross, marketplace_fee=fee_,
            row_type=row_type, matching_status="UNMATCHED",
        )
        db.execute(
            text(
                """
                UPDATE marketplace_settlement_record
                   SET matching_status = 'MATCHED_EXACT',
                       matched_sales_order_id = :so,
                       other_deductions = :cp,
                       net_settlement = :net,
                       line_reference = :lref
                 WHERE id = :id
                """
            ),
            {"so": str(so_id), "cp": cp, "net": gross - cp - fee_,
             "lref": f"L{i}", "id": str(rid)},
        )
        ids.append(rid)
    return ids


def test_calculate_actual_margin_uses_settlement_components(db):
    """매칭된 주문은 **주문 단위 정산 컴포넌트**로 계산한다 (docs/83 D1·D2·D3).

    쿠팡은 주문 하나를 상품·기본배송료·추가배송료 세 행으로 준다. 행 하나를
    골라 붙이면 나머지가 사라진다 — 배송료 행을 고르면 매출이 0 이 된다.
    """
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id, cost_amount=5_000)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 5),
        sale_price=20_000, quantity=1, ad_cost=999,
    )
    _match_settlement(db, org_id, so_id, product_revenue=18_000,
                      delivery_revenue=3_000, coupon=1_000, fee=2_000)

    result = calculate_actual_margin(
        ActualMarginInput(org_id=org_id, sales_order_id=so_id), db)

    # D1 + D2 — 상품매출 + 배송비매출 - 판매자쿠폰
    assert result.revenue == 18_000 + 3_000 - 1_000
    # D3 — 벤더 실측 수수료
    assert result.marketplace_fee == 2_000
    # **정산서가 주지 않는 항목은 장부 값을 지우지 않는다.**
    # 예전에는 msr.ad_cost(적재 시 0)로 덮어써서 장부 광고비가 사라졌다.
    assert result.ad_cost == 999
    assert result.settlement_snapshot["basis"] == "SETTLEMENT_ACTUAL"
    assert result.settlement_snapshot["delivery_revenue"] == 3_000
    assert result.settlement_snapshot["seller_coupon"] == 1_000


def test_settlement_record_from_another_order_is_rejected(db):
    """이 주문에 매칭되지 않은 정산 레코드를 지목하면 막는다.

    예전에는 아무 행이나 받아 그 금액으로 계산했다 — 남의 주문 정산서를
    붙여도 통과했다.
    """
    org_id = f.make_organization(db)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=None, sale_date=date(2026, 6, 5),
        sale_price=20_000,
    )
    stray = f.make_marketplace_settlement_record(db, org_id=org_id, gross_revenue=18_000)

    with pytest.raises(ValidationError):
        calculate_actual_margin(
            ActualMarginInput(org_id=org_id, sales_order_id=so_id,
                              settlement_record_id=stray), db)


def test_unmatched_order_keeps_policy_estimate(db):
    """정산서가 안 붙은 주문은 장부 + 정책 추정 그대로. 0 으로 떨어뜨리지 않는다."""
    org_id = f.make_organization(db)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=None, sale_date=date(2026, 6, 5),
        sale_price=20_000, purchase_cost=5_000,
    )
    result = calculate_actual_margin(
        ActualMarginInput(org_id=org_id, sales_order_id=so_id), db)
    assert result.revenue == 20_000
    assert result.settlement_snapshot["basis"] == "LEDGER_AND_POLICY"


def test_calculate_actual_margin_raises_not_found_for_missing_sales_order(db):
    org_id = f.make_organization(db)
    with pytest.raises(NotFoundError):
        calculate_actual_margin(
            ActualMarginInput(org_id=org_id, sales_order_id=org_id), db
        )


def test_save_actual_margin_persists_row_and_feeds_settlement_direct_cost(db):
    """product_margin_actual 미영속화 gap 회귀 테스트.

    settlement.py 의 direct_cost 는 product_margin_actual 을 SELECT 하므로,
    save_actual_margin 을 호출하지 않으면 실마진을 계산해도 월정산에 안 잡힌다.
    """
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id, cost_amount=7_000)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 5),
        sale_price=20_000, quantity=1, ad_cost=1_000,
    )

    inp = ActualMarginInput(org_id=org_id, sales_order_id=so_id)
    result = calculate_actual_margin(inp, db)
    saved_id = save_actual_margin(inp, result, db)

    row = db.execute(
        text(
            "SELECT product_id, sales_order_id, actual_margin_amount"
            "  FROM product_margin_actual WHERE id = :id"
        ),
        {"id": str(saved_id)},
    ).first()
    assert str(row.product_id) == str(product_id)
    assert str(row.sales_order_id) == str(so_id)
    assert int(row.actual_margin_amount) == result.margin_amount

    direct_cost_row = db.execute(
        text(
            "SELECT COALESCE(SUM(actual_cost + actual_marketplace_fee + actual_ad_cost"
            "  + actual_fulfillment_fee + actual_return_cost + actual_vat), 0) AS c"
            "  FROM product_margin_actual WHERE sales_order_id = :sid"
        ),
        {"sid": str(so_id)},
    ).first()
    assert int(direct_cost_row.c) == 7_000 + 1_000  # cost + ad_cost, 나머지 MVP 0


def test_save_actual_margin_upserts_on_recalculate(db):
    org_id = f.make_organization(db)
    product_id = f.make_product(db, org_id=org_id, cost_amount=5_000)
    so_id = f.make_sales_order(
        db, org_id=org_id, product_id=product_id, sale_date=date(2026, 6, 5),
        sale_price=20_000, quantity=1, ad_cost=999,
    )

    inp = ActualMarginInput(org_id=org_id, sales_order_id=so_id)
    first_id = save_actual_margin(inp, calculate_actual_margin(inp, db), db)

    # 정산서가 나중에 붙는다 — 같은 주문을 다시 계산한다.
    _match_settlement(db, org_id, so_id, product_revenue=18_000,
                      delivery_revenue=0, coupon=0, fee=1_800)
    second_id = save_actual_margin(inp, calculate_actual_margin(inp, db), db)

    assert first_id == second_id  # sales_order_id UNIQUE -> 재계산은 갱신, 신규 행 아님

    count_row = db.execute(
        text("SELECT COUNT(*) AS c FROM product_margin_actual WHERE sales_order_id = :sid"),
        {"sid": str(so_id)},
    ).first()
    assert count_row.c == 1

    row = db.execute(
        text(
            """
            SELECT actual_revenue, actual_marketplace_fee, settlement_record_id,
                   calculation_snapshot_json
              FROM product_margin_actual WHERE id = :id
            """
        ),
        {"id": str(second_id)},
    ).first()
    assert int(row.actual_revenue) == 18_000
    assert int(row.actual_marketplace_fee) == 1_800
    # 계산에 실제로 쓴 대표 행과 근거가 함께 남는다.
    assert row.settlement_record_id is not None
    assert row.calculation_snapshot_json["basis"] == "SETTLEMENT_ACTUAL"
