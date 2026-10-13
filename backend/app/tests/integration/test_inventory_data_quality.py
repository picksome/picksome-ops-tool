"""재고·발주 데이터 품질 스캐너.

이 파일이 고정하는 것
------------------
1. 임계값을 만들지 않는다 — 0 미만, 기한 경과처럼 **데이터로 참인 것**만
2. 예상 입고일이 없으면 지연이 아니다 (모름이지 정상이 아니다)
3. 같은 문제로 이슈가 두 번 생기지 않는다
4. 조건이 사라지면 자동으로 닫힌다
5. 사람이 닫은 것을 다시 열지 않는다
6. 재주문 기한 경과는 이슈로 만들지 않는다 — 매일 바뀐다

1·5·6 이 핵심이다
---------------
근거 없는 임계값은 나중에 아무도 설명하지 못한다. 사람이 닫은 것을 기계가 다시
열면 이슈 센터를 두 번째부터 아무도 안 본다. 매일 바뀌는 것을 이슈로 만들면
이슈 목록이 소음이 된다.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import inventory, purchasing
from app.services import operation_issue as oi
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)


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
        VALUES (:id, :o, :c, '품질공급처', 'KR')
    """), {"id": str(sid), "o": str(org), "c": f"S-{sid.hex[:8]}"})
    db.commit()
    return sid


