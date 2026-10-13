"""REORDER_MONITOR 자동화 (M048).

이 파일이 고정하는 것
------------------
1. 파라미터가 없으면 **돌지 않는다** — 기본값을 만들어 쓰지 않는다
2. `AUTO_EXECUTE` 는 아예 선택지에 없다 — 발주는 자동 실행하지 않는다
3. `REPORT_ONLY` 는 초안도 만들지 않는다
4. `PROPOSE` 는 **DRAFT** 까지만 — 주문이 나가지 않는다
5. 초안은 공급처 단위로 묶인다
6. 처리 안 된 자동 초안이 있으면 또 만들지 않는다
7. 아직 여유 있는 상품으로 초안을 만들지 않는다

1 과 4 가 핵심이다
----------------
추정치(판매속도)로 돈을 쓰는 경로가 생기면 안 된다.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services import automation as auto
from app.services import automation_runners as runners
from app.services import inventory
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_sales_order,
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
        VALUES (:id, :org, :code, '자동화공급처', 'KR')
    """), {"id": str(sid), "org": str(org), "code": f"S-{sid.hex[:8]}"})
    return sid


def _urgent_product(
    db: Session, *, org, actor, supplier, on_hand: int = 1, sold: int = 60,
) -> uuid.UUID:
    """곧 품절될 상품 하나. 재고는 거의 없고 잘 팔린다."""
    p = make_product(db, org_id=org, cost_amount=1_000)
    db.execute(text("""
        INSERT INTO product_source (
          id, org_id, product_id, supplier_site_id, lead_time_days, moq,
          is_primary, status, currency
        ) VALUES (:id, :org, :p, :s, 7, 10, TRUE, 'ACTIVE', 'KRW')
    """), {"id": str(uuid.uuid4()), "org": str(org), "p": str(p),
           "s": str(supplier)})
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=on_hand,
        actor_user_id=actor, unit_cost=1_000)
    today = date.today()
    for i in range(sold):
        make_sales_order(
            db, org_id=org, product_id=p, sale_date=today - timedelta(days=i % 30),
            sale_price=5_000, quantity=1,
            marketplace_order_no=f"RO-{p.hex[:6]}-{i}")
    db.commit()
    return p


def _policy(db: Session, *, org, actor, mode: str, guardrails: dict) -> dict:
    return auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="REORDER_MONITOR", enabled=True, mode=mode,
        guardrails=guardrails)


def _run(db: Session, *, org, actor, policy: dict):
    return runners.run_policy(
        db, org_id=org, policy=policy, actor_user_id=actor, trigger="MANUAL")


def _actions(db: Session, run_id: str) -> list[dict]:
    return [dict(r) for r in db.execute(text("""
        SELECT action_type, outcome, blocked_by, reason
          FROM automation_action WHERE run_id = :r ORDER BY created_at
    """), {"r": run_id}).mappings().all()]


# ── 1. 파라미터 ────────────────────────────────────────────────


def test_auto_execute_is_not_offered(db: Session, org, actor):
    """발주 자동 실행 모드는 존재하지 않는다."""
    assert "AUTO_EXECUTE" not in auto.AUTOMATION_TYPES["REORDER_MONITOR"]["modes"]
    with pytest.raises(ValidationError):
        _policy(db, org=org, actor=actor, mode="AUTO_EXECUTE",
                guardrails={"velocity_days": 30, "coverage_days": 30})


