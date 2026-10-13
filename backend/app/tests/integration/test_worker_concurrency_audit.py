"""자동화 중복 생성 red team.

무엇을 의심하나
------------
자동화의 중복 방지가 전부 **먼저 조회하고 나중에 쓰는**(check-then-act) 구조다.

```
_has_open_draft()             이미 초안이 있나  → 없으면 만든다
_daily_report_already_done()  오늘 만들었나     → 안 만들었으면 만든다
```

한 프로세스에서는 맞다. 두 프로세스가 동시에 조회하면 둘 다 "없다" 를 보고
둘 다 만들 수 있다. **발주 초안이 두 개 생기면 사람이 둘 다 확정할 수 있고,
그건 중복 발주다.**

그래서 두 가지를 확인한다.

1. 순차 실행(2회·10회)에서 하나만 만들어지는가 — 애플리케이션 방어
2. 그 방어가 뚫렸을 때 **DB 가 막는가** — 최종 방어

2가 이 파일의 핵심이다. check-then-act 만 믿으면 언젠가 새기 때문이다.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services import automation as auto
from app.services import automation_runners as runners
from app.services import inventory, purchasing
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
        VALUES (:i, :o, :c, '동시성공급처', 'KR')
    """), {"i": str(sid), "o": str(org), "c": f"S-{sid.hex[:8]}"})
    db.commit()
    return sid


def _urgent_product(db: Session, org, actor, supplier) -> uuid.UUID:
    p = make_product(db, org_id=org, cost_amount=1_000)
    db.execute(text("""
        INSERT INTO product_source (id, org_id, product_id, supplier_site_id,
                                    lead_time_days, moq, is_primary, status,
                                    currency)
        VALUES (:i, :o, :p, :s, 7, 10, TRUE, 'ACTIVE', 'KRW')
    """), {"i": str(uuid.uuid4()), "o": str(org), "p": str(p),
           "s": str(supplier)})
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=1, actor_user_id=actor,
        unit_cost=1_000)
    today = date.today()
    for i in range(30):
        make_sales_order(
            db, org_id=org, product_id=p,
            sale_date=today - timedelta(days=i % 30), sale_price=5_000,
            quantity=1, marketplace_order_no=f"WC-{p.hex[:6]}-{i}")
    db.commit()
    return p


def _po_count(db: Session, org) -> int:
    return db.execute(text("""
        SELECT COUNT(*) FROM purchase_order
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalar_one()


def _policy(db, org, actor):
    return auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="REORDER_MONITOR", enabled=True, mode="PROPOSE",
        guardrails={"velocity_days": 30, "coverage_days": 30})


# ── 1. 반복 실행 ───────────────────────────────────────────────


@pytest.mark.parametrize("times", [2, 10])
def test_repeated_reorder_scan_makes_one_draft(
    db: Session, org, actor, supplier, times: int,
):
    """스케줄이 자주 돌아도 초안이 쌓이면 안 된다."""
    _urgent_product(db, org, actor, supplier)
    policy = _policy(db, org, actor)
    for _ in range(times):
        runners.run_policy(db, org_id=org, policy=policy,
                           actor_user_id=actor, trigger="SCHEDULED")
    assert _po_count(db, org) == 1


@pytest.mark.parametrize("times", [2, 5])
def test_repeated_daily_report_makes_one(db: Session, org, actor, times: int):
    """worker 가 여러 번 돌아도 리포트·알림은 하루 하나."""
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="DAILY_REPORT",
        enabled=True, mode="REPORT_ONLY", guardrails={})
    for _ in range(times):
        runners.run_policy(db, org_id=org, policy=policy,
                           actor_user_id=actor, trigger="SCHEDULED")

    built = db.execute(text("""
        SELECT COUNT(*) FROM automation_action a
          JOIN automation_run r ON r.id = a.run_id
         WHERE r.org_id = :o AND a.action_type = 'DAILY_REPORT_BUILT'
           AND a.outcome = 'REPORTED'
    """), {"o": str(org)}).scalar_one()
    assert built == 1

    notes = db.execute(text("""
        SELECT COUNT(*) FROM notification
         WHERE org_id = :o AND kind = 'DAILY_REPORT_READY'
           AND deleted_at IS NULL
    """), {"o": str(org)}).scalar_one()
    assert notes == 1


# ── 2. DB 최종 방어 ────────────────────────────────────────────


def test_duplicate_business_key_is_blocked_by_db(
    db: Session, org, actor, supplier,
):
    """애플리케이션 방어가 뚫려도 **DB 가 막는다.**

    `_next_business_key` 는 그날 건수 + 1 이다. 두 트랜잭션이 동시에 세면 같은
    번호가 나오고, `uq_purchase_order_key` 가 두 번째 INSERT 를 거부한다.

    즉 동시 실행의 최악은 **초안 2개가 아니라 한쪽 실행의 실패**다. 실패한
    실행은 다음 스케줄에 다시 돈다 — 중복 발주보다 훨씬 낫다.
    """
    p = make_product(db, org_id=org)
    first = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=supplier,
        lines=[{"product_id": str(p), "quantity": 1, "unit_price": 1000}])

    # 같은 번호를 강제로 다시 넣는다 — 동시 실행이 만들어내는 상황이다.
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO purchase_order (org_id, business_key, supplier_site_id,
                                        status, currency, created_by)
            VALUES (:o, :k, :s, 'DRAFT', 'KRW', :a)
        """), {"o": str(org), "k": first["business_key"], "s": str(supplier),
               "a": str(actor)})
        db.flush()
    db.rollback()


