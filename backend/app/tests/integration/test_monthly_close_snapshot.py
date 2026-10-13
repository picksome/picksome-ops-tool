"""월 마감 검증 카탈로그 · snapshot · 이월 원장 (Phase 26~28).

이 파일이 고정하는 것
------------------
1. 확정하면 그 시점 숫자가 snapshot 으로 박제된다
2. 확정 후 raw 데이터가 바뀌어도 **확정 숫자는 그대로다**
3. snapshot 은 UPDATE·DELETE 가 DB 수준에서 막힌다
4. 재오픈해도 과거 snapshot 이 지워지지 않고 세대로 남는다
5. 발견한 기존 결함 4건의 회귀 방지 (D1~D4)
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, SettlementLockedError, ValidationError
from app.services import close_checks, close_snapshot
from app.services import monthly_close as mc
from app.tests.fixtures.factories import (
    confirm_month,
    lock_month,
    make_app_user,
    make_expense_category_policy,
    make_marketplace_settlement_record,
    make_organization,
    make_product,
)

YM = "2026-04"
PERIOD_START = date(2026, 4, 1)


# ──────────────────────────────────────────────────────────────────────────
# fixture
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _month_row(db: Session, org: uuid.UUID, *, net_profit: int = 0,
               carryover_offset: int = 0) -> uuid.UUID:
    ms_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO monthly_settlement
          (id, org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense,
           net_profit, carryover_offset)
        VALUES (:id, :o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, :np, :co)
    """), {"id": str(ms_id), "o": str(org), "bk": f"MS-{ms_id.hex[:10]}", "ym": YM,
           "np": net_profit, "co": carryover_offset})
    return ms_id


def _sale(db: Session, org: uuid.UUID, *, product_id: uuid.UUID | None,
          sale_price: int, purchase_cost: int | None = 1000,
          sale_date: date = PERIOD_START) -> uuid.UUID:
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO sales_order
          (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
           product_id, product_snapshot_name, quantity, sale_price, purchase_cost, line_no)
        VALUES (:id, :o, :bk, :d, 'COUPANG', :ono, :pid, '테스트상품', 1, :sp, :pc, 1)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}", "d": sale_date,
           "ono": f"ORD-{sid.hex[:10]}", "pid": str(product_id) if product_id else None,
           "sp": sale_price, "pc": purchase_cost})
    return sid


def _margin(db: Session, org: uuid.UUID, sale_id: uuid.UUID, *,
            cost: int = 1000, mfee: int = 100, ad: int = 50,
            margin_amount: int = 500) -> None:
    db.execute(text("""
        INSERT INTO product_margin_actual
          (id, org_id, sales_order_id, actual_revenue, actual_cost,
           actual_marketplace_fee, actual_payment_fee, actual_ad_cost,
           actual_fulfillment_fee, actual_return_cost, actual_vat,
           actual_shipping_cost, actual_margin_amount, actual_margin_rate,
           provisional_profit_amount)
        VALUES (:id, :o, :so, 0, :cost, :mfee, 0, :ad, 0, 0, 0, 0, :m, 0, :m)
    """), {"id": str(uuid.uuid4()), "o": str(org), "so": str(sale_id),
           "cost": cost, "mfee": mfee, "ad": ad, "m": margin_amount})


# ──────────────────────────────────────────────────────────────────────────
# 검증 카탈로그
# ──────────────────────────────────────────────────────────────────────────

def test_settlement_not_connected_is_a_warning_not_silence(db: Session, org: uuid.UUID):
    """정산 0건은 '문제 없음' 이 아니라 '연결 안 됨' 이다.

    없는 데이터를 정상으로 간주하면 정산 없이 확정한 달이 완전한 달처럼 보인다.
    """
    items = close_checks.run_checks(db, org_id=org, year_month=YM)
    codes = {i.code: i for i in items}
    assert "SETTLEMENT_NOT_CONNECTED" in codes
    assert codes["SETTLEMENT_NOT_CONNECTED"].grade == close_checks.WARNING


def test_unmatched_settlement_is_warning_not_blocking(db: Session, org: uuid.UUID):
    """D3 회귀 — 미매칭 정산은 마감을 **차단하지 않는다**.

    판매처가 정산서를 늦게 주는 일이 흔하고, 차단으로 만들면 마감이 판매처
    일정에 인질로 잡힌다 (docs/47 §4 에서 확정한 결론).
    """
    make_marketplace_settlement_record(
        db, org_id=org, gross_revenue=10_000, marketplace_fee=1_000,
        settlement_period_start=PERIOD_START, settlement_period_end=date(2026, 4, 30),
    )

    items = close_checks.run_checks(db, org_id=org, year_month=YM)
    unmatched = next(i for i in items if i.code == "UNMATCHED_SETTLEMENT")
    assert unmatched.grade == close_checks.WARNING
    assert unmatched.blocking is False
    # 연결이 됐으므로 '미연결' 경고는 사라진다 — 두 상태가 구분돼야 한다
    assert not any(i.code == "SETTLEMENT_NOT_CONNECTED" for i in items)


