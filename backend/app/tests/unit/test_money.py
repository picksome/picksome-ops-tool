from decimal import Decimal

from app.core.money import apply_rate, split_vat, to_krw


def test_to_krw_rounds_half_up():
    assert to_krw(Decimal("0.5")) == 1
    assert to_krw(Decimal("1.4999")) == 1
    assert to_krw(Decimal("1.5")) == 2
    assert to_krw(100) == 100


def test_apply_rate_rounds_to_int_krw():
    # 10000 * 0.108 = 1080
    assert apply_rate(10_000, Decimal("0.108")) == 1080
    # 12345 * 0.025 = 308.625 → 309
    assert apply_rate(12_345, Decimal("0.025")) == 309


def test_split_vat_included_default():
    # 11000 (vat 10% 포함) → 공급 10000, 부가세 1000
    supply, vat = split_vat(11_000, Decimal("0.10"), "INCLUDED")
    assert supply == 10_000
    assert vat == 1_000
    assert supply + vat == 11_000


def test_split_vat_excluded():
    supply, vat = split_vat(10_000, Decimal("0.10"), "EXCLUDED")
    assert supply == 10_000
    assert vat == 1_000


def test_split_vat_exempt():
    supply, vat = split_vat(50_000, Decimal("0.10"), "EXEMPT")
    assert supply == 50_000
    assert vat == 0
