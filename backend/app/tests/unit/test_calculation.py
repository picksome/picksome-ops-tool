from decimal import Decimal
from uuid import UUID

import pytest

from app.domain.margin import ExpectedMarginInput
from app.domain.policy import FulfillmentFeePolicyRow
from app.services.calculation import (
    apply_tiered_fee,
    calc_ad_cost,
    calc_fulfillment_fee,
    calc_marketplace_fee,
    calc_payment_fee,
    calc_total_cost_for_margin_type,
    compute_expected_margin,
    reverse_calc_price_for_target_margin,
)
from app.tests.fixtures.policies import base_policies


def _input(sale_price=20_000, cost=10_000, margin_type="GENERAL_RESELL"):
    from uuid import UUID

    return ExpectedMarginInput(
        org_id=UUID(int=1),
        product_id=UUID(int=2),
        margin_type=margin_type,
        marketplace="COUPANG",
        cost_amount=cost,
        sale_price=sale_price,
        payment_method="BUSINESS_CARD",
        extras={},
    )


def test_marketplace_fee_rate():
    pol = base_policies(mfee_rate="0.1080").marketplace_fee
    assert calc_marketplace_fee(10_000, pol) == 1_080


def test_marketplace_fee_fixed():
    pol = base_policies().marketplace_fee
    pol.fee_type = "FIXED"
    pol.fixed_amount = 500
    assert calc_marketplace_fee(99_999, pol) == 500


def test_marketplace_fee_tiered():
    tiers = [
        {"min": 0, "max": 10_000, "rate": 0.05},
        {"min": 10_000, "max": 50_000, "rate": 0.10},
        {"min": 50_000, "rate": 0.15},
    ]
    assert apply_tiered_fee(5_000, tiers) == 250
    assert apply_tiered_fee(15_000, tiers) == 1_500
    assert apply_tiered_fee(100_000, tiers) == 15_000


def test_payment_fee():
    pol = base_policies().payment_fee
    assert calc_payment_fee(10_000, pol) == 250


def test_compute_expected_margin_basic_general_resell():
    # sale 20000, cost 10000, mfee 10.8%, pfee 2.5%, ad 5%, return 3%, vat 10%
    result = compute_expected_margin(_input(), base_policies())
    assert result.marketplace_fee == 2_160  # 20000 * 0.108
    assert result.payment_fee == 500  # 20000 * 0.025
    assert result.ad_cost == 1_000  # 20000 * 0.05
    assert result.fulfillment_fee == 0  # GENERAL_RESELL
    assert result.return_risk_cost == 600  # 20000 * 0.03
    # vat_base = 20000 - 2160 = 17840 → 17840 * 0.10 = 1784
    assert result.vat == 1_784
    # gross_in = 20000 - 2160 - 500 - 1000 - 0 - 600 = 15740
    # margin = 15740 - 10000 - 1784 = 3956
    assert result.margin_amount == 3_956
    assert result.margin_rate == Decimal("0.1978")  # 3956/20000


def test_compute_expected_margin_override():
    inp = _input()
    inp.override_fields = {"marketplace_fee": 1_500}
    result = compute_expected_margin(inp, base_policies())
    assert result.marketplace_fee == 1_500
    assert "사용자 수동 override 적용됨" in result.warnings
    # 과세표준 = sale_price - marketplace_fee 이므로 수수료를 override 하면 VAT 도
    # 따라 바뀌어야 한다. (20000 - 1500) * 0.10 = 1850 — 정책 요율 기준 1784 가 아니다.
    assert result.vat == 1_850
    # gross_in = 20000 - 1500 - 500 - 1000 - 0 - 600 = 16400
    # margin = 16400 - 10000 - 1850 = 4550
    assert result.margin_amount == 4_550


def test_explicit_vat_override_wins_over_marketplace_fee_recompute():
    # vat 를 직접 지정하면 사용자 값이 우선 — 수수료 override 에 딸려 덮어쓰지 않는다.
    inp = _input()
    inp.override_fields = {"marketplace_fee": 1_500, "vat": 99}
    result = compute_expected_margin(inp, base_policies())
    assert result.vat == 99
    assert result.margin_amount == 16_400 - 10_000 - 99