def test_duplicate_daily_report_notification_is_blocked_by_db(
    db: Session, org, actor,
):
    """알림도 `dedupe_key` 가 DB 에서 막는다.

    같은 날 리포트 알림이 두 번 가면 사용자는 벨을 안 보게 된다.
    """
    from app.services import notification_events as ne

    today = date.today()
    ne.daily_report_ready(db, org_id=org, report_date=today,
                          attention_count=1)
    db.commit()
    second = ne.daily_report_ready(db, org_id=org, report_date=today,
                                   attention_count=1)
    db.commit()
    # 두 번째는 만들어지지 않고 deduped 로 센다.
    assert second.created == 0
    assert second.deduped >= 1


def test_inventory_import_duplicate_is_blocked_by_db(db: Session, org, actor):
    """재고 가져오기 재적용도 DB 유니크가 최종 방어다."""
    p = make_product(db, org_id=org)
    from app.services.inventory_import import _source_id

    src = _source_id("a" * 64, p, None, date.today())
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=5, actor_user_id=actor,
        unit_cost=100, source_table="inventory_import", source_id=src)
    db.commit()

    with pytest.raises(IntegrityError):
        inventory.record_transaction(
            db, org_id=org, product_id=p, product_variant_id=None,
            transaction_type="OPENING", quantity_delta=5,
            actor_user_id=actor, unit_cost=100,
            source_table="inventory_import", source_id=src)
        db.flush()
    db.rollback()


# ── 3. REPORT_ONLY 는 쓰지 않는다 ──────────────────────────────


def test_report_only_writes_nothing(db: Session, org, actor, supplier):
    """관찰 모드가 실제로 아무것도 안 쓰는지 — 문서가 아니라 행 수로 본다."""
    _urgent_product(db, org, actor, supplier)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="REORDER_MONITOR", enabled=True, mode="REPORT_ONLY",
        guardrails={"velocity_days": 30, "coverage_days": 30})

    before_po = _po_count(db, org)
    before_appr = db.execute(text("""
        SELECT COUNT(*) FROM approval_request WHERE org_id = :o
    """), {"o": str(org)}).scalar_one()

    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor,
                       trigger="MANUAL")

    assert _po_count(db, org) == before_po
    assert db.execute(text("""
        SELECT COUNT(*) FROM approval_request WHERE org_id = :o
    """), {"o": str(org)}).scalar_one() == before_appr
