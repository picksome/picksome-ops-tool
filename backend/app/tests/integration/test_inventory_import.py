"""재고 대량 가져오기 (M050).

이 파일이 고정하는 것
------------------
1. Excel 수량이 **잔량을 덮어쓰지 않는다** — 차이만 원장에 들어간다
2. 같은 파일을 두 번 적용해도 재고가 두 번 늘지 않는다
3. 같은 값이면 아무것도 만들지 않는다
4. 상품을 **추측해서** 맞추지 않는다
5. 옵션을 특정할 수 없으면 반영하지 않는다
6. `ANALYZE` 는 DB 를 건드리지 않는다
7. 0 은 유효한 수량이다
8. 행마다 audit 를 쌓지 않는다

1·2·4 가 핵심이다
---------------
재고를 덮어쓰면 왜 그 숫자인지 영영 모른다. 두 번 늘면 장부가 실물과
어긋난다. 상품을 잘못 맞추면 **엉뚱한 상품의 재고가 바뀌고** 그대로 발주까지
나간다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import inventory
from app.services import inventory_import as imp
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)

AS_OF = date(2026, 8, 11)
SHA = "a" * 64
HEADER = ("SKU", "수량", "메모")


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org)


def _product(db: Session, org, sku: str, name: str = "가져오기상품") -> uuid.UUID:
    pid = make_product(db, org_id=org, name_ko=name)
    db.execute(text("UPDATE product SET sku = :s WHERE id = :i"),
               {"s": sku, "i": str(pid)})
    db.commit()
    return pid


def _run(db, org, rows, mode="DRY_RUN", sha=SHA, as_of=AS_OF):
    return imp.plan(db, org_id=org, rows=rows, mode=mode, as_of_date=as_of,
                    source_sha256=sha)


def _apply(db, org, actor, plan, sha=SHA):
    return imp.apply_plan(db, org_id=org, actor_user_id=actor, plan_result=plan,
                          source_name="재고.csv", source_sha256=sha)


def _on_hand(db, org, pid) -> int:
    return inventory.get_balance(
        db, org_id=org, product_id=pid, product_variant_id=None)["on_hand"]


# ── 1. OPENING ─────────────────────────────────────────────────


def test_untracked_product_becomes_opening(db: Session, org, actor):
    """원장이 없는 상품은 최초 재고(OPENING)."""
    pid = _product(db, org, "SKU-1")
    plan = _run(db, org, [HEADER, ("SKU-1", 17, None)])

    assert plan.counts()["OPENING"] == 1
    _apply(db, org, actor, plan)

    assert _on_hand(db, org, pid) == 17
    kind = db.execute(text("""
        SELECT transaction_type FROM inventory_transaction
         WHERE org_id = :o AND product_id = :p
    """), {"o": str(org), "p": str(pid)}).scalar_one()
    assert kind == "OPENING"


# ── 2. 잔량을 덮어쓰지 않는다 ──────────────────────────────────


def test_tracked_product_records_difference_not_overwrite(
    db: Session, org, actor,
):
    """원장 14, 실사 17 → `ADJUSTMENT +3`. 17 을 쓰는 게 아니다."""
    pid = _product(db, org, "SKU-2")
    inventory.record_transaction(
        db, org_id=org, product_id=pid, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=14, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    plan = _run(db, org, [HEADER, ("SKU-2", 17, None)])
    row = plan.rows[0]
    assert row.outcome == "ADJUSTMENT"
    assert (row.current, row.counted, row.delta) == (14, 17, 3)

    _apply(db, org, actor, plan)
    assert _on_hand(db, org, pid) == 17

    # 원장에 **차이**가 남아 있다. 17 이라는 줄이 아니다.
    deltas = db.execute(text("""
        SELECT quantity_delta FROM inventory_transaction
         WHERE org_id = :o AND product_id = :p ORDER BY occurred_at
    """), {"o": str(org), "p": str(pid)}).scalars().all()
    assert deltas == [14, 3]


def test_same_quantity_creates_nothing(db: Session, org, actor):
    """같으면 원장에 아무것도 안 만든다. 의미 없는 줄이 쌓이면 안 된다."""
    pid = _product(db, org, "SKU-3")
    inventory.record_transaction(
        db, org_id=org, product_id=pid, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=9, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    plan = _run(db, org, [HEADER, ("SKU-3", 9, None)])
    assert plan.rows[0].outcome == "UNCHANGED"
    _apply(db, org, actor, plan)

    n = db.execute(text("""
        SELECT COUNT(*) FROM inventory_transaction
         WHERE org_id = :o AND product_id = :p
    """), {"o": str(org), "p": str(pid)}).scalar_one()
    assert n == 1  # 처음 OPENING 하나뿐


# ── 3. 재적용 ──────────────────────────────────────────────────


def test_reimport_same_file_creates_no_duplicate(db: Session, org, actor):
    """같은 파일을 두 번 적용해도 재고가 두 번 늘지 않는다."""
    pid = _product(db, org, "SKU-4")
    rows = [HEADER, ("SKU-4", 20, None)]

    _apply(db, org, actor, _run(db, org, rows))
    assert _on_hand(db, org, pid) == 20

    second = _run(db, org, rows)
    assert second.rows[0].outcome == "DUPLICATE"
    _apply(db, org, actor, second)

    assert _on_hand(db, org, pid) == 20
    n = db.execute(text("""
        SELECT COUNT(*) FROM inventory_transaction
         WHERE org_id = :o AND product_id = :p
    """), {"o": str(org), "p": str(pid)}).scalar_one()
    assert n == 1


def test_different_file_is_a_new_import(db: Session, org, actor):
    """내용이 다르면 다른 파일이다. 파일명이 같아도 상관없다."""
    pid = _product(db, org, "SKU-5")
    _apply(db, org, actor, _run(db, org, [HEADER, ("SKU-5", 10, None)], sha="b" * 64),
           sha="b" * 64)
    assert _on_hand(db, org, pid) == 10

    # 다른 해시 = 다른 파일. 실사 결과가 12 로 바뀌었다.
    plan = _run(db, org, [HEADER, ("SKU-5", 12, None)], sha="c" * 64)
    assert plan.rows[0].outcome == "ADJUSTMENT"
    _apply(db, org, actor, plan, sha="c" * 64)
    assert _on_hand(db, org, pid) == 12


def test_same_file_different_date_is_a_new_count(db: Session, org, actor):
    """같은 양식으로 **다른 날** 실사한 것은 새 원장 줄이어야 한다."""
    pid = _product(db, org, "SKU-6")
    rows = [HEADER, ("SKU-6", 5, None)]
    _apply(db, org, actor, _run(db, org, rows, as_of=date(2026, 8, 1)))
    assert _on_hand(db, org, pid) == 5

    plan = _run(db, org, [HEADER, ("SKU-6", 8, None)], as_of=date(2026, 8, 11))
    assert plan.rows[0].outcome == "ADJUSTMENT"
    _apply(db, org, actor, plan)
    assert _on_hand(db, org, pid) == 8


# ── 4. 매칭 ────────────────────────────────────────────────────


def test_unknown_sku_is_unmatched_not_created(db: Session, org, actor):
    """못 찾은 상품을 새로 만들지 않는다."""
    plan = _run(db, org, [HEADER, ("없는SKU", 5, None)])
    assert plan.rows[0].outcome == "UNMATCHED"
    _apply(db, org, actor, plan)

    assert db.execute(text(
        "SELECT COUNT(*) FROM inventory_transaction WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0


def test_similar_name_is_not_matched(db: Session, org, actor):
    """이름이 비슷하다고 맞추지 않는다.

    잘못 맞추면 엉뚱한 상품의 재고가 바뀌고 그대로 발주까지 나간다.
    """
    _product(db, org, "SKU-7", name="파란색 티셔츠 L")
    plan = _run(db, org, [("상품명", "수량"), ("파란색 티셔츠", 5)])
    # 상품명 열은 열쇠가 아니다 — 파일 자체가 거부된다.
    assert plan.file_problems
    assert "상품을 찾을 열이 없습니다" in plan.file_problems[0]


def test_business_key_also_works(db: Session, org, actor):
    """SKU 가 없는 상품은 상품번호로 찾는다."""
    pid = make_product(db, org_id=org, business_key="PROD-TEST-1")
    db.commit()
    plan = _run(db, org, [("상품번호", "수량"), ("PROD-TEST-1", 4)])
    assert plan.rows[0].outcome == "OPENING"
    _apply(db, org, actor, plan)
    assert _on_hand(db, org, pid) == 4


def test_ambiguous_variant_is_not_applied(db: Session, org, actor):
    """옵션이 있는 상품에 옵션코드가 없으면 반영하지 않는다."""
    pid = _product(db, org, "SKU-8")
    db.execute(text("""
        INSERT INTO product_variant
               (id, org_id, product_id, variant_key, option_values_json)
        VALUES (gen_random_uuid(), :o, :p, 'RED', '{}'::jsonb),
               (gen_random_uuid(), :o, :p, 'BLUE', '{}'::jsonb)
    """), {"o": str(org), "p": str(pid)})
    db.commit()

    plan = _run(db, org, [HEADER, ("SKU-8", 5, None)])
    assert plan.rows[0].outcome == "AMBIGUOUS"
    _apply(db, org, actor, plan)
    assert db.execute(text(
        "SELECT COUNT(*) FROM inventory_transaction WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0


def test_variant_key_resolves(db: Session, org, actor):
    pid = _product(db, org, "SKU-9")
    vid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_variant
               (id, org_id, product_id, variant_key, option_values_json)
        VALUES (:v, :o, :p, 'RED', '{}'::jsonb)
    """), {"v": str(vid), "o": str(org), "p": str(pid)})
    db.commit()

    plan = _run(db, org, [("SKU", "옵션코드", "수량"), ("SKU-9", "RED", 6)])
    assert plan.rows[0].outcome == "OPENING"
    _apply(db, org, actor, plan)
    assert inventory.get_balance(
        db, org_id=org, product_id=pid, product_variant_id=vid)["on_hand"] == 6


