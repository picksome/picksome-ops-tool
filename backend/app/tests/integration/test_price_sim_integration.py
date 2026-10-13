"""services/price_sim.py 통합 테스트 — 정책 조회 + 역산가 + 검증용 재계산 전체 경로."""

from datetime import date
from decimal import Decimal

from app.domain.margin import PriceSimInput
from app.services.price_sim import simulate_recommended_prices
from app.tests.fixtures import factories as f


def test_simulate_recommended_prices_matches_each_target_rate(db):
    org_id = f.make_organization(db)
    f.make_tax_policy(db, org_id=org_id, vat_rate=Decimal("0.10"))
    f.make_marketplace_fee_policy(db, org_id=org_id, rate=Decimal("0.1080"))
    f.make_payment_fee_policy(db, org_id=org_id, rate=Decimal("0.0250"))
    f.make_return_risk_policy(db, org_id=org_id, return_rate=Decimal("0.03"))
    f.make_ad_cost_policy(db, org_id=org_id, default_ad_ratio=Decimal("0.05"))

    targets = [Decimal("0.10"), Decimal("0.20"), Decimal("0.30")]
    result = simulate_recommended_prices(
        PriceSimInput(
            org_id=org_id, margin_type="GENERAL_RESELL", marketplace="COUPANG",
            cost_amount=10_000, payment_method="BUSINESS_CARD", target_rates=targets,
        ),
        db,
        at=date(2026, 6, 1),
    )

    assert len(result) == 3
    for row, target in zip(result, targets, strict=True):
        assert row.target_rate == target
        assert row.recommended_price > 0
        assert abs(row.expected_margin.margin_rate - target) <= Decimal("0.0005")
