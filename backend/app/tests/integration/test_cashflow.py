"""현금흐름 읽기 모델 (Phase 6, M034).

이 파일이 고정하는 것
------------------
1. **ACTUAL 은 지급 기록이 있을 때만** 만들어진다. 매출·순이익·비용 발생은 현금이 아니다
2. 정산 레코드는 `settled_at` 이라는 이름에도 불구하고 **최대 CONFIRMED** 다
3. 지급을 취소하면 현금흐름에서 **사라진다** (soft delete 를 읽어야 한다)
4. 같은 지급을 수동 기록으로 또 넣으면 **DB 가 막는다** — 출금 이중 계상 방지
5. 기준 잔액이 없으면 잔액은 `None` 이다. **0 이 아니다**
6. 모든 줄은 원본으로 되돌아갈 수 있다 (`source_type`/`source_id`)

`docs/54_cashflow_semantics.md` 가 이 규칙들의 근거다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.services import cashflow as cf
from app.services import payout as payout_svc
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_marketplace_settlement_record,
    make_organization,
    make_partner,
    make_product,
)

# 고정 기준일 — `date.today()` 에 의존하면 테스트가 날마다 달라진다.
TODAY = date(2026, 5, 15)
WINDOW = (date(2026, 5, 1), date(2026, 5, 31))


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _approved_item(
    db: Session, org: uuid.UUID, actor: uuid.UUID, *,
    due_amount: int = 1_000_000, due_date: date | None = date(2026, 5, 20),
    payee: str = "파트너B",
) -> uuid.UUID:
    """승인된 지급 항목 하나. 승인 전(DRAFT)에는 지급 기록을 남길 수 없다."""
    batch = payout_svc.create_batch(
        db, org_id=org, actor_user_id=actor, title="5월 배분", period_year_month="2026-05",
    )
    item = payout_svc.add_item(
        db, org_id=org, actor_user_id=actor, batch_id=batch,
        item_type="PARTNER_DISTRIBUTION", payee_name=payee,
        payee_partner_id=make_partner(db, org_id=org, name=payee),
        due_amount=due_amount, due_date=due_date,
    )
    payout_svc.approve_batch(db, org_id=org, batch_id=batch, actor_user_id=actor)
    return item


def _summary(db: Session, org: uuid.UUID) -> dict:
    return cf.build_summary(
        db, org_id=org, date_from=WINDOW[0], date_to=WINDOW[1], today=TODAY,
    )


# ──────────────────────────────────────────────────────────────────────────
# ACTUAL 의 근거
# ──────────────────────────────────────────────────────────────────────────

def test_recorded_payment_is_the_only_actual_outflow(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """지급 기록이 있어야 실제 출금이다. 승인만으로는 아직 EXPECTED 다."""
    item = _approved_item(db, org, actor, due_amount=1_000_000)

    before = _summary(db, org)
    assert before["outflow"]["actual"] == 0
    assert before["outflow"]["expected"] == 1_000_000

    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
        payment_reference="TEST-TRANSFER-1",
    )

    after = _summary(db, org)
    assert after["outflow"]["actual"] == 1_000_000
    # 전액 지급됐으므로 남은 예정 출금은 없다.
    assert after["outflow"]["expected"] == 0


def test_partial_payment_splits_into_actual_and_expected(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """400만 중 100만만 줬으면 실제 출금 100만, 남은 예정 출금 300만이다.

    `due_amount` 전액을 예정 출금으로 또 세면 현금흐름이 400만 부풀어 오른다.
    """
    item = _approved_item(db, org, actor, due_amount=4_000_000)
    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
    )

    s = _summary(db, org)
    assert s["outflow"]["actual"] == 1_000_000
    assert s["outflow"]["expected"] == 3_000_000


def test_cancelled_payment_leaves_the_cashflow(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """취소한 지급이 현금흐름에 남아 있으면 나가지 않은 돈이 나간 것으로 잡힌다.

    `cancel_payment` 는 행을 지우지 않고 `deleted_at` 을 찍는다 —
    읽기 모델이 그 조건을 안 걸면 취소가 반영되지 않는다.
    """
    item = _approved_item(db, org, actor, due_amount=1_000_000)
    paid = payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
    )
    assert _summary(db, org)["outflow"]["actual"] == 1_000_000

    payout_svc.cancel_payment(
        db, org_id=org, actor_user_id=actor,
        payment_id=uuid.UUID(str(paid["payment_id"])), reason="계좌번호 오류",
    )

    s = _summary(db, org)
    assert s["outflow"]["actual"] == 0
    # 취소로 미지급이 되살아났으므로 예정 출금으로 돌아온다.
    assert s["outflow"]["expected"] == 1_000_000


def test_unapproved_payout_is_not_an_expected_outflow(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """승인 전 항목은 줄지 말지 아직 정해지지 않았다. 현금 계획에 넣지 않는다."""
    batch = payout_svc.create_batch(
        db, org_id=org, actor_user_id=actor, title="초안", period_year_month="2026-05",
    )
    payout_svc.add_item(
        db, org_id=org, actor_user_id=actor, batch_id=batch,
        item_type="PARTNER_DISTRIBUTION", payee_name="미승인",
        due_amount=5_000_000, due_date=date(2026, 5, 20),
    )

    assert _summary(db, org)["outflow"]["expected"] == 0


# ──────────────────────────────────────────────────────────────────────────
# 손익은 현금이 아니다
# ──────────────────────────────────────────────────────────────────────────

def test_sales_revenue_never_appears_as_cash(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """매출 3천만이 있어도 현금 유입은 0이다.

    이 프로젝트에서 가장 위험한 착각이다 — 팔렸다고 통장에 들어온 게 아니다.
    """
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO sales_order
          (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
           product_id, product_snapshot_name, quantity, sale_price, purchase_cost, line_no)
        VALUES (:id, :o, :bk, :d, 'COUPANG', :ono, :pid, '상품', 1, 30000000, 0, 1)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}",
           "d": date(2026, 5, 5), "ono": f"ORD-{sid.hex[:10]}",
           "pid": str(make_product(db, org_id=org))})
    db.commit()

    s = _summary(db, org)
    assert s["inflow"] == {"expected": 0, "confirmed": 0, "actual": 0}
    assert not any(r["source_type"] == "sales_order" for r in s["rows"])


# ──────────────────────────────────────────────────────────────────────────
# 정산 — 이름만 보고 ACTUAL 로 올리지 않는다
# ──────────────────────────────────────────────────────────────────────────

def _settlement(
    db: Session, org: uuid.UUID, *, net: int, settled_at: date | None,
) -> uuid.UUID:
    rid = make_marketplace_settlement_record(
        db, org_id=org, marketplace="COUPANG",
        settlement_period_start=date(2026, 5, 1),
        settlement_period_end=date(2026, 5, 31),
        gross_revenue=net,
        integration_account_id=make_integration_account(
            db, org_id=org, provider="COUPANG", account_label="본점",
        ),
    )
    db.execute(text("""
        UPDATE marketplace_settlement_record
           SET net_settlement = :n, settled_at = :d WHERE id = :i
    """), {"n": net, "d": settled_at, "i": str(rid)})
    db.commit()
    return rid


def test_settlement_is_confirmed_not_actual(db: Session, org: uuid.UUID):
    """`settled_at` 이 실제 은행 입금일이라는 근거가 없다 (docs/54 §3-2).

    ACTUAL 로 올리면 "2천만원 들어왔다" 가 화면에 뜨는데 통장에는 없을 수 있다.
    """
    _settlement(db, org, net=20_000_000, settled_at=date(2026, 5, 12))

    s = _summary(db, org)
    assert s["inflow"]["confirmed"] == 20_000_000
    assert s["inflow"]["actual"] == 0
    assert s["actual_inflow_source_available"] is False


def test_settlement_amount_is_never_unknown(db: Session, org: uuid.UUID):
    """읽기 모델은 `net_settlement` 가 항상 있다고 가정한다 — 스키마가 그걸 보장한다.

    나중에 이 컬럼이 nullable 이 되면 현금흐름은 금액 없는 정산서를 만나 깨진다.
    그때 이 테스트가 먼저 실패해야 한다.
    """
    _settlement(db, org, net=5_000_000, settled_at=date(2026, 5, 12))

    with pytest.raises(IntegrityError):
        db.execute(text(
            "UPDATE marketplace_settlement_record SET net_settlement = NULL"
            " WHERE org_id = :o"
        ), {"o": str(org)})
        db.flush()
    db.rollback()


def test_settlement_without_a_date_is_not_placed_on_the_timeline(
    db: Session, org: uuid.UUID
):
    """날짜를 모르면 어느 칸에도 넣을 수 없다. 오늘 날짜로 밀어 넣지 않는다."""
    _settlement(db, org, net=7_000_000, settled_at=None)
    assert _summary(db, org)["inflow"]["confirmed"] == 0


# ──────────────────────────────────────────────────────────────────────────
# 수동 현금 기록 — 이중 계상 방지
# ──────────────────────────────────────────────────────────────────────────

def test_manual_inflow_is_the_only_way_to_get_an_actual_inflow(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """은행 API 가 없으므로 사람이 확인한 기록만이 실제 입금 근거다."""
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
        amount=3_000_000, occurred_at=date(2026, 5, 8), category="쿠팡 정산 입금",
    )

    s = _summary(db, org)
    assert s["inflow"]["actual"] == 3_000_000
    assert s["actual_inflow_source_available"] is True


def test_recording_an_already_counted_payment_by_hand_is_rejected(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """지급 기록은 이미 실제 출금으로 잡힌다. 손으로 또 넣으면 두 배가 된다."""
    item = _approved_item(db, org, actor, due_amount=1_000_000)
    paid = payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
    )

    with pytest.raises(ValidationError) as err:
        cf.create_manual_event(
            db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
            amount=1_000_000, occurred_at=date(2026, 5, 10),
            source_type="payout_payment",
            source_id=uuid.UUID(str(paid["payment_id"])),
        )
    assert "두 배" in str(err.value)


def test_a_linked_row_that_slips_past_the_service_is_still_not_double_counted(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """서비스를 우회해 들어온 행도 합계를 부풀리지 못한다.

    M034 의 UNIQUE 는 "같은 원본을 두 번" 만 막는다. 원본과 **나란히** 서는 것은
    막지 못하므로, 읽기 모델이 직접 집계하는 source 를 가리키는 행은 제외한다.
    import 나 직접 SQL 로 들어온 행에도 같은 보호가 필요하다.
    """
    item = _approved_item(db, org, actor, due_amount=1_000_000)
    paid = payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
    )
    db.execute(text("""
        INSERT INTO manual_cash_event
               (org_id, direction, status, amount, occurred_at, source_type, source_id)
        VALUES (:o, 'OUTFLOW', 'ACTUAL', 1000000, :d, 'payout_payment', :sid)
    """), {"o": str(org), "d": date(2026, 5, 10), "sid": str(paid["payment_id"])})
    db.commit()

    # 지급 기록 한 건분만 잡힌다 — 200만이 아니다.
    assert _summary(db, org)["outflow"]["actual"] == 1_000_000


def test_the_db_blocks_two_events_on_the_same_source(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """읽기 모델이 세지 않는 source(예: 나중에 붙을 은행 거래)도 두 번은 안 된다.

    애플리케이션 검사만으로는 동시 요청에서 뚫리므로 부분 UNIQUE 가 막는다(M034).
    """
    src = uuid.uuid4()
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
        amount=1_000_000, occurred_at=date(2026, 5, 10),
        source_type="bank_transaction", source_id=src,
    )
    with pytest.raises(ConflictError):
        cf.create_manual_event(
            db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
            amount=1_000_000, occurred_at=date(2026, 5, 10),
            source_type="bank_transaction", source_id=src,
        )


def test_unlinked_manual_events_may_repeat(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """같은 날 같은 금액의 현금 지출이 두 번 있을 수 있다. 그건 중복이 아니다."""
    for _ in range(2):
        cf.create_manual_event(
            db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
            amount=50_000, occurred_at=date(2026, 5, 9), category="현금 소모품",
        )
    assert _summary(db, org)["outflow"]["actual"] == 100_000


def test_actual_event_requires_a_date(db: Session, org: uuid.UUID, actor: uuid.UUID):
    with pytest.raises(ValidationError):
        cf.create_manual_event(
            db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
            amount=1_000, expected_at=date(2026, 5, 9),
        )


def test_amount_must_be_positive(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """방향은 `direction` 이 정한다. 음수로 방향을 표현하면 합계에서 뒤집힌다."""
    with pytest.raises(ValidationError):
        cf.create_manual_event(
            db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
            amount=-5_000, occurred_at=date(2026, 5, 9),
        )


def test_db_rejects_an_actual_event_without_a_date(db: Session, org: uuid.UUID):
    """서비스를 우회해도 CHECK 가 막는다."""
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO manual_cash_event (org_id, direction, status, amount)
            VALUES (:o, 'INFLOW', 'ACTUAL', 1000)
        """), {"o": str(org)})
        db.flush()
    db.rollback()


