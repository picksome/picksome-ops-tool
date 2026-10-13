"""확정 수익배분 + 지급 장부 (Phase 31~34).

이 파일이 고정하는 것
------------------
1. 확정 배분은 **월 확정 snapshot 없이는 만들 수 없다**
2. 배분 기준은 snapshot 의 금액이다 — 확정 후 판매가 늘어도 배분액은 안 바뀐다
3. 재오픈하면 배분도 superseded — 삭제되지는 않는다
4. **PAID 는 지급 기록 없이 만들 수 없다**
5. 과지급은 차단된다. 부분 지급은 지원한다
6. 동업자 배분은 비용이 아니고, 급여·상환은 이미 비용이다 (회계 효과 구분)
7. 개인카드 상환이 새 비용을 만들지 않고, 같은 비용에 두 번 생기지 않는다
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationError
from app.services import distribution as dist
from app.services import monthly_close as mc
from app.services import payout as payout_svc
from app.tests.fixtures.factories import (
    confirm_month,
    lock_month,
    make_app_user,
    make_expense_category_policy,
    make_organization,
    make_partner,
    make_partner_share_policy,
    make_product,
)

YM = "2026-02"
PERIOD_START = date(2026, 2, 1)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def partners(db: Session, org: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    a = make_partner(db, org_id=org, name="파트너B")
    b = make_partner(db, org_id=org, name="파트너A")
    make_partner_share_policy(db, org_id=org, partner_id=a, share_ratio=Decimal("0.6"))
    make_partner_share_policy(db, org_id=org, partner_id=b, share_ratio=Decimal("0.4"))
    return a, b


def _month_with_profit(db: Session, org: uuid.UUID, actor: uuid.UUID,
                       *, revenue: int = 10_000_000, cost: int = 4_000_000) -> uuid.UUID:
    """매출 - 원가 = 순이익이 나는 달 하나를 만든다."""
    ms_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO monthly_settlement
          (id, org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense, net_profit)
        VALUES (:id, :o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0)
    """), {"id": str(ms_id), "o": str(org), "bk": f"MS-{ms_id.hex[:10]}", "ym": YM})

    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO sales_order
          (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
           product_id, product_snapshot_name, quantity, sale_price, purchase_cost, line_no)
        VALUES (:id, :o, :bk, :d, 'COUPANG', :ono, :pid, '상품', 1, :sp, :pc, 1)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}", "d": PERIOD_START,
           "ono": f"ORD-{sid.hex[:10]}", "pid": str(make_product(db, org_id=org)),
           "sp": revenue, "pc": cost})
    db.execute(text("""
        INSERT INTO product_margin_actual
          (id, org_id, sales_order_id, actual_revenue, actual_cost,
           actual_marketplace_fee, actual_payment_fee, actual_ad_cost,
           actual_fulfillment_fee, actual_return_cost, actual_vat,
           actual_shipping_cost, actual_margin_amount, actual_margin_rate)
        VALUES (:id, :o, :so, :rev, :cost, 0, 0, 0, 0, 0, 0, 0, :m, 0)
    """), {"id": str(uuid.uuid4()), "o": str(org), "so": str(sid),
           "rev": revenue, "cost": cost, "m": revenue - cost})
    return ms_id