def test_compute_expected_margin_purchase_agency_adds_extras():
    inp = _input(sale_price=30_000, cost=10_000, margin_type="PURCHASE_AGENCY")
    inp.extras = {"agency_fee": 2_000, "intl_shipping": 3_000}
    result = compute_expected_margin(inp, base_policies())
    # effective cost = 10000 + 2000 + 3000 = 15000
    # mfee = 30000 * 0.108 = 3240, pfee = 750, ad = 1500, return = 900
    # vat = (30000 - 3240) * 0.10 = 2676
    # gross_in = 30000 - 3240 - 750 - 1500 - 0 - 900 = 23610
    # margin = 23610 - 15000 - 2676 = 5934
    assert result.margin_amount == 5_934


def test_compute_expected_margin_warns_needs_review():
    pol = base_policies()
    pol.marketplace_fee.reliability_status = "NEEDS_REVIEW"
    result = compute_expected_margin(_input(), pol)
    assert any("NEEDS_REVIEW" in w for w in result.warnings)


def test_reverse_price_recovers_target_rate():
    pol = base_policies(ad_ratio="0.0500")
    target = Decimal("0.20")
    price = reverse_calc_price_for_target_margin(
        target, cost_amount=10_000, margin_type="GENERAL_RESELL", policies=pol, extras={}
    )
    # 역산 가격으로 다시 마진 계산 → margin_rate ≈ target_rate (±1원 반올림 오차 허용)
    inp = _input(sale_price=price)
    result = compute_expected_margin(inp, pol)
    assert abs(result.margin_rate - target) <= Decimal("0.0005")


def test_reverse_price_raises_when_target_infeasible():
    pol = base_policies()
    with pytest.raises(ValueError):
        reverse_calc_price_for_target_margin(
            Decimal("0.95"), 10_000, "GENERAL_RESELL", pol, {}
        )


def test_compute_expected_margin_rejects_zero_price():
    inp = _input(sale_price=0)
    with pytest.raises(ValueError):
        compute_expected_margin(inp, base_policies())


def test_marketplace_fee_unknown_type_raises():
    pol = base_policies().marketplace_fee
    pol.fee_type = "UNKNOWN"
    with pytest.raises(ValueError):
        calc_marketplace_fee(10_000, pol)


def test_calc_ad_cost_direct_amount_overrides_ratio():
    # extras.ad_cost 는 원 단위 직접 지정 — 정책 default_ad_ratio 보다 우선.
    assert calc_ad_cost(20_000, {"ad_cost": 777}, Decimal("0.05")) == 777


def test_calc_ad_cost_ratio_override_beats_policy_default():
    assert calc_ad_cost(20_000, {"ad_ratio_override": "0.10"}, Decimal("0.05")) == 2_000


def test_calc_ad_cost_falls_back_to_policy_default():
    assert calc_ad_cost(20_000, {}, Decimal("0.05")) == 1_000


def test_calc_ad_cost_zero_when_nothing_configured():
    assert calc_ad_cost(20_000, {}, None) == 0


def test_calc_fulfillment_fee_general_resell_always_zero():
    pol = base_policies()
    assert calc_fulfillment_fee("GENERAL_RESELL", pol, {"storage_days": 10}) == 0


def test_calc_fulfillment_fee_rocket_growth_sums_inbound_outbound_storage():
    pol = base_policies()
    pol.fulfillment = FulfillmentFeePolicyRow(
        id=UUID(int=5),
        provider="COUPANG_ROCKET",
        inbound_fee=500,
        outbound_fee=800,
        storage_fee_per_day=50,
        return_handling_fee=None,
        extra_json=None,
        version=1,
    )
    assert calc_fulfillment_fee("ROCKET_GROWTH", pol, {"storage_days": 10}) == 500 + 800 + 50 * 10


def test_calc_fulfillment_fee_rocket_growth_no_policy_uses_extras_fallback():
    pol = base_policies()
    assert pol.fulfillment is None
    assert calc_fulfillment_fee("ROCKET_GROWTH", pol, {"fulfillment_fee": 1_234}) == 1_234


def test_calc_total_cost_overseas_direct_adds_customs_and_fx():
    cost = calc_total_cost_for_margin_type(
        "OVERSEAS_DIRECT", 10_000, {"customs": 1_000, "fx_adjustment": 500}
    )
    assert cost == 11_500


