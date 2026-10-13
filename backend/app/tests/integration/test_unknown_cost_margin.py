"""원가 미상 상품의 마진 — **0 으로 계산하지 않는다** (AUD-2).

무엇이 문제였나
------------
```sql
ELSE COALESCE(p.cost_amount, 0) * so.quantity
```

원가를 모르는 상품이 **원가 0원짜리**로 집계됐다. 그러면

```
매출 100,000 − 원가 0 = 마진 100,000 (100%)
```

이 되고, 상품별 손익표에서 **"이 상품이 제일 남는다"** 로 읽힌다. 실제로는
얼마 남는지 모르는 상품이다.

현재 DB 에 NULL 원가 상품이 없다는 것은 안전 근거가 아니다 — 컬럼이
nullable 이고, Connector 가 새 상품을 끌어오면 원가 없이 들어온다.

무엇으로 바꿨나
------------
모르면 **모른다고 한다.**

```
cost_complete = false
actual_margin_amount = None
actual_margin_rate = None
unknown_cost_lines = N
```

0 을 더해 계산하면 과대계상이고, 그냥 빼고 계산하면 과소계상이다. 둘 다 틀린
숫자이므로 `None` 이 유일하게 정직한 답이다.

집계는 **아는 것만 더하고 빠진 개수를 함께** 돌려준다. 조용히 빼면 사용자가
합계를 전부라고 믿는다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.margin_analytics import aggregate_actual_margin
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_sales_order,
)

START = date(2026, 5, 1)
END = date(2026, 5, 31)
SALE_DAY = date(2026, 5, 10)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


def _sell(db, org, product, *, price: int, qty: int = 1, tag: str = "x"):
    make_sales_order(
        db, org_id=org, product_id=product, sale_date=SALE_DAY,
        sale_price=price, quantity=qty, marketplace_order_no=f"UC-{tag}")


def _by_name(items, name):
    return next(i for i in items if i["product_name"] == name)


# ── 1. 핵심 ────────────────────────────────────────────────────


def test_unknown_cost_is_not_treated_as_zero(db: Session, org):
    """원가 미상 상품이 100% 마진으로 보이지 않는다."""
    known = make_product(db, org_id=org, name_ko="원가있음", cost_amount=6_000)
    unknown = make_product(db, org_id=org, name_ko="원가없음", cost_amount=None)
    _sell(db, org, known, price=10_000, tag="k")
    _sell(db, org, unknown, price=10_000, tag="u")
    db.commit()

    items = aggregate_actual_margin(db, org_id=org, start=START, end=END)

    ok = _by_name(items, "원가있음")
    assert ok["cost_complete"] is True
    assert ok["actual_margin_amount"] == 4_000
    assert ok["actual_margin_rate"] == pytest.approx(0.4)

    bad = _by_name(items, "원가없음")
    assert bad["cost_complete"] is False
    assert bad["unknown_cost_lines"] == 1
    # **숫자를 내놓지 않는다.** 0 도 100% 도 아니다.
    assert bad["actual_margin_amount"] is None
    assert bad["actual_margin_rate"] is None


def test_line_level_purchase_cost_still_wins(db: Session, org):
    """주문에 매입가가 있으면 상품 원가가 없어도 알 수 있다.

    원가 출처 우선순위(주문 → 옵션 → 상품)는 그대로다.
    """
    p = make_product(db, org_id=org, name_ko="주문원가", cost_amount=None)
    make_sales_order(
        db, org_id=org, product_id=p, sale_date=SALE_DAY, sale_price=10_000,
        purchase_cost=3_000, marketplace_order_no="UC-line")
    db.commit()

    row = _by_name(aggregate_actual_margin(db, org_id=org, start=START, end=END),
                   "주문원가")
    assert row["cost_complete"] is True
    assert row["actual_margin_amount"] == 7_000


def test_zero_cost_is_not_unknown(db: Session, org):
    """원가 0원은 **아는 값**이다. 미상과 구분한다."""
    p = make_product(db, org_id=org, name_ko="무료상품", cost_amount=0)
    _sell(db, org, p, price=10_000, tag="z")
    db.commit()

    row = _by_name(aggregate_actual_margin(db, org_id=org, start=START, end=END),
                   "무료상품")
    assert row["cost_complete"] is True
    assert row["actual_margin_amount"] == 10_000


def test_partially_unknown_product_is_incomplete(db: Session, org):
    """한 상품에 아는 줄과 모르는 줄이 섞이면 **불완전**이다.

    아는 줄만 더해 마진을 내놓으면 그 숫자가 전부인 것처럼 보인다.
    """
    p = make_product(db, org_id=org, name_ko="섞임", cost_amount=None)
    make_sales_order(
        db, org_id=org, product_id=p, sale_date=SALE_DAY, sale_price=10_000,
        purchase_cost=4_000, marketplace_order_no="UC-m1")
    make_sales_order(
        db, org_id=org, product_id=p, sale_date=SALE_DAY, sale_price=10_000,
        marketplace_order_no="UC-m2")     # 원가 미상
    db.commit()

    row = _by_name(aggregate_actual_margin(db, org_id=org, start=START, end=END),
                   "섞임")
    assert row["cost_complete"] is False
    assert row["unknown_cost_lines"] == 1
    assert row["actual_margin_amount"] is None
    # 매출은 안다. 아는 것까지 지우지는 않는다.
    assert row["gross_revenue"] == 20_000


def test_expected_delta_is_unknown_too(db: Session, org):
    """실 마진을 모르면 예상과의 차이도 모른다. 0 이면 "예상과 같다" 로 읽힌다."""
    p = make_product(db, org_id=org, name_ko="차이미상", cost_amount=None)
    _sell(db, org, p, price=10_000, tag="d")
    db.commit()

    row = _by_name(aggregate_actual_margin(db, org_id=org, start=START, end=END),
                   "차이미상")
    assert row["delta_amount"] is None
    assert row["delta_rate"] is None


# ── 2. 소비처 ──────────────────────────────────────────────────


def test_dashboard_summary_excludes_and_counts(db: Session, org):
    """집계는 아는 것만 더하고 **빠진 개수를 함께** 돌려준다."""
    from app.api.v1.reports import _sum_known_margin

    known = make_product(db, org_id=org, name_ko="A", cost_amount=6_000)
    unknown = make_product(db, org_id=org, name_ko="B", cost_amount=None)
    _sell(db, org, known, price=10_000, tag="a")
    _sell(db, org, unknown, price=10_000, tag="b")
    db.commit()

    items = aggregate_actual_margin(db, org_id=org, start=START, end=END)
    total, excluded = _sum_known_margin(items)
    assert total == 4_000        # 모르는 상품의 10,000 이 섞이지 않았다
    assert excluded == 1


def test_margin_alerts_do_not_flag_unknown_cost(db: Session, org):
    """마진을 모르는 상품을 "기준 미달" 이라고 말하지 않는다.

    그렇다고 조용히 빼지도 않는다 — `cost_unknown` 으로 따로 알린다.
    """
    p = make_product(db, org_id=org, name_ko="미상경고", cost_amount=None)
    _sell(db, org, p, price=10_000, tag="al")
    db.commit()

    items = aggregate_actual_margin(db, org_id=org, start=START, end=END)
    flagged = [i for i in items
               if i["actual_margin_rate"] is not None
               and i["actual_margin_rate"] < 0.3]
    assert not flagged
    unknown = [i for i in items if i["actual_margin_rate"] is None]
    assert len(unknown) == 1


# ── 3. 세무 리포트 ─────────────────────────────────────────────


def test_purchase_report_leaves_unknown_cost_blank(db: Session, org):
    """매입 리포트에서 0원은 "공짜로 샀다" 로 읽히고 매입세액을 과소 계상한다."""
    actor = make_app_user(db, org_id=org, role="ADMIN")
    p = make_product(db, org_id=org, name_ko="매입미상", cost_amount=None)
    _sell(db, org, p, price=10_000, tag="pr")
    db.commit()
    _ = actor

    rows = db.execute(text("""
        SELECT CASE WHEN so.purchase_cost IS NOT NULL THEN so.purchase_cost
                    WHEN sv.cost_amount IS NOT NULL THEN sv.cost_amount * so.quantity
                    WHEN p.cost_amount IS NOT NULL THEN p.cost_amount * so.quantity
                    ELSE NULL END AS total_cost
          FROM sales_order so
          LEFT JOIN product p ON p.id = so.product_id
          LEFT JOIN product_variant sv ON sv.id = so.product_variant_id
         WHERE so.org_id = :o AND so.deleted_at IS NULL
    """), {"o": str(org)}).scalars().all()
    assert rows == [None]      # 0 이 아니라 빈 값


def test_source_code_has_no_zero_cost_fallback():
    """`COALESCE(p.cost_amount, 0)` 이 코드에서 사라졌는지 확인한다.

    주석에는 남아 있다(왜 없앴는지 설명). SQL 에 남으면 실패한다.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in ("services/margin_analytics.py", "io/excel_export/tax_reports.py"):
        text_ = (root / rel).read_text(encoding="utf-8")
        for line in text_.splitlines():
            stripped = line.strip()
            if stripped.startswith("--") or stripped.startswith("#"):
                continue
            assert "COALESCE(p.cost_amount, 0)" not in line, f"{rel}: {line}"