def _types(db, org) -> dict[str, dict]:
    rows = db.execute(text("""
        SELECT issue_type, severity, status, affected_count
          FROM operation_issue
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).mappings().all()
    return {r["issue_type"]: dict(r) for r in rows}


def _order(db, org, actor, supplier, *, expected: date | None):
    p = make_product(db, org_id=org)
    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 3, "unit_price": 1000}],
        expected_at=expected)
    purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        status="ORDERED")
    return po


# ── 음수 재고 ──────────────────────────────────────────────────


def test_negative_inventory_becomes_issue(db: Session, org, actor):
    """0 미만은 임계값이 필요 없다. 그 자체가 문제다."""
    p = make_product(db, org_id=org)
    inventory.adjust(db, org_id=org, actor_user_id=actor, product_id=p,
                     product_variant_id=None, quantity_delta=-3,
                     reason="실사 차이")

    oi.scan(db, org_id=org)
    issue = _types(db, org)["NEGATIVE_INVENTORY"]
    assert issue["severity"] == "WARNING"
    assert issue["affected_count"] == 1


def test_positive_inventory_creates_no_issue(db: Session, org, actor):
    """재고가 적다고 이슈를 만들지 않는다. '적다' 의 기준이 없다."""
    p = make_product(db, org_id=org)
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=1, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    oi.scan(db, org_id=org)
    assert "NEGATIVE_INVENTORY" not in _types(db, org)


# ── 입고 지연 ──────────────────────────────────────────────────


def test_overdue_purchase_becomes_issue(db: Session, org, actor, supplier):
    _order(db, org, actor, supplier, expected=date.today() - timedelta(days=1))
    oi.scan(db, org_id=org)
    assert "PURCHASE_OVERDUE" in _types(db, org)


def test_purchase_without_expected_date_is_not_overdue(
    db: Session, org, actor, supplier,
):
    """예상일이 없으면 늦었는지 **알 수 없다.** 정상이 아니라 모름이다."""
    _order(db, org, actor, supplier, expected=None)
    oi.scan(db, org_id=org)
    assert "PURCHASE_OVERDUE" not in _types(db, org)


def test_future_expected_date_is_not_overdue(db: Session, org, actor, supplier):
    """유예 기간을 두지 않는다. 지났으면 지난 거고 아니면 아니다."""
    _order(db, org, actor, supplier, expected=date.today() + timedelta(days=7))
    oi.scan(db, org_id=org)
    assert "PURCHASE_OVERDUE" not in _types(db, org)


# ── 재주문 데이터 ──────────────────────────────────────────────


def test_reorder_data_incomplete_is_info_not_blocking(db: Session, org, actor):
    """기능이 안 되는 것이지 마감을 막는 것은 아니다."""
    p = make_product(db, org_id=org)
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=5, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    oi.scan(db, org_id=org)
    issue = _types(db, org)["REORDER_DATA_INCOMPLETE"]
    assert issue["severity"] == "INFO"


def test_untracked_products_are_not_counted(db: Session, org, supplier):
    """재고를 안 잡은 상품까지 세면 상품 마스터 전체가 이슈가 된다."""
    make_product(db, org_id=org)  # 원장 없음
    db.commit()
    oi.scan(db, org_id=org)
    assert "REORDER_DATA_INCOMPLETE" not in _types(db, org)


def test_complete_source_creates_no_issue(db: Session, org, actor, supplier):
    p = make_product(db, org_id=org)
    db.execute(text("""
        INSERT INTO product_source (id, org_id, product_id, supplier_site_id,
                                    lead_time_days, is_primary, status, currency)
        VALUES (gen_random_uuid(), :o, :p, :s, 7, TRUE, 'ACTIVE', 'KRW')
    """), {"o": str(org), "p": str(p), "s": str(supplier)})
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=5, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    oi.scan(db, org_id=org)
    assert "REORDER_DATA_INCOMPLETE" not in _types(db, org)


def test_reorder_overdue_is_not_an_issue(db: Session, org, actor, supplier):
    """재주문 기한 경과는 이슈로 만들지 않는다.

    매일 바뀌는 값이라 이슈로 만들면 목록이 소음이 된다. 예외 센터에서 본다.
    """
    p = make_product(db, org_id=org, cost_amount=1000)
    db.execute(text("""
        INSERT INTO product_source (id, org_id, product_id, supplier_site_id,
                                    lead_time_days, is_primary, status, currency)
        VALUES (gen_random_uuid(), :o, :p, :s, 7, TRUE, 'ACTIVE', 'KRW')
    """), {"o": str(org), "p": str(p), "s": str(supplier)})
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=1, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    oi.scan(db, org_id=org)
    assert "REORDER_REQUIRED" not in _types(db, org)


# ── 중복·해소·사람이 닫은 것 ───────────────────────────────────


def test_second_scan_does_not_duplicate(db: Session, org, actor):
    p = make_product(db, org_id=org)
    inventory.adjust(db, org_id=org, actor_user_id=actor, product_id=p,
                     product_variant_id=None, quantity_delta=-1, reason="실사")

    oi.scan(db, org_id=org)
    oi.scan(db, org_id=org)

    n = db.execute(text("""
        SELECT COUNT(*) FROM operation_issue
         WHERE org_id = :o AND issue_type = 'NEGATIVE_INVENTORY'
           AND deleted_at IS NULL
    """), {"o": str(org)}).scalar_one()
    assert n == 1


def test_resolved_when_condition_disappears(db: Session, org, actor):
    """음수를 고치면 이슈가 자동으로 닫힌다."""
    p = make_product(db, org_id=org)
    inventory.adjust(db, org_id=org, actor_user_id=actor, product_id=p,
                     product_variant_id=None, quantity_delta=-4, reason="실사")
    oi.scan(db, org_id=org)
    assert _types(db, org)["NEGATIVE_INVENTORY"]["status"] == "OPEN"

    inventory.adjust(db, org_id=org, actor_user_id=actor, product_id=p,
                     product_variant_id=None, quantity_delta=4, reason="입고 누락분")
    oi.scan(db, org_id=org)
    assert _types(db, org)["NEGATIVE_INVENTORY"]["status"] == "RESOLVED"


def test_human_closed_issue_is_not_reopened(db: Session, org, actor):
    """사람이 닫은 것을 기계가 다시 열면 이슈 센터를 아무도 안 본다."""
    p = make_product(db, org_id=org)
    inventory.adjust(db, org_id=org, actor_user_id=actor, product_id=p,
                     product_variant_id=None, quantity_delta=-2, reason="실사")
    oi.scan(db, org_id=org)

    # 사람이 화면에서 하는 것과 같은 경로로 닫는다. raw SQL 로 상태만 바꾸면
    # `ck_oi_ignore_reason`·`ck_oi_resolved_at` 같은 제약을 우회하게 되고,
    # 실제 사용자 흐름을 검증하지 못한다.
    issue_id = db.execute(text("""
        SELECT id FROM operation_issue
         WHERE org_id = :o AND issue_type = 'NEGATIVE_INVENTORY'
    """), {"o": str(org)}).scalar_one()
    oi.update_issue(
        db, org_id=org, issue_id=uuid.UUID(str(issue_id)), actor_user_id=actor,
        status="IGNORED", resolution_note="기초재고 정리 예정")

    # 조건은 여전히 참이다.
    oi.scan(db, org_id=org)
    assert _types(db, org)["NEGATIVE_INVENTORY"]["status"] == "IGNORED"