def test_deleted_manual_event_leaves_the_cashflow(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    ev = cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=200_000, occurred_at=date(2026, 5, 9),
    )
    cf.delete_manual_event(
        db, org_id=org, actor_user_id=actor,
        event_id=uuid.UUID(ev["id"]), reason="잘못 입력",
    )

    assert _summary(db, org)["outflow"]["actual"] == 0
    with pytest.raises(NotFoundError):
        cf.get_manual_event(db, org_id=org, event_id=uuid.UUID(ev["id"]))


def test_delete_requires_a_reason(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """사유 없이 지우면 나중에 금액이 왜 달라졌는지 설명할 수 없다."""
    ev = cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=1_000, occurred_at=date(2026, 5, 9),
    )
    with pytest.raises(ValidationError):
        cf.delete_manual_event(
            db, org_id=org, actor_user_id=actor, event_id=uuid.UUID(ev["id"]), reason="  ",
        )


def test_deleting_a_linked_event_frees_the_source(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """잘못 연결한 것을 지운 뒤 다시 넣을 수 있어야 한다.

    UNIQUE 가 `deleted_at IS NULL` 조건이 없으면 한 번 실수하면 영영 못 넣는다.
    """
    src = uuid.uuid4()
    first = cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=1_000, occurred_at=date(2026, 5, 9),
        source_type="bank_transaction", source_id=src,
    )
    cf.delete_manual_event(
        db, org_id=org, actor_user_id=actor,
        event_id=uuid.UUID(first["id"]), reason="원본 잘못 지정",
    )
    again = cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=2_000, occurred_at=date(2026, 5, 9),
        source_type="bank_transaction", source_id=src,
    )
    assert again["amount"] == 2_000