def test_compute_expected_margin_multi_field_override():
    inp = _input()
    inp.override_fields = {
        "fulfillment_fee": 300,
        "return_risk_cost": 100,
        "vat": 50,
    }
    result = compute_expected_margin(inp, base_policies())
    assert result.fulfillment_fee == 300
    assert result.return_risk_cost == 100
    assert result.vat == 50
    # gross_in = 20000 - 2160(mfee) - 500(pfee) - 1000(ad) - 300(fulfill) - 100(return) = 15940
    # margin = 15940 - 10000(cost) - 50(vat) = 5890
    assert result.margin_amount == 5_890


def test_reverse_price_with_fixed_marketplace_fee():
    """정액 수수료에서도 역산가가 목표 마진과 정확히 맞는가.

    오래 xfail 이었다. 정액 수수료는 **과세표준을 깎으므로**
    `vat = (sp - mfee) * vat_rate` — 실질 부담이 `mfee * (1 - vat_rate)` 다.
    그 가중치가 빠져 역산가가 높게 나왔고 실제 마진이 목표를 넘었다.
    """
    pol = base_policies()
    pol.marketplace_fee.fee_type = "FIXED"
    pol.marketplace_fee.fixed_amount = 1_000
    pol.marketplace_fee.rate = None
    target = Decimal("0.20")
    price = reverse_calc_price_for_target_margin(
        target, cost_amount=10_000, margin_type="GENERAL_RESELL", policies=pol, extras={}
    )
    inp = _input(sale_price=price)
    result = compute_expected_margin(inp, pol)
    assert abs(result.margin_rate - target) <= Decimal("0.0005")


def test_reverse_price_includes_rocket_growth_fulfillment_fee():
    pol = base_policies()
    pol.fulfillment = FulfillmentFeePolicyRow(
        id=UUID(int=6), provider="COUPANG_ROCKET", inbound_fee=500, outbound_fee=500,
        storage_fee_per_day=0, return_handling_fee=None, extra_json=None, version=1,
    )
    target = Decimal("0.20")
    price = reverse_calc_price_for_target_margin(
        target, cost_amount=10_000, margin_type="ROCKET_GROWTH", policies=pol, extras={}
    )
    inp = _input(sale_price=price, margin_type="ROCKET_GROWTH")
    result = compute_expected_margin(inp, pol)
    assert abs(result.margin_rate - target) <= Decimal("0.0005")


def test_reverse_price_with_tiered_marketplace_fee():
    """구간 수수료를 **무시하지 않는다.**

    예전에는 TIERED 를 통째로 건너뛰어 수수료가 0인 것처럼 역산했다. 목표
    20% 로 나온 가격의 실제 마진이 11% 였다 — 판매자는 9%p 를 손해 보면서
    그 사실을 모른다.
    """
    pol = base_policies(pfee_rate="0.0000", ad_ratio="0.0000",
                        return_rate="0.0000")
    pol.marketplace_fee.fee_type = "TIERED"
    pol.marketplace_fee.rate = None
    pol.marketplace_fee.tier_json = [
        {"min": 0, "max": 50_000, "rate": 0.10},
        {"min": 50_000, "rate": 0.08},
    ]
    target = Decimal("0.20")
    price = reverse_calc_price_for_target_margin(
        target, cost_amount=10_000, margin_type="GENERAL_RESELL",
        policies=pol, extras={})

    result = compute_expected_margin(_input(sale_price=price), pol)
    assert abs(result.margin_rate - target) <= Decimal("0.0005")
    # 수수료를 무시했다면 "정책 없음" 가격과 같아진다 — 그게 예전 동작이었다.
    pol_none = base_policies(pfee_rate="0.0000", ad_ratio="0.0000",
                             return_rate="0.0000")
    pol_none.marketplace_fee = None
    assert price != reverse_calc_price_for_target_margin(
        target, cost_amount=10_000, margin_type="GENERAL_RESELL",
        policies=pol_none, extras={})


def test_reverse_price_refuses_when_no_tier_is_consistent():
    """어느 구간에도 안 맞으면 값을 지어내지 않는다."""
    pol = base_policies()
    pol.marketplace_fee.fee_type = "TIERED"
    pol.marketplace_fee.rate = None
    # 해가 절대 들어올 수 없는 좁은 구간만 둔다.
    pol.marketplace_fee.tier_json = [{"min": 0, "max": 1, "rate": 0.10}]
    with pytest.raises(ValueError):
        reverse_calc_price_for_target_margin(
            Decimal("0.20"), cost_amount=10_000,
            margin_type="GENERAL_RESELL", policies=pol, extras={})