# ── 5. 검증 ────────────────────────────────────────────────────


def test_analyze_does_not_touch_db(db: Session, org, actor):
    """`ANALYZE` 는 상품을 찾지도 않는다. 파일 형식만 본다."""
    _product(db, org, "SKU-10")
    plan = _run(db, org, [HEADER, ("SKU-10", 3, None)], mode="ANALYZE")
    assert plan.mode == "ANALYZE"
    assert plan.rows[0].product_id is None
    assert db.execute(text(
        "SELECT COUNT(*) FROM inventory_transaction WHERE org_id = :o"),
        {"o": str(org)}).scalar_one() == 0


@pytest.mark.parametrize("value", ["", "abc", "12.5", None])
def test_invalid_quantity_is_reported(db: Session, org, value):
    _product(db, org, "SKU-11")
    plan = _run(db, org, [HEADER, ("SKU-11", value, None)])
    assert plan.rows[0].outcome == "INVALID"
    assert plan.rows[0].message


def test_zero_is_a_valid_quantity(db: Session, org, actor):
    """0 은 "실제로 0개" 다. 빈 값과 다르다."""
    pid = _product(db, org, "SKU-12")
    inventory.record_transaction(
        db, org_id=org, product_id=pid, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=5, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    plan = _run(db, org, [HEADER, ("SKU-12", 0, None)])
    assert plan.rows[0].outcome == "ADJUSTMENT"
    assert plan.rows[0].delta == -5
    _apply(db, org, actor, plan)
    assert _on_hand(db, org, pid) == 0


def test_float_from_excel_is_accepted(db: Session, org):
    """Excel 은 정수를 12.0 으로 준다. 이건 받는다."""
    _product(db, org, "SKU-13")
    plan = _run(db, org, [HEADER, ("SKU-13", 12.0, None)])
    assert plan.rows[0].outcome == "OPENING"
    assert plan.rows[0].counted == 12


def test_missing_quantity_column_is_a_file_problem(db: Session, org):
    plan = _run(db, org, [("SKU", "메모"), ("SKU-1", "없음")])
    assert plan.file_problems
    assert "수량" in plan.file_problems[0]


# ── 6. 감사 ────────────────────────────────────────────────────


def test_audit_is_per_run_not_per_row(db: Session, org, actor):
    """1,000행이면 audit 가 1,000줄 늘어난다. 실행 단위로 남긴다.

    행 단위 추적성은 원장이 이미 갖고 있다 — 누가·언제·무엇을·왜.
    """
    for i in range(5):
        _product(db, org, f"BULK-{i}")
    rows = [HEADER] + [(f"BULK-{i}", 10, None) for i in range(5)]
    _apply(db, org, actor, _run(db, org, rows))

    n = db.execute(text("""
        SELECT COUNT(*) FROM audit_log
         WHERE org_id = :o AND domain_table = 'inventory_import_run'
    """), {"o": str(org)}).scalar_one()
    assert n == 1

    run = db.execute(text("""
        SELECT rows_total, opening, source_sha256 FROM inventory_import_run
         WHERE org_id = :o
    """), {"o": str(org)}).mappings().one()
    assert (run["rows_total"], run["opening"]) == (5, 5)
    assert run["source_sha256"] == SHA


def test_run_history_is_listed(db: Session, org, actor):
    _product(db, org, "SKU-14")
    _apply(db, org, actor, _run(db, org, [HEADER, ("SKU-14", 2, None)]))
    runs = imp.list_runs(db, org_id=org)
    assert len(runs) == 1
    assert runs[0]["mode"] == "APPLY"
    assert runs[0]["finished_at"]