def _personal_card_expense(
    db: Session, org: uuid.UUID, holder: uuid.UUID, amount: int, desc: str = "출장 식대",
) -> uuid.UUID:
    eid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO expenses (
          id, org_id, expense_date, category_id, description,
          gross_amount, supply_amount, vat_amount, payment_method,
          payment_card_holder_id, is_personal_card,
          is_settlement_deductible, is_business_use
        ) VALUES (
          :id, :o, :d, :c, :desc, :amt, :amt, 0,
          CAST('PERSONAL_CARD' AS enum_payment_method), :holder, TRUE, TRUE, TRUE
        )
    """), {"id": str(eid), "o": str(org), "d": PERIOD_START,
           "c": str(make_expense_category_policy(db, org_id=org,
                                                 name=f"카테고리-{eid.hex[:6]}")),
           "desc": desc, "amt": amount, "holder": str(holder)})
    return eid


# ──────────────────────────────────────────────────────────────────────────
# 확정 수익배분
# ──────────────────────────────────────────────────────────────────────────

def test_distribution_requires_confirmed_month(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """확정 snapshot 없이 배분을 만들면 '이 금액이 어느 순이익에서 나왔나' 를 답할 수 없다."""
    _month_with_profit(db, org, actor)
    with pytest.raises(ConflictError) as exc:
        dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)
    assert "확정되지 않" in str(exc.value)


def test_distribution_splits_snapshot_profit(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    a, b = partners
    _month_with_profit(db, org, actor, revenue=10_000_000, cost=4_000_000)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    run = dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)
    assert run["distributable_base"] == 6_000_000
    assert run["total_distributed"] == 6_000_000

    by_partner = {r["partner_id"]: r for r in run["results"]}
    assert by_partner[a]["amount"] == 3_600_000
    assert by_partner[b]["amount"] == 2_400_000
    # 비율 배분은 비율이 있어야 금액을 재현할 수 있다
    assert by_partner[a]["ratio_used"] == Decimal("0.60000")
    # 규칙 버전이 박제된다 — 나중에 정책이 바뀌어도 이 배분은 재현 가능해야 한다
    assert "partner_share_policy" in run["applied_rule_versions_json"]


def test_distribution_does_not_change_when_sales_change_after_confirm(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """확정 후 판매가 늘어도 배분액은 그대로다. snapshot 을 기준으로 하기 때문이다."""
    _month_with_profit(db, org, actor, revenue=10_000_000, cost=4_000_000)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    run = dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)
    before = run["total_distributed"]

    # 확정 후 판매가 하나 더 들어왔다 (잠금 전이라 DB 는 막지 않는다)
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO sales_order
          (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
           product_id, product_snapshot_name, quantity, sale_price, purchase_cost, line_no)
        VALUES (:id, :o, :bk, :d, 'COUPANG', :ono, :pid, '추가상품', 1, 99000000, 0, 1)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}", "d": PERIOD_START,
           "ono": f"ORD-{sid.hex[:10]}", "pid": str(make_product(db, org_id=org))})

    again = dist.get_current_run(db, org_id=org, year_month=YM)
    assert again["total_distributed"] == before == 6_000_000


def test_second_run_in_same_month_is_rejected(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    _month_with_profit(db, org, actor)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)

    with pytest.raises(ConflictError):
        dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)


def test_reopen_supersedes_distribution_without_deleting_it(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """없어진 순이익을 근거로 한 배분이 유효한 것처럼 남으면 안 된다.

    그렇다고 지우지도 않는다 — 이미 지급했을 수 있고 그 근거가 사라지면 안 된다.
    """
    _month_with_profit(db, org, actor)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    run = dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)
    run_id = run["id"]
    lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    mc.unlock(db, org_id=org, year_month=YM, actor_user_id=actor, reason="재확정 필요")

    assert dist.get_current_run(db, org_id=org, year_month=YM) is None
    history = dist.list_runs(db, org_id=org, year_month=YM)
    assert len(history) == 1
    assert history[0]["id"] == run_id
    assert history[0]["superseded_at"] is not None
    assert history[0]["status"] == "SUPERSEDED"


def test_preview_marks_unconfirmed_month_as_provisional(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """확정 전 미리보기를 '확정 배분액' 이라고 부르면 안 된다."""
    _month_with_profit(db, org, actor)
    pv = dist.preview(db, org_id=org, year_month=YM)
    assert pv["is_provisional"] is True

    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    pv = dist.preview(db, org_id=org, year_month=YM)
    assert pv["is_provisional"] is False


def test_preview_does_not_create_anything(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """시뮬레이션은 **아무것도 만들지 않는다.**

    화면에 "미리보기" 라고 써 두는 것만으로는 부족하다. 미리보기가 조용히
    확정 배분이나 지급 배치를 만들면, 사용자는 시험 삼아 눌러 본 뒤
    그것이 확정된 줄 모른다. 그래서 행 개수를 직접 센다.
    """
    _month_with_profit(db, org, actor)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)

    def counts() -> tuple[int, int, int, int]:
        q = lambda t: db.execute(  # noqa: E731
            text(f"SELECT COUNT(*) FROM {t} WHERE org_id = :o"), {"o": str(org)}
        ).scalar_one()
        return q("distribution_run"), q("distribution_result"), q("payout_batch"), q("payout_item")

    before = counts()
    for _ in range(3):
        dist.preview(db, org_id=org, year_month=YM)
    assert counts() == before

    # 마감 상태도 건드리지 않는다.
    assert mc.get_close_state_of(db, org_id=org, year_month=YM) == "CONFIRMED"


def test_ratio_sum_must_be_one_and_backend_enforces_it(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """배분 비율 합 invariant 는 **서버가** 강제한다. 두 겹으로.

    화면 경고만 있으면 API 를 직접 부르거나 경고를 무시하고 넘어갈 수 있고,
    그러면 배분 합계가 순이익과 어긋난 채로 확정된다.

    서버의 방어는 두 지점에 있다.
      ① 월 마감 검증 — `DISTRIBUTION_RATIO_INVALID` 는 **BLOCKING** 이라
         확인 처리로 넘길 수 없다. 애초에 확정이 안 된다
      ② 배분 계산 — 그래도 계산을 부르면 `ConflictError` 로 거절한다
    """
    a = make_partner(db, org_id=org, name="파트너A")
    b = make_partner(db, org_id=org, name="파트너B")
    # 합이 0.9 — 10%가 어디로도 가지 않는다.
    make_partner_share_policy(db, org_id=org, partner_id=a, share_ratio=Decimal("0.6"))
    make_partner_share_policy(db, org_id=org, partner_id=b, share_ratio=Decimal("0.3"))

    _month_with_profit(db, org, actor)

    # ① 마감 검증이 차단한다. 확인 처리로 우회할 수 없다.
    items = mc.validate_month(db, org_id=org, year_month=YM)
    ratio_item = next(i for i in items if i.code == "DISTRIBUTION_RATIO_INVALID")
    assert ratio_item.grade == "BLOCKING"

    with pytest.raises(ValidationError):
        mc.confirm(
            db, org_id=org, year_month=YM, actor_user_id=actor,
            acknowledged_warnings=[i.code for i in items],
        )

    # ② 계산 경로도 스스로 거절한다 — 마감을 거치지 않는 미리보기까지 막는다.
    db.rollback()
    with pytest.raises(ConflictError) as err:
        dist.preview(db, org_id=org, year_month=YM)
    assert "1.0" in str(err.value)

    # 거부됐으면 아무 행도 남지 않아야 한다 — 반쪽 배분이 남으면 더 나쁘다.
    assert db.execute(
        text("SELECT COUNT(*) FROM distribution_run WHERE org_id = :o"), {"o": str(org)}
    ).scalar_one() == 0


def test_changing_rules_does_not_change_past_results(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """새 규칙을 승인해도 이미 확정된 배분은 그대로다.

    정책 테이블에 새 버전을 넣는 것으로 과거가 다시 계산되면, 작년 배분액이
    오늘 바뀐다. 이미 지급한 돈의 근거가 사라진다.
    """
    a, b = partners
    _month_with_profit(db, org, actor)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    run = dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)
    before = {r["partner_name_snapshot"]: r["amount"] for r in run["results"]}

    # 비율을 9:1 로 뒤집는다 (기존 정책은 종료하고 새 버전을 넣는다).
    db.execute(text("""
        UPDATE partner_share_policy SET is_active = FALSE, effective_to = :d
         WHERE org_id = :o
    """), {"o": str(org), "d": PERIOD_START})
    make_partner_share_policy(db, org_id=org, partner_id=a, share_ratio=Decimal("0.9"))
    make_partner_share_policy(db, org_id=org, partner_id=b, share_ratio=Decimal("0.1"))
    db.commit()

    after = dist.get_current_run(db, org_id=org, year_month=YM)
    assert {r["partner_name_snapshot"]: r["amount"] for r in after["results"]} == before


# ──────────────────────────────────────────────────────────────────────────
# 지급 장부
# ──────────────────────────────────────────────────────────────────────────

def _approved_distribution_batch(
    db: Session, org: uuid.UUID, actor: uuid.UUID
) -> dict:
    _month_with_profit(db, org, actor, revenue=10_000_000, cost=4_000_000)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    run = dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)
    dist.approve_run(db, org_id=org, run_id=uuid.UUID(str(run["id"])),
                     actor_user_id=actor)
    return payout_svc.create_batch_from_distribution(
        db, org_id=org, year_month=YM, actor_user_id=actor,
    )


def test_payout_batch_from_distribution(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    batch = _approved_distribution_batch(db, org, actor)
    assert batch["due_total"] == 6_000_000
    assert batch["paid_total"] == 0
    assert len(batch["items"]) == 2
    # 동업자 배분은 순이익 후 이익 처분이다. 비용이 아니다.
    assert all(i["accounting_effect"] == "PROFIT_APPROPRIATION" for i in batch["items"])
    assert all(i["item_type"] == "PARTNER_DISTRIBUTION" for i in batch["items"])


def test_unapproved_distribution_cannot_create_payout(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """검토되지 않은 금액을 지급 대상으로 올리면 안 된다."""
    _month_with_profit(db, org, actor)
    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    dist.create_run(db, org_id=org, year_month=YM, actor_user_id=actor)

    with pytest.raises(ConflictError) as exc:
        payout_svc.create_batch_from_distribution(
            db, org_id=org, year_month=YM, actor_user_id=actor,
        )
    assert "승인된 배분" in str(exc.value)


def test_paid_status_requires_actual_payment_record(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """지급 기록 없이 PAID 를 만드는 경로가 DB 수준에서 없다."""
    batch = _approved_distribution_batch(db, org, actor)
    payout_svc.approve_batch(db, org_id=org, batch_id=uuid.UUID(str(batch["id"])),
                             actor_user_id=actor)
    item_id = batch["items"][0]["id"]

    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE payout_item SET status = 'PAID' WHERE id = :i"),
                   {"i": str(item_id)})
    db.rollback()


def test_partial_payment_then_full(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """지급 예정 3,600,000 → 1차 2,000,000 → 잔액 1,600,000 → 완납."""
    batch = _approved_distribution_batch(db, org, actor)
    payout_svc.approve_batch(db, org_id=org, batch_id=uuid.UUID(str(batch["id"])),
                             actor_user_id=actor)
    item = next(i for i in batch["items"] if i["due_amount"] == 3_600_000)
    item_id = uuid.UUID(str(item["id"]))

    r1 = payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item_id,
        payment_date=date(2026, 3, 10), amount=2_000_000, payment_reference="TRX-1",
    )
    assert r1["item"]["status"] == "PARTIALLY_PAID"
    assert r1["item"]["outstanding_amount"] == 1_600_000

    r2 = payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item_id,
        payment_date=date(2026, 3, 20), amount=1_600_000, payment_reference="TRX-2",
    )
    assert r2["item"]["status"] == "PAID"
    assert r2["item"]["outstanding_amount"] == 0

    payments = payout_svc.list_payments(db, org_id=org, item_id=item_id)
    assert [p["amount"] for p in payments] == [2_000_000, 1_600_000]


def test_overpayment_is_rejected(db: Session, org: uuid.UUID, actor: uuid.UUID, partners):
    """과지급을 지원해야 할 업무 근거가 없다. 열어 두면 오타가 조용히 통과한다."""
    batch = _approved_distribution_batch(db, org, actor)
    payout_svc.approve_batch(db, org_id=org, batch_id=uuid.UUID(str(batch["id"])),
                             actor_user_id=actor)
    item = next(i for i in batch["items"] if i["due_amount"] == 3_600_000)

    with pytest.raises(ValidationError) as exc:
        payout_svc.record_payment(
            db, org_id=org, actor_user_id=actor, item_id=uuid.UUID(str(item["id"])),
            payment_date=date(2026, 3, 10), amount=3_600_001,
        )
    # 사용자가 다음에 무엇을 해야 하는지 알 수 있어야 한다
    assert "남은 지급액" in str(exc.value)


def test_payment_before_approval_is_rejected(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    batch = _approved_distribution_batch(db, org, actor)
    item = batch["items"][0]
    with pytest.raises(ConflictError) as exc:
        payout_svc.record_payment(
            db, org_id=org, actor_user_id=actor, item_id=uuid.UUID(str(item["id"])),
            payment_date=date(2026, 3, 10), amount=1000,
        )
    assert "승인" in str(exc.value)


def test_cancelling_a_payment_reduces_paid_amount(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """잘못 입력한 지급을 취소하면 금액이 줄고 상태도 되돌아간다."""
    batch = _approved_distribution_batch(db, org, actor)
    payout_svc.approve_batch(db, org_id=org, batch_id=uuid.UUID(str(batch["id"])),
                             actor_user_id=actor)
    item = next(i for i in batch["items"] if i["due_amount"] == 3_600_000)
    item_id = uuid.UUID(str(item["id"]))

    r = payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item_id,
        payment_date=date(2026, 3, 10), amount=3_600_000,
    )
    assert r["item"]["status"] == "PAID"

    payout_svc.cancel_payment(
        db, org_id=org, actor_user_id=actor,
        payment_id=uuid.UUID(r["payment_id"]), reason="이체 실패로 취소",
    )
    after = db.execute(text("""
        SELECT paid_amount, status FROM payout_item WHERE id = :i
    """), {"i": str(item_id)}).mappings().one()
    assert after["paid_amount"] == 0
    assert after["status"] == "APPROVED"


# ──────────────────────────────────────────────────────────────────────────
# 개인카드 상환 (Phase 34)
# ──────────────────────────────────────────────────────────────────────────

def test_reimbursement_does_not_create_a_new_expense(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """원래 비용이 이미 손익에 반영돼 있다. 상환이 비용을 하나 더 만들면 이중 계상이다."""
    a, _ = partners
    eid = _personal_card_expense(db, org, a, 150_000)

    before = db.execute(text("""
        SELECT COUNT(*), COALESCE(SUM(gross_amount), 0) FROM expenses
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).first()

    batch = payout_svc.create_reimbursement_batch(
        db, org_id=org, actor_user_id=actor, expense_ids=[eid], title="2월 개인카드 상환",
    )

    after = db.execute(text("""
        SELECT COUNT(*), COALESCE(SUM(gross_amount), 0) FROM expenses
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).first()

    assert (after[0], after[1]) == (before[0], before[1]), "상환이 비용을 만들면 안 된다"
    assert batch["due_total"] == 150_000
    assert batch["items"][0]["accounting_effect"] == "ALREADY_EXPENSED"


def test_same_expense_cannot_be_reimbursed_twice(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    a, _ = partners
    eid = _personal_card_expense(db, org, a, 150_000)
    payout_svc.create_reimbursement_batch(
        db, org_id=org, actor_user_id=actor, expense_ids=[eid], title="1차",
    )
    with pytest.raises(IntegrityError):
        payout_svc.create_reimbursement_batch(
            db, org_id=org, actor_user_id=actor, expense_ids=[eid], title="2차",
        )
    db.rollback()


def test_reimbursed_expense_disappears_from_candidates(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    a, _ = partners
    eid = _personal_card_expense(db, org, a, 150_000)
    assert any(c["expense_id"] == eid for c in
               payout_svc.list_reimbursement_candidates(db, org_id=org))

    payout_svc.create_reimbursement_batch(
        db, org_id=org, actor_user_id=actor, expense_ids=[eid], title="상환",
    )
    assert not any(c["expense_id"] == eid for c in
                   payout_svc.list_reimbursement_candidates(db, org_id=org))


def test_reimbursement_status_is_computed_from_payments(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """비용 자체의 상태를 바꾸지 않는다. 지급 기록이 진실이다."""
    a, _ = partners
    eid = _personal_card_expense(db, org, a, 150_000)

    assert payout_svc.reimbursement_status_of(
        db, org_id=org, expense_id=eid)["status"] == "PENDING"

    batch = payout_svc.create_reimbursement_batch(
        db, org_id=org, actor_user_id=actor, expense_ids=[eid], title="상환",
    )
    payout_svc.approve_batch(db, org_id=org, batch_id=uuid.UUID(str(batch["id"])),
                             actor_user_id=actor)
    item_id = uuid.UUID(str(batch["items"][0]["id"]))

    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item_id,
        payment_date=date(2026, 3, 5), amount=100_000,
    )
    st = payout_svc.reimbursement_status_of(db, org_id=org, expense_id=eid)
    assert st["status"] == "PARTIALLY_PAID"
    assert st["paid_amount"] == 100_000

    payout_svc.record_payment(
        db, org_id=org, actor_user_id=actor, item_id=item_id,
        payment_date=date(2026, 3, 6), amount=50_000,
    )
    assert payout_svc.reimbursement_status_of(
        db, org_id=org, expense_id=eid)["status"] == "PAID"

    # 원본 비용 상태는 건드리지 않았다
    row = db.execute(text("""
        SELECT is_personal_card, evidence_status::text FROM expenses WHERE id = :e
    """), {"e": str(eid)}).mappings().one()
    assert row["is_personal_card"] is True


def test_business_card_expense_is_not_a_reimbursement_candidate(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    eid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO expenses (
          id, org_id, expense_date, category_id, description,
          gross_amount, supply_amount, vat_amount, payment_method,
          is_personal_card, is_settlement_deductible, is_business_use
        ) VALUES (
          :id, :o, :d, :c, '법인카드 결제', 50000, 50000, 0,
          CAST('BUSINESS_CARD' AS enum_payment_method), FALSE, TRUE, TRUE
        )
    """), {"id": str(eid), "o": str(org), "d": PERIOD_START,
           "c": str(make_expense_category_policy(db, org_id=org))})

    assert not any(c["expense_id"] == eid for c in
                   payout_svc.list_reimbursement_candidates(db, org_id=org))
    assert payout_svc.reimbursement_status_of(
        db, org_id=org, expense_id=eid)["status"] == "NOT_REQUIRED"


