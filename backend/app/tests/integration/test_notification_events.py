"""IN_APP 알림 (Phase 39).

이 파일이 고정하는 것
------------------
1. 같은 사건이 반복돼도 알림은 하나다 (`dedupe_key` + DB UNIQUE)
2. **알림 실패가 업무를 롤백시키지 않는다**
3. 사건마다 받는 사람이 다르다 — 반려는 요청자, worker 실패는 관리자
4. 조직 격리: 다른 조직 사용자에게 새지 않는다
5. 읽음 처리는 본인 것만
6. Slack/Discord 는 연결되지 않은 상태를 유지한다 (성공 mock 금지)
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import FeatureDisabledError
from app.core.notification import get_channel, get_mvp_active_channels
from app.services import notification as noti_svc
from app.services import notification_events as ev
from app.tests.fixtures.factories import make_app_user, make_organization


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def admin(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN", email=f"admin-{uuid.uuid4().hex[:8]}@t.com")


@pytest.fixture
def operator(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="OPERATOR", email=f"op-{uuid.uuid4().hex[:8]}@t.com")


def _rows(db: Session, org: uuid.UUID, *, kind: str | None = None) -> list[dict]:
    sql = """
        SELECT id, recipient_user_id, kind, title, dedupe_key, read_at, channel_code
          FROM notification
         WHERE org_id = :o AND deleted_at IS NULL
    """
    p: dict = {"o": str(org)}
    if kind:
        sql += " AND kind = CAST(:k AS enum_notification_kind)"
        p["k"] = kind
    return [dict(r) for r in db.execute(text(sql), p).mappings().all()]


# ──────────────────────────────────────────────────────────────────────────
# 중복 방지
# ──────────────────────────────────────────────────────────────────────────

def test_same_event_creates_only_one_notification(
    db: Session, org: uuid.UUID, admin: uuid.UUID
):
    """worker 가 5분마다 돌면서 같은 알림을 만들면 알림함이 못 쓰게 된다."""
    for _ in range(5):
        ev.month_close_blocked(
            db, org_id=org, year_month="2026-04", blocking_codes=["SALE_WITHOUT_PRODUCT"],
        )
    db.commit()

    rows = _rows(db, org, kind=ev.MONTH_CLOSE_BLOCKED)
    assert len(rows) == 1


def test_different_blocking_reason_is_a_new_notification(
    db: Session, org: uuid.UUID, admin: uuid.UUID
):
    """차단 사유가 바뀌면 새 소식이다. 같은 알림으로 묶으면 변화를 놓친다."""
    ev.month_close_blocked(db, org_id=org, year_month="2026-04",
                           blocking_codes=["SALE_WITHOUT_PRODUCT"])
    ev.month_close_blocked(db, org_id=org, year_month="2026-04",
                           blocking_codes=["SALE_WITHOUT_PRODUCT", "ZERO_COST_SALE"])
    db.commit()

    assert len(_rows(db, org, kind=ev.MONTH_CLOSE_BLOCKED)) == 2


def test_dedupe_is_per_recipient(db: Session, org: uuid.UUID, admin: uuid.UUID):
    """같은 사건을 관리자 2명에게 보내는 것은 중복이 아니다."""
    second = make_app_user(db, org_id=org, role="ADMIN",
                           email=f"admin2-{uuid.uuid4().hex[:8]}@t.com")
    result = ev.month_close_ready(db, org_id=org, year_month="2026-04")
    db.commit()

    assert result.created == 2
    recipients = {r["recipient_user_id"] for r in _rows(db, org, kind=ev.MONTH_CLOSE_READY)}
    assert recipients == {admin, second}


def test_dedupe_result_reports_the_truth(db: Session, org: uuid.UUID, admin: uuid.UUID):
    """조용히 성공한 척하지 않는다 — 무엇이 생기고 무엇이 걸러졌는지 돌려준다."""
    first = ev.month_close_ready(db, org_id=org, year_month="2026-04")
    second = ev.month_close_ready(db, org_id=org, year_month="2026-04")
    db.commit()

    assert first.created == 1 and first.deduped == 0
    assert second.created == 0 and second.deduped == 1


# ──────────────────────────────────────────────────────────────────────────
# 수신자
# ──────────────────────────────────────────────────────────────────────────

def test_rejection_goes_to_the_requester_not_the_approver(
    db: Session, org: uuid.UUID, admin: uuid.UUID, operator: uuid.UUID
):
    """반려는 요청자가 알아야 한다. 승인자에게 보내면 자기가 누른 버튼을 다시 알려준다."""
    approval_id = uuid.uuid4()
    ev.approval_rejected(
        db, org_id=org, approval_id=approval_id, business_key="AR-1",
        requester_user_id=operator, reason="근거 부족",
    )
    db.commit()

    rows = _rows(db, org, kind=ev.APPROVAL_REJECTED)
    assert len(rows) == 1
    assert rows[0]["recipient_user_id"] == operator


def test_worker_failure_goes_to_admins_only(
    db: Session, org: uuid.UUID, admin: uuid.UUID, operator: uuid.UUID
):
    ev.worker_failure(
        db, org_id=org, worker_name="due_approvals", run_id=uuid.uuid4(),
        error_code="RuntimeError",
    )
    db.commit()

    rows = _rows(db, org, kind=ev.WORKER_FAILURE)
    assert {r["recipient_user_id"] for r in rows} == {admin}


def test_apply_failure_reaches_admin_and_requester(
    db: Session, org: uuid.UUID, admin: uuid.UUID, operator: uuid.UUID
):
    """가장 위험한 상태다 — 승인자도 요청자도 반영됐다고 믿는데 데이터는 그대로다."""
    ev.approval_apply_failed(
        db, org_id=org, approval_id=uuid.uuid4(), business_key="AR-2",
        error_code="ValidationError", requester_user_id=operator,
    )
    db.commit()

    rows = _rows(db, org, kind=ev.APPROVAL_APPLY_FAILED)
    assert {r["recipient_user_id"] for r in rows} == {admin, operator}


# ──────────────────────────────────────────────────────────────────────────
# 실패해도 업무를 막지 않는다
# ──────────────────────────────────────────────────────────────────────────

def test_unknown_kind_does_not_raise(db: Session, org: uuid.UUID, admin: uuid.UUID):
    """오타 하나로 승인이 롤백되면 알림이 업무의 장애 지점이 된다."""
    result = ev.emit(
        db, org_id=org, kind="NOT_A_REAL_EVENT", title="x", dedupe_key="k",
    )
    assert result.failed == 1
    assert result.created == 0


def test_emit_with_no_recipients_is_not_an_error(db: Session, org: uuid.UUID):
    """받을 사람이 없는 조직에서도 업무는 계속돼야 한다."""
    result = ev.month_close_ready(db, org_id=org, year_month="2026-04")
    assert result.recipients == 0
    assert result.created == 0
    assert result.failed == 0


def test_emit_failure_does_not_poison_the_outer_transaction(
    db: Session, org: uuid.UUID, admin: uuid.UUID
):
    """알림이 실패해도 바깥 트랜잭션은 계속 쓸 수 있어야 한다.

    SAVEPOINT 없이 오류가 나면 트랜잭션 전체가 무효화되고, 그 뒤의 모든 쿼리가
    `InFailedSqlTransaction` 으로 터진다 — 승인이 알림 때문에 취소된다.
    """
    # 제목 길이 제한을 넘겨도(잘라서 넣는다) 그 뒤 쿼리가 살아 있어야 한다.
    ev.month_close_ready(db, org_id=org, year_month="2026-04")
    ev.emit(db, org_id=org, kind="NOT_A_REAL_EVENT", title="x" * 5000, dedupe_key="k2")
    db.commit()

    # 바깥 트랜잭션이 멀쩡하다.
    assert db.execute(text("SELECT 1")).scalar_one() == 1
    assert len(_rows(db, org, kind=ev.MONTH_CLOSE_READY)) == 1


# ──────────────────────────────────────────────────────────────────────────
# 격리 / 읽음
# ──────────────────────────────────────────────────────────────────────────

def test_notifications_do_not_leak_across_orgs(db: Session, org: uuid.UUID, admin: uuid.UUID):
    other = make_organization(db)
    other_admin = make_app_user(db, org_id=other, role="ADMIN",
                                email=f"o-{uuid.uuid4().hex[:8]}@t.com")
    ev.worker_failure(db, org_id=other, worker_name="due_approvals",
                      run_id=uuid.uuid4(), error_code="X")
    db.commit()

    mine, total = noti_svc.list_my_notifications(db, user_id=admin)
    assert total == 0
    theirs, other_total = noti_svc.list_my_notifications(db, user_id=other_admin)
    assert other_total == 1


def test_mark_read_only_affects_my_notifications(
    db: Session, org: uuid.UUID, admin: uuid.UUID, operator: uuid.UUID
):
    ev.approval_rejected(db, org_id=org, approval_id=uuid.uuid4(), business_key="AR-3",
                         requester_user_id=operator, reason="x")
    db.commit()

    row = _rows(db, org, kind=ev.APPROVAL_REJECTED)[0]
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        noti_svc.mark_read(db, user_id=admin, notification_id=row["id"])

    assert noti_svc.unread_count(db, user_id=operator) == 1
    noti_svc.mark_read(db, user_id=operator, notification_id=row["id"])
    assert noti_svc.unread_count(db, user_id=operator) == 0


def test_mark_all_read_is_scoped_to_the_user(
    db: Session, org: uuid.UUID, admin: uuid.UUID, operator: uuid.UUID
):
    ev.month_close_ready(db, org_id=org, year_month="2026-04")       # → admin
    ev.approval_rejected(db, org_id=org, approval_id=uuid.uuid4(),   # → operator
                         business_key="AR-4", requester_user_id=operator, reason="x")
    db.commit()

    noti_svc.mark_all_read(db, user_id=admin)
    assert noti_svc.unread_count(db, user_id=admin) == 0
    assert noti_svc.unread_count(db, user_id=operator) == 1


# ──────────────────────────────────────────────────────────────────────────
# 채널 — 성공 mock 금지
# ──────────────────────────────────────────────────────────────────────────

def test_slack_and_discord_stay_disconnected():
    """webhook 이 없으므로 연결할 수 없다. 성공한 척하지 않는다."""
    active = {a.code for a in get_mvp_active_channels()}
    assert active == {"IN_APP", "EMAIL"}

    with pytest.raises(FeatureDisabledError):
        get_channel("SLACK")


def test_in_app_notifications_are_only_in_app_channel(
    db: Session, org: uuid.UUID, admin: uuid.UUID
):
    """기본 채널은 IN_APP 뿐이다.

    모든 사건마다 EMAIL 을 함께 만들면 SMTP 가 없는 환경에서 QUEUED 행이 쌓이고,
    "발송 대기" 가 정상 상태가 되어 진짜 문제를 가린다.
    """
    ev.month_close_ready(db, org_id=org, year_month="2026-04")
    db.commit()

    assert {r["channel_code"] for r in _rows(db, org)} == {"IN_APP"}