def test_zero_cost_sale_blocks_confirm(db: Session, org: uuid.UUID, actor: uuid.UUID):
    _month_row(db, org)
    _sale(db, org, product_id=make_product(db, org_id=org, cost_amount=0),
          sale_price=10000, purchase_cost=0)

    items = close_checks.run_checks(db, org_id=org, year_month=YM)
    assert any(i.code == "ZERO_COST_SALE" and i.grade == close_checks.BLOCKING
               for i in items)

    with pytest.raises(ValidationError):
        confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)


def test_approved_but_unapplied_blocks_confirm(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """승인됐지만 반영되지 않은 변경이 남으면 확정할 수 없다.

    그대로 확정하면 **승인 내용이 빠진 숫자**가 박제된다.
    """
    _month_row(db, org)
    db.execute(text("""
        INSERT INTO approval_request
          (id, org_id, business_key, target_type, target_id, request_type, status,
           requested_by, effective_from, proposed_changes_json, auto_apply_status)
        VALUES (:id, :o, :bk, 'product', :tid, 'COST_CHANGE', 'APPROVED',
                :u, :eff, '{}'::jsonb, 'PENDING')
    """), {"id": str(uuid.uuid4()), "o": str(org), "bk": f"AR-{uuid.uuid4().hex[:8]}",
           "tid": str(make_product(db, org_id=org)), "u": str(actor), "eff": PERIOD_START})

    items = close_checks.run_checks(db, org_id=org, year_month=YM)
    item = next(i for i in items if i.code == "APPROVAL_APPROVED_NOT_APPLIED")
    assert item.grade == close_checks.BLOCKING


def test_check_run_is_recorded_even_when_confirm_is_rejected(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """거부된 확정 시도의 근거도 남아야 한다.

    "확정을 눌렀는데 왜 안 됐나" 를 나중에 답할 수 없으면 검증 기록의 의미가 없다.
    """
    _month_row(db, org)
    _sale(db, org, product_id=make_product(db, org_id=org, cost_amount=0),
          sale_price=10000, purchase_cost=0)

    with pytest.raises(ValidationError):
        confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    runs = close_checks.list_runs(db, org_id=org, year_month=YM)
    assert runs, "검증 run 이 남아야 한다"
    assert any(r["trigger"] == "CONFIRM" for r in runs)
    assert all(r["status"] == "COMPLETED" for r in runs)
    assert runs[0]["blocking_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────
# snapshot
# ──────────────────────────────────────────────────────────────────────────

def test_confirm_creates_snapshot_with_real_figures(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    _month_row(db, org)
    pid = make_product(db, org_id=org, cost_amount=1000)
    s1 = _sale(db, org, product_id=pid, sale_price=10000)
    _margin(db, org, s1, cost=4000, mfee=1000, ad=500)
    cat = make_expense_category_policy(db, org_id=org)
    db.execute(text("""
        INSERT INTO expenses (id, org_id, expense_date, category_id, description,
                              gross_amount, supply_amount, vat_amount, payment_method,
                              is_settlement_deductible, is_business_use)
        VALUES (:id, :o, :d, :c, '테스트비용', 2000, 1818, 182,
                CAST('BUSINESS_CARD' AS enum_payment_method), TRUE, TRUE)
    """), {"id": str(uuid.uuid4()), "o": str(org), "d": PERIOD_START, "c": str(cat)})

    result = confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    assert result["close_state"] == "CONFIRMED"

    snap = close_snapshot.get_current(db, org_id=org, year_month=YM)
    assert snap is not None
    assert snap["generation"] == 1
    assert snap["gross_sales"] == 10000
    assert snap["cogs"] == 4000
    assert snap["marketplace_fees"] == 1000
    assert snap["advertising_cost"] == 500
    assert snap["total_direct_cost"] == 5500
    assert snap["business_expenses"] == 2000
    assert snap["net_profit"] == 10000 - 5500 - 2000
    # 정산이 연결되지 않았으므로 "대사 완료" 라고 주장하지 않는다
    assert snap["settlement_connected"] is False
    assert snap["profit_basis"] == "SALES_AND_EXPENSE_ONLY"
    assert len(snap["content_hash"]) == 64


def test_confirmed_figures_do_not_change_when_raw_data_changes(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """확정 후 원본이 바뀌어도 **확정 숫자는 그대로다.**

    이게 snapshot 의 존재 이유다. 실시간 재집계를 확정 숫자로 쓰면
    "4월 확정 순이익" 이 어제와 오늘 달라진다.
    """
    _month_row(db, org)
    pid = make_product(db, org_id=org, cost_amount=1000)
    s1 = _sale(db, org, product_id=pid, sale_price=10000)
    _margin(db, org, s1)

    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    before = close_snapshot.get_current(db, org_id=org, year_month=YM)["gross_sales"]

    # 확정 후 판매가 하나 더 들어왔다(잠금 전이라 DB 는 막지 않는다)
    s2 = _sale(db, org, product_id=pid, sale_price=99999)
    _margin(db, org, s2)

    after = close_snapshot.get_current(db, org_id=org, year_month=YM)["gross_sales"]
    assert after == before == 10000, "확정 숫자가 조용히 달라지면 안 된다"

    # 대신 drift 로 탐지된다 — 모르고 지나가지는 않는다
    drift = mc.check_drift(db, org_id=org, year_month=YM)
    assert drift["drifted"] is True
    assert "gross_sales" in drift["changed_fields"]


def test_lock_refuses_when_data_drifted_after_confirm(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """확정과 잠금 사이에 데이터가 바뀌었으면 잠그지 않는다.

    바뀐 채로 잠그면 박제된 숫자와 실제 데이터가 어긋난 채 영구히 봉인된다.
    """
    _month_row(db, org)
    pid = make_product(db, org_id=org, cost_amount=1000)
    _margin(db, org, _sale(db, org, product_id=pid, sale_price=10000))
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    _margin(db, org, _sale(db, org, product_id=pid, sale_price=5000))

    with pytest.raises(ConflictError) as exc:
        lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    assert "재오픈" in str(exc.value)


def test_snapshot_cannot_be_updated(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """DB 트리거가 막는다. service 에만 두면 다음 사람이 UPDATE 한 줄을 쓴다."""
    _month_row(db, org)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    snap_id = close_snapshot.get_current(db, org_id=org, year_month=YM)["id"]

    with pytest.raises(DBAPIError) as exc:
        db.execute(text("UPDATE monthly_close_snapshot SET net_profit = 1 WHERE id = :i"),
                   {"i": str(snap_id)})
    assert "수정할 수 없습니다" in str(exc.value)
    db.rollback()


def test_snapshot_cannot_be_deleted(db: Session, org: uuid.UUID, actor: uuid.UUID):
    _month_row(db, org)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    snap_id = close_snapshot.get_current(db, org_id=org, year_month=YM)["id"]

    with pytest.raises(DBAPIError) as exc:
        db.execute(text("DELETE FROM monthly_close_snapshot WHERE id = :i"),
                   {"i": str(snap_id)})
    assert "삭제할 수 없습니다" in str(exc.value)
    db.rollback()


def test_second_snapshot_in_same_generation_is_rejected(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """살아 있는 snapshot 이 있는데 또 만들려 하면 거부한다."""
    ms_id = _month_row(db, org)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    with pytest.raises(ConflictError):
        close_snapshot.create_snapshot(
            db, org_id=org, year_month=YM, monthly_settlement_id=ms_id,
            taken_at_state="CONFIRMED", actor_user_id=actor, reason="중복",
            check_run_id=None, warning_count=0, acknowledged_warnings=[],
        )


def test_reopen_supersedes_snapshot_without_deleting_it(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """재오픈해도 과거 확정 숫자는 남는다. 세대로 쌓인다."""
    _month_row(db, org)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    mc.unlock(db, org_id=org, year_month=YM, actor_user_id=actor,
              reason="정산서 늦게 도착 — 재확정 필요")

    assert close_snapshot.get_current(db, org_id=org, year_month=YM) is None
    history = close_snapshot.list_generations(db, org_id=org, year_month=YM)
    assert len(history) == 1
    assert history[0]["generation"] == 1
    assert history[0]["superseded_at"] is not None

    # 재확정하면 세대 2가 생기고, 세대 1은 그대로 남는다
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=5000))
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    history = close_snapshot.list_generations(db, org_id=org, year_month=YM)
    assert [h["generation"] for h in history] == [2, 1]
    assert history[1]["gross_sales"] == 10000, "과거 세대의 숫자는 그대로다"
    assert history[0]["gross_sales"] == 15000


def test_reopen_audit_row_is_written(db: Session, org: uuid.UUID, actor: uuid.UUID):
    _month_row(db, org)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    mc.unlock(db, org_id=org, year_month=YM, actor_user_id=actor, reason="사유 기록 확인")

    row = db.execute(text("""
        SELECT reason, reopen_count_after, snapshot_before_json
          FROM settlement_reopen_audit sra
          JOIN monthly_settlement ms ON ms.id = sra.monthly_settlement_id
         WHERE ms.org_id = :o AND ms.year_month = :ym
    """), {"o": str(org), "ym": YM}).mappings().first()
    assert row is not None
    assert row["reason"] == "사유 기록 확인"
    assert row["reopen_count_after"] == 1
    assert row["snapshot_before_json"]["superseded_snapshot_id"] is not None


# ──────────────────────────────────────────────────────────────────────────
# 발견한 기존 결함 회귀 (D1 · D4)
# ──────────────────────────────────────────────────────────────────────────

def test_legacy_lock_path_actually_locks_the_month(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """D1 회귀 — `settlement_crud.lock_settlement` 이 `close_state` 를 동기화한다.

    예전에는 `status='LOCKED'` 만 쓰고 `close_state` 를 그대로 뒀다. 월 잠금 가드는
    `close_state` 를 읽으므로 **이 경로로 잠근 달은 잠기지 않았다** —
    판매·비용이 그대로 수정 가능했다.
    """
    from app.core.month_lock import assert_month_writable
    from app.services import settlement_crud

    ms_id = _month_row(db, org)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))

    items = mc.validate_month(db, org_id=org, year_month=YM)
    warnings = [i.code for i in items if i.grade != "BLOCKING"]
    settlement_crud.confirm_settlement(
        db, org_id=org, actor=actor, settlement_id=ms_id, note="레거시 경로",
        acknowledged_warnings=warnings,
    )
    settlement_crud.lock_settlement(db, org_id=org, actor=actor, settlement_id=ms_id)

    state = db.execute(text("""
        SELECT close_state, status FROM monthly_settlement WHERE id = :i
    """), {"i": str(ms_id)}).mappings().one()
    assert state["close_state"] == "LOCKED"
    assert state["status"] == "LOCKED"

    with pytest.raises(SettlementLockedError):
        assert_month_writable(db, org_id=org, at=PERIOD_START)


def test_locking_a_loss_month_records_carryover(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """D4 회귀 — 적자월 잠금이 500 이 아니라 이월 원장을 남긴다.

    예전 구현은 `event_kind` 에 `'ACCRUE'` 를 넣었다. 컬럼은
    `enum_carryover_event` 이고 그런 라벨이 없어서 **적자월은 잠글 수 없었다.**
    """
    ms_id = _month_row(db, org, net_profit=-500_000)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))

    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    row = db.execute(text("""
        SELECT event_kind::text AS kind, amount, balance_after
          FROM loss_carryover_ledger
         WHERE org_id = :o AND source_settlement_id = :s
    """), {"o": str(org), "s": str(ms_id)}).mappings().first()
    assert row is not None, "적자가 이월 원장에 쌓여야 한다"
    assert row["kind"] == "LOSS_ADDED"
    assert row["amount"] == 500_000


def test_relock_does_not_double_count_carryover(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """재오픈 → 재잠금에서 이월이 두 번 쌓이면 다음 달 분배가 줄어든다."""
    ms_id = _month_row(db, org, net_profit=-500_000)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))

    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    mc.unlock(db, org_id=org, year_month=YM, actor_user_id=actor, reason="재확인")
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    n = db.execute(text("""
        SELECT COUNT(*) FROM loss_carryover_ledger
         WHERE org_id = :o AND source_settlement_id = :s
    """), {"o": str(org), "s": str(ms_id)}).scalar()
    assert n == 1, "재잠금이 이월을 다시 쌓으면 안 된다"


def test_confirm_is_atomic_with_snapshot(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """snapshot INSERT 가 실패하면 상태 변경도 함께 롤백된다.

    snapshot 없이 CONFIRMED 인 달이 생기면 "확정했는데 확정 숫자가 없는" 상태가 된다.
    """
    ms_id = _month_row(db, org)
    _margin(db, org, _sale(db, org, product_id=make_product(db, org_id=org),
                           sale_price=10000))

    # 같은 달 같은 세대에 snapshot 을 미리 만들어 둔다 → 확정 시 UNIQUE 충돌
    close_snapshot.create_snapshot(
        db, org_id=org, year_month=YM, monthly_settlement_id=ms_id,
        taken_at_state="CONFIRMED", actor_user_id=actor, reason="선점",
        check_run_id=None, warning_count=0, acknowledged_warnings=[],
    )
    db.commit()

    with pytest.raises((ConflictError, IntegrityError)):
        confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    db.rollback()

    state = db.execute(text("SELECT close_state FROM monthly_settlement WHERE id = :i"),
                       {"i": str(ms_id)}).scalar()
    assert state == "OPEN", "snapshot 이 실패하면 상태도 바뀌면 안 된다"