# ──────────────────────────────────────────────────────────────────────────
# 기준 잔액 — ZERO 와 UNKNOWN
# ──────────────────────────────────────────────────────────────────────────

def test_balance_is_unknown_without_a_baseline(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """기준 잔액이 없으면 잔액은 `None` 이다. 0 으로 두면 "통장에 0원" 으로 읽힌다."""
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=500_000, occurred_at=date(2026, 5, 9),
    )

    s = _summary(db, org)
    assert s["balance_available"] is False
    assert s["projected_ending_balance"] is None
    assert all(row["balance"] is None for row in s["timeline"])
    # 유출 합계는 0 이 아니라 실제 금액이다 — 모르는 것과 0 은 다르다.
    assert s["outflow"]["actual"] == 500_000


def test_projected_balance_walks_from_the_baseline(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor,
        as_of_date=date(2026, 5, 1), amount=10_000_000, memo="주거래 통장",
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
        amount=3_000_000, occurred_at=date(2026, 5, 5),
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=4_000_000, occurred_at=date(2026, 5, 20),
    )

    s = _summary(db, org)
    assert s["balance_available"] is True
    assert s["projected_ending_balance"] == 9_000_000
    # 최저점은 마지막 잔액이 아니다 — 중간에 바닥을 치면 그때 돈이 모자란다.
    assert s["lowest_projected_balance"] == 9_000_000
    assert [t["balance"] for t in s["timeline"]] == [13_000_000, 9_000_000]