def test_salary_and_distribution_have_different_accounting_effects(
    db: Session, org: uuid.UUID, actor: uuid.UUID, partners
):
    """직원 급여 비용과 동업자 배분을 같은 계산 단계로 처리하지 않는다."""
    a, _ = partners
    batch_id = payout_svc.create_batch(
        db, org_id=org, actor_user_id=actor, title="혼합 배치", period_year_month=YM,
    )
    payout_svc.add_item(
        db, org_id=org, actor_user_id=actor, batch_id=batch_id,
        item_type="EMPLOYEE_SALARY", payee_name="직원A", due_amount=2_500_000,
    )
    payout_svc.add_item(
        db, org_id=org, actor_user_id=actor, batch_id=batch_id,
        item_type="PARTNER_DISTRIBUTION", payee_name="파트너B",
        payee_partner_id=a, due_amount=1_000_000,
    )
    items = {i["item_type"]: i for i in
             payout_svc.list_items(db, org_id=org, batch_id=batch_id)}
    assert items["EMPLOYEE_SALARY"]["accounting_effect"] == "ALREADY_EXPENSED"
    assert items["PARTNER_DISTRIBUTION"]["accounting_effect"] == "PROFIT_APPROPRIATION"


def test_wrong_accounting_effect_is_rejected_by_db(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """동업자 배분을 비용으로 만드는 조합은 DB 가 거부한다."""
    batch_id = payout_svc.create_batch(
        db, org_id=org, actor_user_id=actor, title="잘못된 조합",
    )
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO payout_item (
              org_id, batch_id, item_type, accounting_effect,
              payee_name_snapshot, due_amount
            ) VALUES (
              :o, :b, 'PARTNER_DISTRIBUTION', 'ALREADY_EXPENSED', '파트너B', 1000
            )
        """), {"o": str(org), "b": str(batch_id)})
    db.rollback()
