"""services/expense._split_amounts 순수 로직 단위 테스트 (D-284 VAT 분기).

DB 의존 없는 (supply, vat) 산출 함수만 대상 — CRUD 는 통합 테스트에서 별도 검증.
"""

from decimal import Decimal

import pytest

from app.core.exceptions import InvalidVatInputError
from app.services.expense import _split_amounts


def test_included_splits_out_vat_from_gross():
    supply, vat = _split_amounts(
        vat_inclusion="INCLUDED", gross=11_000, supply=None, vat=None,
        vat_rate=Decimal("0.10"), is_manual_override=False,
    )
    assert (supply, vat) == (10_000, 1_000)


def test_included_zero_vat_rate_no_split():
    supply, vat = _split_amounts(
        vat_inclusion="INCLUDED", gross=10_000, supply=None, vat=None,
        vat_rate=Decimal("0"), is_manual_override=False,
    )
    assert (supply, vat) == (10_000, 0)


def test_included_rounding_half_up():
    # 10005 * 0.10 / 1.10 = 909.545... -> HALF_UP -> 910
    supply, vat = _split_amounts(
        vat_inclusion="INCLUDED", gross=10_005, supply=None, vat=None,
        vat_rate=Decimal("0.10"), is_manual_override=False,
    )
    assert vat == 910
    assert supply == 10_005 - 910


def test_excluded_uses_supplied_supply_amount():
    supply, vat = _split_amounts(
        vat_inclusion="EXCLUDED", gross=11_000, supply=10_000, vat=None,
        vat_rate=Decimal("0.10"), is_manual_override=False,
    )
    assert (supply, vat) == (10_000, 1_000)


def test_excluded_without_supply_treats_gross_as_supply():
    supply, vat = _split_amounts(
        vat_inclusion="EXCLUDED", gross=10_000, supply=None, vat=None,
        vat_rate=Decimal("0.10"), is_manual_override=False,
    )
    assert (supply, vat) == (10_000, 1_000)


def test_exempt_always_zero_vat():
    supply, vat = _split_amounts(
        vat_inclusion="EXEMPT", gross=10_000, supply=9_000, vat=500,
        vat_rate=Decimal("0.10"), is_manual_override=False,
    )
    assert (supply, vat) == (10_000, 0)


def test_manual_override_accepts_consistent_input():
    supply, vat = _split_amounts(
        vat_inclusion="INCLUDED", gross=10_000, supply=9_500, vat=500,
        vat_rate=Decimal("0.10"), is_manual_override=True,
    )
    assert (supply, vat) == (9_500, 500)


def test_manual_override_rejects_inconsistent_sum():
    with pytest.raises(InvalidVatInputError):
        _split_amounts(
            vat_inclusion="INCLUDED", gross=10_000, supply=9_000, vat=500,
            vat_rate=Decimal("0.10"), is_manual_override=True,
        )


def test_manual_override_defaults_missing_vat_to_zero():
    supply, vat = _split_amounts(
        vat_inclusion="EXEMPT", gross=10_000, supply=10_000, vat=None,
        vat_rate=Decimal("0.10"), is_manual_override=True,
    )
    assert (supply, vat) == (10_000, 0)