def test_lowest_balance_catches_a_dip_in_the_middle(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """말일 잔액만 보면 중간의 자금 부족을 놓친다."""
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=date(2026, 5, 1), amount=1_000_000,
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="EXPECTED",
        amount=5_000_000, expected_at=date(2026, 5, 10),
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="EXPECTED",
        amount=9_000_000, expected_at=date(2026, 5, 25),
    )

    s = _summary(db, org)
    assert s["projected_ending_balance"] == 5_000_000
    assert s["lowest_projected_balance"] == -4_000_000


def test_baseline_is_replaced_not_duplicated(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """같은 날에 기준 잔액이 둘이면 어느 쪽이 맞는지 알 수 없다."""
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=date(2026, 5, 1), amount=1_000_000,
    )
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=date(2026, 5, 1), amount=2_000_000,
    )

    assert cf.get_baseline(db, org_id=org)["amount"] == 2_000_000
    n = db.execute(text("""
        SELECT COUNT(*) FROM cash_balance_baseline
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalar()
    assert n == 1


def test_baseline_uses_the_latest_one_before_the_window(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """5월 현금흐름은 5월 이후에 입력한 기준 잔액을 쓰면 안 된다 — 미래를 당겨쓰는 것이다."""
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=date(2026, 4, 1), amount=1_000_000,
    )
    cf.set_baseline(
        db, org_id=org, actor_user_id=actor, as_of_date=date(2026, 6, 1), amount=9_000_000,
    )

    assert _summary(db, org)["baseline"]["amount"] == 1_000_000


# ──────────────────────────────────────────────────────────────────────────
# 원본 추적 · 격리
# ──────────────────────────────────────────────────────────────────────────

def test_every_row_can_be_traced_back_to_its_source(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """출처 없는 숫자를 보여주면 "이 300만원이 뭐지" 에 답할 수 없다."""
    item = _approved_item(db, org, actor, due_amount=1_000_000)
    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=400_000,
    )
    _settlement(db, org, net=2_000_000, settled_at=date(2026, 5, 12))
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="ACTUAL",
        amount=100_000, occurred_at=date(2026, 5, 13),
    )

    rows = _summary(db, org)["rows"]
    assert {r["source_type"] for r in rows} == {
        "payout_payment", "payout_item",
        "marketplace_settlement_record", "manual_cash_event",
    }
    for r in rows:
        assert r["source_id"], r
        assert r["drill_href"], r
        assert r["label"]


def test_cashflow_does_not_leak_across_orgs(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    other = make_organization(db)
    other_actor = make_app_user(db, org_id=other, role="ADMIN")
    cf.create_manual_event(
        db, org_id=other, actor_user_id=other_actor, direction="INFLOW", status="ACTUAL",
        amount=99_000_000, occurred_at=date(2026, 5, 9),
    )
    cf.set_baseline(
        db, org_id=other, actor_user_id=other_actor,
        as_of_date=date(2026, 5, 1), amount=99_000_000,
    )

    s = _summary(db, org)
    assert s["inflow"]["actual"] == 0
    assert s["rows"] == []
    assert s["baseline"] is None


def test_rows_outside_the_window_are_excluded(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=700_000, occurred_at=date(2026, 4, 30),
    )
    assert _summary(db, org)["outflow"]["actual"] == 0


# ──────────────────────────────────────────────────────────────────────────
# 미지급 / 연체
# ──────────────────────────────────────────────────────────────────────────

def test_unpaid_total_includes_items_without_a_due_date(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """기한 없는 미지급이 타임라인에는 못 들어가도 **빚이라는 사실은 사라지지 않는다.**"""
    _approved_item(db, org, actor, due_amount=3_000_000, due_date=None)

    s = _summary(db, org)
    assert s["unpaid_payout"] == 3_000_000
    # 날짜가 없으므로 기간 타임라인에는 넣을 수 없다.
    assert s["outflow"]["expected"] == 0


def test_overdue_is_measured_against_today(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    _approved_item(db, org, actor, due_amount=1_000_000, due_date=date(2026, 5, 5))
    _approved_item(db, org, actor, due_amount=2_000_000, due_date=date(2026, 5, 25),
                   payee="파트너A")

    s = _summary(db, org)
    assert s["unpaid_payout"] == 3_000_000
    assert s["overdue_payout"] == 1_000_000


# ──────────────────────────────────────────────────────────────────────────
# 기간 해석
# ──────────────────────────────────────────────────────────────────────────

def test_preset_looks_forward_from_today():
    """현금흐름은 예측이다 — 프리셋은 과거가 아니라 앞으로를 본다."""
    start, end = cf.resolve_range(
        preset="30d", date_from=None, date_to=None, today=TODAY,
    )
    assert start == TODAY
    assert (end - start).days == 30


def test_reversed_range_is_rejected():
    with pytest.raises(ValidationError):
        cf.resolve_range(
            preset=None, date_from=date(2026, 5, 31), date_to=date(2026, 5, 1),
        )


# ──────────────────────────────────────────────────────────────────────────
# 데이터 품질 검사 (Phase 8)
#
# 현금 기록은 그 달의 손익에 들어가지 않는다(docs/54 §1). 그래서 확정을 막지
# 않는다 — 등급을 올리면 마감이 통장 정리에 인질로 잡힌다.
# ──────────────────────────────────────────────────────────────────────────

def _codes(db: Session, org: uuid.UUID) -> dict[str, str]:
    from app.services import close_checks

    return {
        i.code: i.grade
        for i in close_checks.run_checks(db, org_id=org, year_month="2026-05")
    }


def test_manual_outflow_matching_a_payment_is_flagged(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """원본 연결이 없으면 DB 도 읽기 모델도 같은 건인지 알 수 없다.

    확신할 수 없으므로 지우거나 합치지 않고 사람에게 보여준다.
    """
    item = _approved_item(db, org, actor, due_amount=1_000_000)
    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=1_000_000, occurred_at=date(2026, 5, 10), category="파트너 송금",
    )

    assert _codes(db, org).get("CASH_POSSIBLE_DOUBLE_COUNT") == "WARNING"


def test_a_different_amount_on_the_same_day_is_not_flagged(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """정상 기록이 잘못 울리면 사용자가 검증 전체를 무시하게 된다."""
    item = _approved_item(db, org, actor, due_amount=1_000_000)
    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=77_000, occurred_at=date(2026, 5, 10), category="택배비",
    )

    assert "CASH_POSSIBLE_DOUBLE_COUNT" not in _codes(db, org)


def test_expired_expectation_is_flagged(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """예정일이 지났는데 그대로면 예상 잔액이 계속 틀린다."""
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="EXPECTED",
        amount=2_000_000, expected_at=date(2026, 5, 3),
    )

    assert _codes(db, org).get("STALE_EXPECTED_CASHFLOW") == "WARNING"


def test_unexplained_actual_is_informational_only(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """금액이 틀린 건 아니다. 확정을 막지도, 확인을 요구하지도 않는다."""
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=300_000, occurred_at=date(2026, 5, 9),
    )

    assert _codes(db, org).get("CASH_ACTUAL_UNEXPLAINED") == "INFO"


def test_a_described_actual_raises_no_issue(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=300_000, occurred_at=date(2026, 5, 9), memo="현금 소모품 구입",
    )

    assert "CASH_ACTUAL_UNEXPLAINED" not in _codes(db, org)


def test_cashflow_checks_never_block_the_close(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """현금 기록은 그 달의 손익 숫자에 들어가지 않는다 — 확정을 막을 근거가 없다."""
    from app.services import close_checks

    item = _approved_item(db, org, actor, due_amount=1_000_000)
    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item,
        payment_date=date(2026, 5, 10), amount=1_000_000,
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="OUTFLOW", status="ACTUAL",
        amount=1_000_000, occurred_at=date(2026, 5, 10),
    )
    cf.create_manual_event(
        db, org_id=org, actor_user_id=actor, direction="INFLOW", status="EXPECTED",
        amount=2_000_000, expected_at=date(2026, 5, 3),
    )

    cash = [
        i for i in close_checks.run_checks(db, org_id=org, year_month="2026-05")
        if i.domain == "CASHFLOW"
    ]
    assert cash, "현금 검사가 하나도 실행되지 않았다"
    assert not any(i.blocking for i in cash)
