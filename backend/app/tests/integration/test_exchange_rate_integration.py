"""services/exchange_rate.py 통합 테스트 (M008, docs/27 D-288)."""

from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationError
from app.domain.product import ProductCreate
from app.services.exchange_rate import get_latest_rate, sync_daily_rates, upsert_manual_rate
from app.services.product import create_product
from app.tests.fixtures import factories as f


def test_sync_without_api_key_raises(db, monkeypatch):
    monkeypatch.setattr("app.services.exchange_rate.get_settings", lambda: type(
        "S", (), {"KOREAEXIM_API_KEY": None}
    )())
    with pytest.raises(ValidationError):
        sync_daily_rates(db, rate_date=date(2026, 7, 28), currencies=["USD"])


def test_manual_upsert_and_latest_lookup(db):
    upsert_manual_rate(db, currency="USD", rate_date=date(2026, 7, 28), rate_to_krw=Decimal("1360.5000"))
    upsert_manual_rate(db, currency="USD", rate_date=date(2026, 7, 29), rate_to_krw=Decimal("1362.0000"))

    assert get_latest_rate(db, currency="USD") == Decimal("1362.0000")
    assert get_latest_rate(db, currency="USD", at=date(2026, 7, 28)) == Decimal("1360.5000")
    assert get_latest_rate(db, currency="EUR") is None


def test_manual_upsert_idempotent_same_day(db):
    upsert_manual_rate(db, currency="USD", rate_date=date(2026, 7, 28), rate_to_krw=Decimal("1360.0000"))
    upsert_manual_rate(db, currency="USD", rate_date=date(2026, 7, 28), rate_to_krw=Decimal("1365.0000"))
    assert get_latest_rate(db, currency="USD", at=date(2026, 7, 28)) == Decimal("1365.0000")


def test_product_auto_fills_fx_rate_from_snapshot(db):
    upsert_manual_rate(db, currency="USD", rate_date=date.today(), rate_to_krw=Decimal("1350.0000"))
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)

    out = create_product(
        db, org_id=org_id, actor=actor,
        body=ProductCreate(
            name_ko="해외상품", margin_type="OVERSEAS_DIRECT",
            cost_currency="USD", cost_amount_foreign=100,
        ),
    )
    assert out.cost_fx_rate == Decimal("1350.0000")
    assert out.cost_amount == 135000


def test_product_manual_fx_rate_overrides_snapshot(db):
    upsert_manual_rate(db, currency="USD", rate_date=date.today(), rate_to_krw=Decimal("1350.0000"))
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)

    out = create_product(
        db, org_id=org_id, actor=actor,
        body=ProductCreate(
            name_ko="해외상품 수동환율", margin_type="OVERSEAS_DIRECT",
            cost_currency="USD", cost_amount_foreign=100, cost_fx_rate=Decimal("1400.0000"),
        ),
    )
    assert out.cost_fx_rate == Decimal("1400.0000")
    assert out.cost_amount == 140000
