"""재고 원장 · 발주 · 재주문 (M048).

이 파일이 고정하는 것
------------------
1. 재고는 **원장 합계**다 — 덮어쓰기가 아니다
2. 조정에는 사유가 **필수**다
3. 입고는 발주와 재고를 **한 트랜잭션**으로 움직인다
4. 같은 입고 사건 재전송은 **막힌다** (키를 준 경우)
5. 같은 수량 두 번 입고는 **막지 않는다** — 정상적인 일이다
6. 끝난 발주는 되살아나지 않는다
7. 재주문은 **모자란 데이터를 0 으로 채우지 않는다**
8. 재주문에 **기본 임계값이 없다**
9. 미입고 발주를 빼먹지 않는다 — 이미 주문한 것을 또 권하지 않는다
10. MOQ 는 공급처 값이다

7·8 이 이 파일의 핵심이다
----------------------
재고 시스템이 잘못된 숫자를 자신 있게 말하면, 사람이 그걸 믿고 돈을 쓴다.
모르는 것은 모른다고 해야 한다.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services import inventory, purchasing, reorder
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_sales_order,
)

TODAY = date(2026, 8, 11)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org)


@pytest.fixture
def supplier(db: Session, org: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code)
        VALUES (:id, :org, :code, '테스트공급처', 'KR')
    """), {"id": str(sid), "org": str(org), "code": f"S-{sid.hex[:8]}"})
    return sid


