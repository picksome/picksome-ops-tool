"""월 재오픈 승인 워크플로 (Phase 29).

이 파일이 고정하는 것
------------------
1. 잠긴 달은 **승인을 거쳐야** 열린다
2. 사유 없는 재오픈은 만들 수 없다
3. 재오픈 승인만 월 잠금 가드를 면제받는다 — 다른 승인은 여전히 막힌다
4. 열린 달에는 재오픈 요청 자체가 만들어지지 않는다
5. 재오픈 한도(D-285)를 요청 생성 시점에도 본다
6. 승인 이력에 누가 요청·승인했고 어느 snapshot 이 무효화됐는지 남는다
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationError
from app.services import approval as approval_svc
from app.services import approval_targets as targets
from app.services import close_snapshot
from app.services import monthly_close as mc
from app.tests.fixtures.factories import (
    confirm_month,
    lock_month,
    make_app_user,
    make_organization,
    make_product,
)

YM = "2026-03"
PERIOD_START = date(2026, 3, 1)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def admin(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def requester(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ACCOUNTING")


def _locked_month(db: Session, org: uuid.UUID, actor: uuid.UUID) -> uuid.UUID:
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
        VALUES (:id, :o, :bk, :d, 'COUPANG', :ono, :pid, '상품', 1, 10000, 3000, 1)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}", "d": PERIOD_START,
           "ono": f"ORD-{sid.hex[:10]}", "pid": str(make_product(db, org_id=org))})
    db.execute(text("""
        INSERT INTO product_margin_actual
          (id, org_id, sales_order_id, actual_revenue, actual_cost,
           actual_marketplace_fee, actual_payment_fee, actual_ad_cost,
           actual_fulfillment_fee, actual_return_cost, actual_vat,
           actual_shipping_cost, actual_margin_amount, actual_margin_rate)
        VALUES (:id, :o, :so, 10000, 3000, 0, 0, 0, 0, 0, 0, 0, 7000, 0)
    """), {"id": str(uuid.uuid4()), "o": str(org), "so": str(sid)})

    confirm_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    lock_month(db, org_id=org, year_month=YM, actor_user_id=actor)
    return ms_id


# ──────────────────────────────────────────────────────────────────────────

def test_reopen_goes_through_approval_and_actually_opens_the_month(
    db: Session, org: uuid.UUID, admin: uuid.UUID, requester: uuid.UUID
):
    _locked_month(db, org, admin)

    req = mc.request_reopen_approval(
        db, org_id=org, year_month=YM, actor_user_id=requester,
        reason="정산서가 늦게 도착해 재확정 필요",
    )
    assert req["status"] == "PENDING"
    assert req["target_type"] == "monthly_close"
    assert req["request_type"] == "MONTH_REOPEN"
    # 귀속월은 그 달의 1일이어야 한다 — 오늘로 잡으면 승인 목록에서 달이 섞인다
    assert req["effective_from"] == PERIOD_START

    approval_svc.approve(db, org_id=org, request_id=req["id"],
                         actor_user_id=admin, actor_role="ADMIN")
    applied = approval_svc.apply(db, org_id=org, request_id=req["id"],
                                 actor_user_id=admin, actor_role="ADMIN")
    assert applied["status"] == "APPLIED"

    month = mc.get_month(db, org_id=org, year_month=YM)
    assert month["close_state"] == "OPEN"
    assert month["reopen_count"] == 1
    # 확정 숫자는 지워지지 않고 superseded 로 남는다
    assert month["snapshot"] is None
    history = close_snapshot.list_generations(db, org_id=org, year_month=YM)
    assert len(history) == 1 and history[0]["superseded_at"] is not None


def test_reason_is_required(db: Session, org: uuid.UUID, admin: uuid.UUID):
    _locked_month(db, org, admin)
    with pytest.raises(ValidationError) as exc:
        mc.request_reopen_approval(
            db, org_id=org, year_month=YM, actor_user_id=admin, reason="짧음",
        )
    assert "사유" in str(exc.value)


def test_open_month_cannot_be_reopened(db: Session, org: uuid.UUID, admin: uuid.UUID):
    """열린 달을 '재오픈' 하는 것은 의미가 없다. 절차만 늘어난다."""
    ms_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO monthly_settlement
          (id, org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense, net_profit)
        VALUES (:id, :o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0)
    """), {"id": str(ms_id), "o": str(org), "bk": f"MS-{ms_id.hex[:10]}", "ym": YM})

    with pytest.raises(ValidationError) as exc:
        mc.request_reopen_approval(
            db, org_id=org, year_month=YM, actor_user_id=admin,
            reason="열린 달인데 재오픈 시도",
        )
    assert "잠긴 달이 아닙니다" in str(exc.value)