def test_missing_parameters_block_the_run(db: Session, org, actor, supplier):
    """관찰 기간을 안 정했으면 돌지 않는다. 30일을 지어내지 않는다."""
    _urgent_product(db, org=org, actor=actor, supplier=supplier)
    policy = _policy(db, org=org, actor=actor, mode="PROPOSE", guardrails={})

    result = _run(db, org=org, actor=actor, policy=policy)

    acts = _actions(db, result.run_id)
    assert len(acts) == 1
    assert acts[0]["outcome"] == "BLOCKED"
    assert acts[0]["blocked_by"] == "MISSING_GUARDRAIL"
    # 초안이 하나도 안 생겼다.
    assert db.execute(text(
        "SELECT COUNT(*) FROM purchase_order WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0


def test_unknown_guardrail_is_rejected(db: Session, org, actor):
    """오타 난 파라미터가 조용히 무시되면 사용자는 설정했다고 믿는다."""
    with pytest.raises(ValidationError):
        _policy(db, org=org, actor=actor, mode="PROPOSE",
                guardrails={"velocity_day": 30})


# ── 2. 모드별 행동 ─────────────────────────────────────────────


def test_report_only_creates_no_draft(db: Session, org, actor, supplier):
    """관찰 모드는 발주 초안도 만들지 않는다."""
    _urgent_product(db, org=org, actor=actor, supplier=supplier)
    policy = _policy(db, org=org, actor=actor, mode="REPORT_ONLY",
                     guardrails={"velocity_days": 30, "coverage_days": 30})

    result = _run(db, org=org, actor=actor, policy=policy)

    assert any(a["action_type"] == "REORDER_NEEDED"
               for a in _actions(db, result.run_id))
    assert db.execute(text(
        "SELECT COUNT(*) FROM purchase_order WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0


def test_propose_creates_draft_only(db: Session, org, actor, supplier):
    """제안 모드는 **DRAFT** 까지만. 주문은 나가지 않는다."""
    _urgent_product(db, org=org, actor=actor, supplier=supplier)
    policy = _policy(db, org=org, actor=actor, mode="PROPOSE",
                     guardrails={"velocity_days": 30, "coverage_days": 30})

    result = _run(db, org=org, actor=actor, policy=policy)

    rows = db.execute(text("""
        SELECT status, automation_run_id FROM purchase_order WHERE org_id = :o
    """), {"o": str(org)}).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "DRAFT"          # ORDERED 가 아니다
    assert rows[0]["automation_run_id"] is not None  # 출처가 남는다

    acts = _actions(db, result.run_id)
    assert any(a["action_type"] == "PURCHASE_ORDER_PROPOSED"
               and a["outcome"] == "PROPOSED" for a in acts)


def test_draft_groups_by_supplier(db: Session, org, actor, supplier):
    """한 공급처의 여러 상품은 발주 하나로 묶인다."""
    for _ in range(3):
        _urgent_product(db, org=org, actor=actor, supplier=supplier)
    policy = _policy(db, org=org, actor=actor, mode="PROPOSE",
                     guardrails={"velocity_days": 30, "coverage_days": 30})

    _run(db, org=org, actor=actor, policy=policy)

    orders = db.execute(text(
        "SELECT id FROM purchase_order WHERE org_id = :o"),
        {"o": str(org)}).scalars().all()
    assert len(orders) == 1
    lines = db.execute(text(
        "SELECT COUNT(*) FROM purchase_order_line WHERE purchase_order_id = :p"),
        {"p": str(orders[0])}).scalar_one()
    assert lines == 3


def test_second_run_does_not_duplicate(db: Session, org, actor, supplier):
    """처리 안 된 자동 초안이 있으면 또 만들지 않는다 — 중복 발주가 된다."""
    _urgent_product(db, org=org, actor=actor, supplier=supplier)
    policy = _policy(db, org=org, actor=actor, mode="PROPOSE",
                     guardrails={"velocity_days": 30, "coverage_days": 30})

    _run(db, org=org, actor=actor, policy=policy)
    second = _run(db, org=org, actor=actor, policy=policy)

    assert db.execute(text(
        "SELECT COUNT(*) FROM purchase_order WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 1
    acts = _actions(db, second.run_id)
    assert any(a["blocked_by"] == "PENDING_DRAFT_EXISTS" for a in acts)


def test_stocked_product_creates_nothing(db: Session, org, actor, supplier):
    """재고가 넉넉하면 초안을 만들지 않는다."""
    _urgent_product(db, org=org, actor=actor, supplier=supplier,
                    on_hand=5_000, sold=30)
    policy = _policy(db, org=org, actor=actor, mode="PROPOSE",
                     guardrails={"velocity_days": 30, "coverage_days": 30})

    _run(db, org=org, actor=actor, policy=policy)

    assert db.execute(text(
        "SELECT COUNT(*) FROM purchase_order WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0


def test_missing_data_is_summarised_not_hidden(db: Session, org, actor):
    """추천할 수 없는 상품이 있다는 사실 자체를 보고한다.

    숨기면 사용자는 "우리 상품은 다 괜찮구나" 로 잘못 읽는다.
    """
    p = make_product(db, org_id=org, cost_amount=1_000)  # 소싱처 없음
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=1, actor_user_id=actor,
        unit_cost=1_000)
    make_sales_order(db, org_id=org, product_id=p, sale_date=date.today(),
                     sale_price=5_000, quantity=1, marketplace_order_no="X-1")
    db.commit()

    policy = _policy(db, org=org, actor=actor, mode="PROPOSE",
                     guardrails={"velocity_days": 30, "coverage_days": 30})
    result = _run(db, org=org, actor=actor, policy=policy)

    acts = _actions(db, result.run_id)
    missing = next(a for a in acts if a["action_type"] == "REORDER_DATA_MISSING")
    assert "소싱처" in missing["reason"]