def _source(
    db: Session, *, org: uuid.UUID, product_id: uuid.UUID, supplier_id: uuid.UUID,
    lead_time_days: int | None = 7, moq: int | None = None,
) -> uuid.UUID:
    """소싱처 연결. `lead_time_days=None` 으로 결측을 만들 수 있다."""
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_source (
          id, org_id, product_id, supplier_site_id, lead_time_days, moq,
          is_primary, status, currency
        ) VALUES (:id, :org, :p, :s, :lt, :moq, TRUE, 'ACTIVE', 'KRW')
    """), {
        "id": str(sid), "org": str(org), "p": str(product_id),
        "s": str(supplier_id), "lt": lead_time_days, "moq": moq,
    })
    return sid


# ── 1. 원장 ────────────────────────────────────────────────────


def test_balance_is_sum_of_movements(db: Session, org, actor):
    """재고는 저장된 숫자가 아니라 움직임의 합이다."""
    p = make_product(db, org_id=org)
    for qty, ttype in ((10, "OPENING"), (5, "PURCHASE_RECEIPT"), (-3, "SALE")):
        inventory.record_transaction(
            db, org_id=org, product_id=p, product_variant_id=None,
            transaction_type=ttype, quantity_delta=qty, actor_user_id=actor,
            unit_cost=1000 if ttype in ("OPENING", "PURCHASE_RECEIPT") else None,
        )
    db.commit()

    bal = inventory.get_balance(
        db, org_id=org, product_id=p, product_variant_id=None)
    assert bal["on_hand"] == 12
    assert bal["tracked"] is True


def test_untracked_is_not_zero(db: Session, org):
    """움직임이 없는 상품은 `on_hand=0` 이지만 `tracked=False` 다.

    "세어 봤더니 0" 과 "재고를 관리한 적 없다" 는 다른 상태다. 화면이 둘을
    같게 보여주면 사용자가 재고가 떨어졌다고 착각한다.
    """
    p = make_product(db, org_id=org)
    bal = inventory.get_balance(
        db, org_id=org, product_id=p, product_variant_id=None)
    assert bal["on_hand"] == 0
    assert bal["tracked"] is False


def test_reserved_is_not_connected_not_zero(db: Session, org, actor):
    """예약 수량은 0 이 아니라 `NOT_CONNECTED` 다.

    미출고 주문을 알 방법이 없는데 0 이라고 하면 거짓말이다.
    """
    p = make_product(db, org_id=org)
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=1, actor_user_id=actor)
    db.commit()
    assert inventory.list_balances(db, org_id=org)["reserved_status"] == "NOT_CONNECTED"


def test_adjustment_requires_reason(db: Session, org, actor):
    """사유 없는 조정은 거부한다. 나중에 왜 바뀌었는지 알 수 없기 때문이다."""
    p = make_product(db, org_id=org)
    with pytest.raises(ValidationError):
        inventory.adjust(
            db, org_id=org, actor_user_id=actor, product_id=p,
            product_variant_id=None, quantity_delta=-1, reason="   ")


def test_negative_inventory_is_recorded_not_clamped(db: Session, org, actor):
    """음수 재고를 0 으로 만들지 않는다. 몇 개가 비었는지가 정보다."""
    p = make_product(db, org_id=org)
    result = inventory.adjust(
        db, org_id=org, actor_user_id=actor, product_id=p,
        product_variant_id=None, quantity_delta=-5, reason="실사 차이")
    assert result["on_hand_after"] == -5
    assert result["negative"] is True


def test_unit_cost_only_on_acquiring_movements(db: Session, org, actor):
    """판매에는 취득 단가를 기록하지 않는다.

    판매 시점의 원가는 마진 계산이 따로 다룬다. 여기에 넣으면 취득원가와
    판매원가가 같은 컬럼에 섞여 재고 평가액을 못 구한다.
    """
    p = make_product(db, org_id=org)
    with pytest.raises(ValidationError):
        inventory.record_transaction(
            db, org_id=org, product_id=p, product_variant_id=None,
            transaction_type="SALE", quantity_delta=-1, actor_user_id=actor,
            unit_cost=5000)


# ── 2. 발주 · 입고 ─────────────────────────────────────────────


def test_receiving_moves_order_and_inventory_together(
    db: Session, org, actor, supplier,
):
    """입고 한 번에 발주 수량과 재고가 같이 움직인다."""
    p = make_product(db, org_id=org)
    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 20, "unit_price": 5000}])
    po = purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        status="ORDERED")

    po = purchasing.receive(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        receipts=[{"line_id": po["lines"][0]["id"], "quantity": 8}])

    assert po["status"] == "PARTIALLY_RECEIVED"
    assert po["lines"][0]["received_quantity"] == 8
    assert inventory.get_balance(
        db, org_id=org, product_id=p, product_variant_id=None)["on_hand"] == 8


def test_receipt_uses_order_price_not_current_cost(
    db: Session, org, actor, supplier,
):
    """취득원가는 **발주 단가**다. 지금의 소싱 원가가 아니다.

    발주 후에 공급처가 값을 올려도, 우리가 실제로 지불한 것은 발주 단가다.
    """
    p = make_product(db, org_id=org, cost_amount=9_999)
    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 5, "unit_price": 3_000}])
    po = purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        status="ORDERED")
    purchasing.receive(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        receipts=[{"line_id": po["lines"][0]["id"], "quantity": 5}])

    cost = db.execute(text("""
        SELECT unit_cost FROM inventory_transaction
         WHERE org_id = :o AND product_id = :p
           AND transaction_type = 'PURCHASE_RECEIPT'
    """), {"o": str(org), "p": str(p)}).scalar_one()
    assert cost == 3_000  # 9,999 (현재 원가) 가 아니다


def test_same_receipt_key_is_rejected(db: Session, org, actor, supplier):
    """같은 입고 사건이 두 번 오면 두 번째는 막힌다."""
    from sqlalchemy.exc import IntegrityError

    p = make_product(db, org_id=org)
    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 10, "unit_price": 1000}])
    po = purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        status="ORDERED")
    line = po["lines"][0]["id"]

    purchasing.receive(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        receipts=[{"line_id": line, "quantity": 3}], receipt_key="k1")
    with pytest.raises(IntegrityError):
        purchasing.receive(
            db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
            receipts=[{"line_id": line, "quantity": 3}], receipt_key="k1")
    db.rollback()

    assert inventory.get_balance(
        db, org_id=org, product_id=p, product_variant_id=None)["on_hand"] == 3


def test_repeated_receipt_without_key_is_allowed(
    db: Session, org, actor, supplier,
):
    """같은 수량을 두 번 받는 것은 **정상**이다 — 상자가 두 번 왔을 수 있다.

    수량만 보고 자동으로 걸러내면 진짜 입고를 잃는다.
    """
    p = make_product(db, org_id=org)
    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 10, "unit_price": 1000}])
    po = purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        status="ORDERED")
    line = po["lines"][0]["id"]

    for _ in range(2):
        purchasing.receive(
            db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
            receipts=[{"line_id": line, "quantity": 3}])

    assert inventory.get_balance(
        db, org_id=org, product_id=p, product_variant_id=None)["on_hand"] == 6


def test_finished_order_cannot_reopen(db: Session, org, actor, supplier):
    """완료된 발주는 되살아나지 않는다 — 이미 반영된 재고와 어긋난다."""
    p = make_product(db, org_id=org)
    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 2, "unit_price": 1000}])
    oid = uuid.UUID(po["id"])
    purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=oid, status="ORDERED")
    po = purchasing.receive(
        db, org_id=org, actor_user_id=actor, order_id=oid,
        receipts=[{"line_id": po["lines"][0]["id"], "quantity": 2}])
    assert po["status"] == "RECEIVED"

    with pytest.raises(ValidationError):
        purchasing.set_status(
            db, org_id=org, actor_user_id=actor, order_id=oid, status="ORDERED")


def test_order_line_without_price_is_rejected(db: Session, org, actor, supplier):
    """단가를 모르면 발주를 만들지 않는다. 0 으로 채우지 않는다."""
    p = make_product(db, org_id=org)
    with pytest.raises(ValidationError):
        purchasing.create_order(
            db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
            lines=[{"product_id": str(p), "quantity": 1}])


# ── 3. 재주문 ──────────────────────────────────────────────────


def _stock_and_sales(
    db: Session, *, org, actor, product, on_hand: int, sold: int, days: int,
) -> None:
    """재고와 판매 이력을 깔아 둔다. 판매는 관찰 기간 안에 고르게 넣는다."""
    if on_hand:
        inventory.record_transaction(
            db, org_id=org, product_id=product, product_variant_id=None,
            transaction_type="OPENING", quantity_delta=on_hand,
            actor_user_id=actor, unit_cost=1000)
    for i in range(sold):
        make_sales_order(
            db, org_id=org, product_id=product,
            sale_date=TODAY - timedelta(days=i % days), sale_price=10_000,
            quantity=1, marketplace_order_no=f"O-{product.hex[:6]}-{i}")
    db.commit()


def test_reorder_has_no_default_thresholds(db: Session, org):
    """관찰 기간·목표 커버는 반드시 넘겨야 한다. 기본값이 없다."""
    with pytest.raises(TypeError):
        reorder.calculate(db, org_id=org)  # type: ignore[call-arg]


def test_reorder_math(db: Session, org, actor, supplier):
    """계산이 손으로 푼 값과 같은가.

    ```
    판매 30개 / 30일   → 1.0개/일
    현재고 10, 미입고 0
    소진일수 = 10 / 1 = 10일   → 소진 8/21
    리드타임 7일               → 발주기한 8/14
    목표 = (7 + 30) × 1 = 37   → 부족 27
    MOQ 10                     → 권장 30
    ```
    """
    p = make_product(db, org_id=org, cost_amount=2_000)
    _source(db, org=org, product_id=p, supplier_id=supplier,
            lead_time_days=7, moq=10)
    _stock_and_sales(db, org=org, actor=actor, product=p,
                     on_hand=10, sold=30, days=30)

    res = reorder.calculate(
        db, org_id=org, velocity_days=30, coverage_days=30, as_of=TODAY)
    c = next(c for c in res["candidates"] if c["product_id"] == str(p))

    assert c["daily_velocity"] == "1.00"
    assert c["days_of_cover"] == 10
    assert c["stockout_date"] == "2026-08-21"
    assert c["reorder_by_date"] == "2026-08-14"
    assert c["recommended_quantity"] == 30      # 27 을 MOQ 10 배수로 올림
    assert c["estimated_amount"] == 60_000
    assert c["price_basis"] == "PRODUCT_COST"


def test_reorder_subtracts_inbound(db: Session, org, actor, supplier):
    """이미 발주한 수량을 빼지 않으면 같은 것을 또 주문하라고 권한다."""
    p = make_product(db, org_id=org, cost_amount=1_000)
    _source(db, org=org, product_id=p, supplier_id=supplier, lead_time_days=7)
    _stock_and_sales(db, org=org, actor=actor, product=p,
                     on_hand=10, sold=30, days=30)

    before = reorder.calculate(
        db, org_id=org, velocity_days=30, coverage_days=30, as_of=TODAY)
    qty_before = next(
        c for c in before["candidates"] if c["product_id"] == str(p)
    )["recommended_quantity"]

    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 20, "unit_price": 1000}])
    purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        status="ORDERED")

    after = reorder.calculate(
        db, org_id=org, velocity_days=30, coverage_days=30, as_of=TODAY)
    qty_after = next(
        c for c in after["candidates"] if c["product_id"] == str(p)
    )["recommended_quantity"]

    assert qty_after == qty_before - 20


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("no_source", "NO_SOURCE"),
        ("no_lead_time", "NO_LEAD_TIME"),
        ("no_sales", "NO_SALES_HISTORY"),
    ],
)
def test_reorder_blocks_with_specific_reason(
    db: Session, org, actor, supplier, setup: str, expected: str,
):
    """모자란 데이터마다 **다른 이유**를 준다.

    전부 "추천 불가" 로 뭉뚱그리면 사용자가 무엇을 채워야 하는지 모른다.
    """
    p = make_product(db, org_id=org, cost_amount=1_000)
    if setup == "no_lead_time":
        _source(db, org=org, product_id=p, supplier_id=supplier,
                lead_time_days=None)
    elif setup == "no_sales":
        _source(db, org=org, product_id=p, supplier_id=supplier)

    sold = 0 if setup == "no_sales" else 30
    _stock_and_sales(db, org=org, actor=actor, product=p,
                     on_hand=5, sold=sold, days=30)

    res = reorder.calculate(
        db, org_id=org, velocity_days=30, coverage_days=30, as_of=TODAY)
    blocked = next(b for b in res["blocked"] if b["product_id"] == str(p))
    assert blocked["block_reason"] == expected
    # 추천 목록에는 절대 안 들어간다.
    assert all(c["product_id"] != str(p) for c in res["candidates"])


def test_zero_sales_is_not_zero_velocity(db: Session, org, actor, supplier):
    """판매가 없으면 "영원히 안 떨어진다" 가 아니라 "모른다" 다."""
    p = make_product(db, org_id=org, cost_amount=1_000)
    _source(db, org=org, product_id=p, supplier_id=supplier)
    _stock_and_sales(db, org=org, actor=actor, product=p,
                     on_hand=5, sold=0, days=30)

    res = reorder.calculate(
        db, org_id=org, velocity_days=30, coverage_days=30, as_of=TODAY)
    blocked = next(b for b in res["blocked"] if b["product_id"] == str(p))
    assert blocked["block_reason"] == "NO_SALES_HISTORY"
    assert "days_of_cover" not in blocked


def test_sufficient_stock_is_not_a_candidate(db: Session, org, actor, supplier):
    """재고가 충분하면 후보에 넣지 않는다. 목록이 길면 아무도 안 본다."""
    p = make_product(db, org_id=org, cost_amount=1_000)
    _source(db, org=org, product_id=p, supplier_id=supplier, lead_time_days=7)
    _stock_and_sales(db, org=org, actor=actor, product=p,
                     on_hand=500, sold=30, days=30)

    res = reorder.calculate(
        db, org_id=org, velocity_days=30, coverage_days=30, as_of=TODAY)
    assert all(c["product_id"] != str(p) for c in res["candidates"])
    assert all(b["product_id"] != str(p) for b in res["blocked"])


def test_grouping_by_supplier(db: Session, org, actor, supplier):
    """발주는 공급처 단위로 나간다. 상품별로 흩어 두면 손으로 묶어야 한다."""
    for i in range(2):
        p = make_product(db, org_id=org, cost_amount=1_000,
                         name_ko=f"상품{i}")
        _source(db, org=org, product_id=p, supplier_id=supplier,
                lead_time_days=7)
        _stock_and_sales(db, org=org, actor=actor, product=p,
                         on_hand=1, sold=30, days=30)

    res = reorder.calculate(
        db, org_id=org, velocity_days=30, coverage_days=30, as_of=TODAY)
    groups = reorder.group_by_supplier(res["candidates"])
    assert len(groups) == 1
    assert len(groups[0]["lines"]) == 2
    assert groups[0]["total_amount"] == sum(
        c["estimated_amount"] for c in res["candidates"])