def test_reopen_limit_is_checked_at_request_time(
    db: Session, org: uuid.UUID, admin: uuid.UUID
):
    """한도를 넘긴 달의 요청을 만들어 두면 승인자가 결재한 뒤에야 실패한다."""
    _locked_month(db, org, admin)
    db.execute(text("""
        UPDATE monthly_settlement SET reopen_count = 3
         WHERE org_id = :o AND year_month = :ym
    """), {"o": str(org), "ym": YM})

    with pytest.raises(ConflictError) as exc:
        mc.request_reopen_approval(
            db, org_id=org, year_month=YM, actor_user_id=admin,
            reason="한도 초과 상태에서 요청",
        )
    assert "한도" in str(exc.value)


def test_only_admin_can_approve_reopen(
    db: Session, org: uuid.UUID, admin: uuid.UUID, requester: uuid.UUID
):
    """재오픈은 확정 숫자를 무효화한다. ACCOUNTING 도 단독으로 결정하지 않는다."""
    from app.core.exceptions import ApprovalPermissionDeniedError

    _locked_month(db, org, admin)
    req = mc.request_reopen_approval(
        db, org_id=org, year_month=YM, actor_user_id=requester,
        reason="권한 확인용 요청",
    )
    with pytest.raises(ApprovalPermissionDeniedError):
        approval_svc.approve(db, org_id=org, request_id=req["id"],
                             actor_user_id=requester, actor_role="ACCOUNTING")


def test_reopen_snapshot_shows_what_is_being_invalidated(
    db: Session, org: uuid.UUID, admin: uuid.UUID, requester: uuid.UUID
):
    """승인자가 '무엇을 무효화하는지' 를 보고 판단할 수 있어야 한다."""
    _locked_month(db, org, admin)
    req = mc.request_reopen_approval(
        db, org_id=org, year_month=YM, actor_user_id=requester,
        reason="스냅샷 내용 확인",
    )
    before = req["before_snapshot_json"]
    assert before["year_month"] == YM
    assert before["close_state"] == "LOCKED"
    assert before["close_generation"] == 1
    assert "confirmed_net_profit" in before


def test_only_month_reopen_is_exempt_from_month_lock(
    db: Session, org: uuid.UUID, admin: uuid.UUID
):
    """면제가 늘어나면 승인이 월 잠금을 우회하는 일반 통로가 된다.

    이 목록이 바뀌면 import 시점에 터진다 — 여기서는 그 사실을 명시적으로 고정한다.
    """
    exempt = sorted(
        t for t in targets.supported_target_types()
        if getattr(targets.get_adapter(t), "month_lock_exempt", False)
    )
    assert exempt == ["monthly_close"]


def test_other_approvals_still_blocked_in_locked_month(
    db: Session, org: uuid.UUID, admin: uuid.UUID
):
    """재오픈 면제가 다른 승인까지 열어 주지 않는다."""
    from app.core.exceptions import SettlementLockedError

    _locked_month(db, org, admin)
    requester_admin = make_app_user(db, org_id=org, role="ADMIN")
    pid = make_product(db, org_id=org, cost_amount=10_000)
    req = approval_svc.create_request(
        db, org_id=org, actor_user_id=requester_admin, target_type="product", target_id=pid,
        request_type="COST_CHANGE", proposed_changes={"cost_amount": 12_000},
        effective_from=PERIOD_START, submit=True,
    )
    approval_svc.approve(db, org_id=org, request_id=req["id"],
                         actor_user_id=admin, actor_role="ADMIN")

    with pytest.raises(SettlementLockedError):
        approval_svc.apply(db, org_id=org, request_id=req["id"],
                           actor_user_id=admin, actor_role="ADMIN")


def test_reopen_audit_records_the_approval_request(
    db: Session, org: uuid.UUID, admin: uuid.UUID, requester: uuid.UUID
):
    """누가 요청하고 누가 승인했는지, 어느 snapshot 이 무효화됐는지 남아야 한다."""
    _locked_month(db, org, admin)
    req = mc.request_reopen_approval(
        db, org_id=org, year_month=YM, actor_user_id=requester,
        reason="감사 기록 확인용",
    )
    approval_svc.approve(db, org_id=org, request_id=req["id"],
                         actor_user_id=admin, actor_role="ADMIN")
    approval_svc.apply(db, org_id=org, request_id=req["id"],
                       actor_user_id=admin, actor_role="ADMIN")

    row = db.execute(text("""
        SELECT sra.reason, sra.reopened_by, sra.snapshot_before_json
          FROM settlement_reopen_audit sra
          JOIN monthly_settlement ms ON ms.id = sra.monthly_settlement_id
         WHERE ms.org_id = :o AND ms.year_month = :ym
    """), {"o": str(org), "ym": YM}).mappings().one()
    assert row["reason"] == "감사 기록 확인용"
    assert row["snapshot_before_json"]["approval_request_id"] == str(req["id"])
    assert row["snapshot_before_json"]["superseded_snapshot_id"] is not None
