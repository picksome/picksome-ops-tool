"""신규 도메인 보안 회귀 — 조직 격리 · 민감정보 노출 (Phase 47).

무엇을 고정하나
------------
1. **조직 격리** — 다른 조직의 마감·배분·지급·이슈에 접근할 수 없다
2. **존재 여부도 새지 않는다** — 다른 조직의 id 로 조회하면 NOT_FOUND 다.
   "권한 없음" 을 돌려주면 그 id 가 존재한다는 사실이 새어나간다
3. **승인 우회 차단** — 재오픈은 승인 밖 경로로 되지 않는다
4. **저장하지 않기로 한 것은 저장되지 않는다** — 계좌번호, stack trace

이 테스트들은 서비스 계층을 직접 부른다. 라우터의 `require_roles` 는
별도로 걸려 있고(E2E 가 403 을 확인한다), 여기서는 **서비스가 org_id 를
실제로 강제하는지**를 본다 — 라우터만 막으면 서비스를 직접 부르는
배치·워커 경로가 뚫린다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.services import close_snapshot, distribution
from app.services import monthly_close as mc
from app.services import operation_issue as issues
from app.services import payout as payout_svc
from app.services import worker_runtime as rt
from app.tests.fixtures.factories import (
    confirm_month,
    make_app_user,
    make_organization,
    make_partner,
    make_partner_share_policy,
    make_product,
)

YM = "2026-05"
PERIOD_START = date(2026, 5, 1)


def _org_with_month(db: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """조직 + 관리자 + 확정 가능한 달 하나."""
    org = make_organization(db)
    actor = make_app_user(db, org_id=org, role="ADMIN")

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
        VALUES (:id, :o, :bk, :d, 'COUPANG', :ono, :pid, '상품', 1, 1000000, 300000, 1)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}", "d": PERIOD_START,
           "ono": f"ORD-{sid.hex[:10]}", "pid": str(make_product(db, org_id=org))})
    db.execute(text("""
        INSERT INTO product_margin_actual
          (id, org_id, sales_order_id, actual_revenue, actual_cost,
           actual_marketplace_fee, actual_payment_fee, actual_ad_cost,
           actual_fulfillment_fee, actual_return_cost, actual_vat,
           actual_shipping_cost, actual_margin_amount, actual_margin_rate)
        VALUES (:id, :o, :so, 1000000, 300000, 0, 0, 0, 0, 0, 0, 0, 700000, 0)
    """), {"id": str(uuid.uuid4()), "o": str(org), "so": str(sid)})
    return org, actor, ms_id


@pytest.fixture
def org_a(db: Session):
    return _org_with_month(db)


@pytest.fixture
def org_b(db: Session):
    return _org_with_month(db)


# ──────────────────────────────────────────────────────────────────────────
# 월 마감
# ──────────────────────────────────────────────────────────────────────────

def test_month_close_is_org_scoped(db: Session, org_a, org_b):
    """A 조직이 확정해도 B 조직의 같은 달은 그대로 열려 있다."""
    a_org, a_actor, _ = org_a
    b_org, _, _ = org_b

    confirm_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)

    assert mc.get_month(db, org_id=a_org, year_month=YM)["close_state"] == "CONFIRMED"
    assert mc.get_month(db, org_id=b_org, year_month=YM)["close_state"] == "OPEN"
    assert close_snapshot.get_current(db, org_id=b_org, year_month=YM) is None


def test_snapshot_of_another_org_is_not_visible(db: Session, org_a, org_b):
    a_org, a_actor, _ = org_a
    b_org, _, _ = org_b
    confirm_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)

    assert close_snapshot.list_generations(db, org_id=b_org, year_month=YM) == []
    assert mc.check_drift(db, org_id=b_org, year_month=YM) is None


def test_check_run_of_another_org_is_not_readable(db: Session, org_a, org_b):
    """run id 를 알아도 다른 조직 것은 읽을 수 없다."""
    a_org, a_actor, _ = org_a
    b_org, _, _ = org_b

    mc.run_validation(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)
    runs = mc.list_check_runs(db, org_id=a_org, year_month=YM)
    assert runs

    run_id = uuid.UUID(str(runs[0]["id"]))
    with pytest.raises(NotFoundError):
        mc.get_check_run(db, org_id=b_org, run_id=run_id)


def test_month_close_figures_only_count_own_org(db: Session, org_a, org_b):
    """집계가 다른 조직 매출을 섞으면 순이익이 통째로 틀린다."""
    a_org, _, _ = org_a
    b_org, _, _ = org_b

    fa = close_snapshot.compute_figures(db, org_id=a_org, year_month=YM)
    fb = close_snapshot.compute_figures(db, org_id=b_org, year_month=YM)
    # 두 조직이 각각 1,000,000 매출 1건씩 — 섞이면 2,000,000 이 된다
    assert fa["gross_sales"] == 1_000_000
    assert fb["gross_sales"] == 1_000_000


# ──────────────────────────────────────────────────────────────────────────
# 재오픈 — 승인 우회 차단
# ──────────────────────────────────────────────────────────────────────────

def test_reopen_request_for_another_org_month_is_rejected(db: Session, org_a, org_b):
    a_org, a_actor, _ = org_a
    b_org, b_actor, _ = org_b

    confirm_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)
    from app.tests.fixtures.factories import lock_month

    lock_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)

    # B 조직 컨텍스트로는 A 의 잠긴 달을 열 수 없다 —
    # B 의 같은 달은 잠겨 있지 않으므로 "잠긴 달이 아니다" 로 거부된다.
    with pytest.raises(Exception) as exc:
        mc.request_reopen_approval(
            db, org_id=b_org, year_month=YM, actor_user_id=b_actor,
            reason="다른 조직의 달을 열려는 시도",
        )
    assert "잠긴 달이 아닙니다" in str(exc.value)

    # A 의 달은 여전히 잠겨 있다
    assert mc.get_month(db, org_id=a_org, year_month=YM)["close_state"] == "LOCKED"


# ──────────────────────────────────────────────────────────────────────────
# 배분 · 지급
# ──────────────────────────────────────────────────────────────────────────

def test_distribution_is_org_scoped(db: Session, org_a, org_b):
    a_org, a_actor, _ = org_a
    b_org, _, _ = org_b

    pa = make_partner(db, org_id=a_org, name="A 파트너")
    make_partner_share_policy(db, org_id=a_org, partner_id=pa, share_ratio=1)

    confirm_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)
    distribution.create_run(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)

    assert distribution.get_current_run(db, org_id=a_org, year_month=YM) is not None
    assert distribution.get_current_run(db, org_id=b_org, year_month=YM) is None
    assert distribution.list_runs(db, org_id=b_org, year_month=YM) == []


def test_payout_batch_of_another_org_is_not_found(db: Session, org_a, org_b):
    a_org, a_actor, _ = org_a
    b_org, _, _ = org_b

    batch_id = payout_svc.create_batch(
        db, org_id=a_org, actor_user_id=a_actor, title="A 조직 지급",
    )
    assert payout_svc.get_batch(db, org_id=a_org, batch_id=batch_id)["title"] == "A 조직 지급"

    # 존재 여부도 새지 않는다 — "권한 없음" 이 아니라 NOT_FOUND 다
    with pytest.raises(NotFoundError):
        payout_svc.get_batch(db, org_id=b_org, batch_id=batch_id)

    rows, total = payout_svc.list_batches(db, org_id=b_org)
    assert total == 0 and rows == []


def test_payment_cannot_be_recorded_across_orgs(db: Session, org_a, org_b):
    """다른 조직의 지급 항목에 돈을 기록할 수 없다."""
    a_org, a_actor, _ = org_a
    b_org, b_actor, _ = org_b

    batch_id = payout_svc.create_batch(
        db, org_id=a_org, actor_user_id=a_actor, title="A 지급",
    )
    item_id = payout_svc.add_item(
        db, org_id=a_org, actor_user_id=a_actor, batch_id=batch_id,
        item_type="EMPLOYEE_SALARY", payee_name="A 직원", due_amount=500_000,
    )
    payout_svc.approve_batch(db, org_id=a_org, batch_id=batch_id, actor_user_id=a_actor)

    with pytest.raises(NotFoundError):
        payout_svc.record_payment(
            db, org_id=b_org, actor_user_id=b_actor, item_id=item_id,
            payment_date=PERIOD_START, amount=100_000,
        )

    row = db.execute(text("SELECT paid_amount FROM payout_item WHERE id = :i"),
                     {"i": str(item_id)}).scalar()
    assert int(row) == 0


def test_reimbursement_candidates_are_org_scoped(db: Session, org_a, org_b):
    a_org, _, _ = org_a
    b_org, _, _ = org_b

    from app.tests.fixtures.factories import make_expense_category_policy

    # `ck_expenses_personal_card`: 개인카드 결제는 카드 소유자를 명시해야 한다.
    # 누가 쓴 카드인지 모르면 누구에게 상환할지도 알 수 없다.
    eid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO expenses (
          id, org_id, expense_date, category_id, description,
          gross_amount, supply_amount, vat_amount, payment_method,
          payment_card_holder_id, is_personal_card,
          is_settlement_deductible, is_business_use
        ) VALUES (
          :id, :o, :d, :c, 'A 조직 개인카드', 90000, 90000, 0,
          CAST('PERSONAL_CARD' AS enum_payment_method), :holder, TRUE, TRUE, TRUE
        )
    """), {"id": str(eid), "o": str(a_org), "d": PERIOD_START,
           "c": str(make_expense_category_policy(db, org_id=a_org)),
           "holder": str(make_partner(db, org_id=a_org, name="A 카드소유자"))})

    assert any(c["expense_id"] == eid
               for c in payout_svc.list_reimbursement_candidates(db, org_id=a_org))
    assert not any(c["expense_id"] == eid
                   for c in payout_svc.list_reimbursement_candidates(db, org_id=b_org))

    with pytest.raises(NotFoundError):
        payout_svc.create_reimbursement_batch(
            db, org_id=b_org, actor_user_id=make_app_user(db, org_id=b_org),
            expense_ids=[eid], title="남의 비용 상환 시도",
        )


