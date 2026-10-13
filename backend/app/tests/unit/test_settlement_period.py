from datetime import date

from app.services.settlement import month_range


def test_month_range_normal_month():
    s, e = month_range("2026-06")
    assert s == date(2026, 6, 1)
    assert e == date(2026, 6, 30)


def test_month_range_february_leap():
    s, e = month_range("2024-02")
    assert s == date(2024, 2, 1)
    assert e == date(2024, 2, 29)


def test_month_range_february_non_leap():
    _, e = month_range("2026-02")
    assert e == date(2026, 2, 28)


def test_month_range_december():
    s, e = month_range("2025-12")
    assert s == date(2025, 12, 1)
    assert e == date(2025, 12, 31)