def test_account_number_is_not_stored_anywhere_in_payout(db: Session, org_a):
    """계좌번호를 저장하지 않기로 했다. 저장할 컬럼 자체가 없어야 한다."""
    cols = db.execute(text("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name IN ('payout_item','payout_payment','payout_batch')
    """)).scalars().all()
    lowered = {c.lower() for c in cols}
    forbidden = {"account_no", "account_number", "bank_account", "iban", "card_no"}
    assert not (lowered & forbidden), f"계좌 관련 컬럼이 생겼다: {lowered & forbidden}"


# ──────────────────────────────────────────────────────────────────────────
# 운영 이슈 · worker
# ──────────────────────────────────────────────────────────────────────────

def test_issues_are_org_scoped(db: Session, org_a, org_b):
    a_org, a_actor, _ = org_a
    b_org, _, _ = org_b

    issues.scan(db, org_id=a_org, year_months=[YM])
    rows_a, total_a = issues.list_issues(db, org_id=a_org)
    assert total_a > 0

    _rows_b, total_b = issues.list_issues(db, org_id=b_org)
    assert total_b == 0

    issue_id = uuid.UUID(str(rows_a[0]["id"]))
    with pytest.raises(NotFoundError):
        issues.get_issue(db, org_id=b_org, issue_id=issue_id)
    with pytest.raises(NotFoundError):
        issues.update_issue(db, org_id=b_org, issue_id=issue_id,
                            actor_user_id=a_actor, status="RESOLVED")


def test_issue_summary_does_not_leak_other_org_counts(db: Session, org_a, org_b):
    a_org, _, _ = org_a
    b_org, _, _ = org_b
    issues.scan(db, org_id=a_org, year_months=[YM])

    assert issues.summary(db, org_id=a_org)["open_total"] > 0
    assert issues.summary(db, org_id=b_org)["open_total"] == 0


def test_worker_error_message_does_not_contain_stack_trace(db: Session, org_a):
    """관리 화면에 stack trace 가 노출되면 내부 경로와 SQL 이 샌다."""
    a_org, _, _ = org_a

    def _boom() -> rt.WorkerOutcome:
        raise RuntimeError(
            'relation "secret_table" does not exist\n'
            '  File "/app/services/secret.py", line 42, in leak\n'
            "    SELECT password_hash FROM app_user"
        )

    with pytest.raises(RuntimeError):
        rt.run_worker(db, worker_name="period_recalc", org_id=a_org, fn=_boom)

    run = rt.list_runs(db, worker_name="period_recalc", limit=1)[0]
    message = run["error_message"] or ""
    assert "File " not in message
    assert "password_hash" not in message
    assert "/app/services" not in message


def test_worker_health_exposes_no_secrets(db: Session, org_a):
    a_org, _, _ = org_a
    health = rt.worker_health(db, org_id=a_org)
    serialized = str(health).lower()
    for banned in ("password", "secret", "token", "database_url", "postgresql://"):
        assert banned not in serialized


def test_distributable_base_cannot_exceed_snapshot(db: Session, org_a):
    """DB 제약이 '없는 돈을 나누는' 배분을 막는다."""
    a_org, a_actor, ms_id = org_a
    confirm_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)
    snap = close_snapshot.get_current(db, org_id=a_org, year_month=YM)

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO distribution_run (
              org_id, year_month, monthly_settlement_id, close_snapshot_id,
              snapshot_generation, distributable_base, total_distributed, status
            ) VALUES (
              :o, :ym, :ms, :snap, 1, 100, 999999, 'CALCULATED'
            )
        """), {"o": str(a_org), "ym": YM, "ms": str(ms_id), "snap": str(snap["id"])})
    db.rollback()


def test_locked_month_snapshot_survives_second_confirm_attempt(db: Session, org_a):
    """확정된 달을 다시 확정하려 해도 snapshot 이 덮이지 않는다."""
    a_org, a_actor, _ = org_a
    confirm_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)
    first = close_snapshot.get_current(db, org_id=a_org, year_month=YM)

    with pytest.raises((ConflictError, Exception)):
        confirm_month(db, org_id=a_org, year_month=YM, actor_user_id=a_actor)
    db.rollback()

    again = close_snapshot.get_current(db, org_id=a_org, year_month=YM)
    assert again["id"] == first["id"]
    assert again["content_hash"] == first["content_hash"]
