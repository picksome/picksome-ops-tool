"""일일 리포트 자동 생성·전달 (M051).

이 파일이 고정하는 것
------------------
1. 하루에 한 번만 생성된다 — worker 가 두 번 돌아도
2. 실패한 실행은 "이미 했다" 로 세지 않는다
3. 날짜는 **KST** 기준이다
4. 만들기만 하지 않고 앱 알림까지 전달한다
5. 같은 날 알림이 두 번 가지 않는다
6. 받을 사람이 없으면 **성공으로 만들지 않는다**
7. 한 구획이 실패해도 리포트는 만들어진다
8. 리포트 숫자와 예외 센터 숫자가 같다

1·5·6 이 핵심이다
---------------
매일 아침 같은 알림이 두 개씩 오면 사용자는 벨을 아예 안 본다. 그리고
아무에게도 안 갔는데 "전달함" 이라고 하면 그 뒤로 아무도 못 믿는다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import automation as auto
from app.services import automation_runners as runners
from app.services import daily_report, exception_center
from app.tests.fixtures.factories import make_app_user, make_organization


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def policy(db: Session, org, actor) -> dict:
    return auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="DAILY_REPORT",
        enabled=True, mode="REPORT_ONLY", guardrails={})


def _run(db, org, actor, policy):
    return runners.run_policy(
        db, org_id=org, policy=policy, actor_user_id=actor, trigger="MANUAL")


def _actions(db, run_id) -> dict[str, dict]:
    rows = db.execute(text("""
        SELECT action_type, outcome, blocked_by, reason
          FROM automation_action WHERE run_id = :r
    """), {"r": run_id}).mappings().all()
    return {r["action_type"]: dict(r) for r in rows}


def _notifications(db, org) -> list[dict]:
    return [dict(r) for r in db.execute(text("""
        SELECT kind, title, dedupe_key, recipient_user_id
          FROM notification
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).mappings().all()]


# ── 생성 ───────────────────────────────────────────────────────


def test_report_is_generated_and_delivered(db: Session, org, actor, policy):
    """만들고 **알린다.** 만들기만 하면 사용자가 화면을 열어야 한다."""
    result = _run(db, org, actor, policy)

    acts = _actions(db, result.run_id)
    assert acts["DAILY_REPORT_BUILT"]["outcome"] == "REPORTED"
    assert acts["DAILY_REPORT_DELIVERED"]["outcome"] == "EXECUTED"

    notes = _notifications(db, org)
    assert len(notes) == 1
    assert notes[0]["kind"] == "DAILY_REPORT_READY"
    # dedupe 키가 날짜다.
    assert notes[0]["dedupe_key"] == f"daily_report:{date.today().isoformat()}"


def test_second_run_same_day_does_not_regenerate(db: Session, org, actor, policy):
    """worker 가 두 번 돌아도 하루에 하나."""
    _run(db, org, actor, policy)
    second = _run(db, org, actor, policy)

    acts = _actions(db, second.run_id)
    assert acts["DAILY_REPORT_BUILT"]["outcome"] == "BLOCKED"
    assert acts["DAILY_REPORT_BUILT"]["blocked_by"] == "ALREADY_GENERATED"

    # 알림도 하나뿐이다.
    assert len(_notifications(db, org)) == 1


def test_failed_run_does_not_count_as_done(db: Session, org, actor, policy):
    """실패한 실행 뒤에는 다시 만들 수 있어야 한다.

    아니면 정산 조회가 한 번 터진 날은 리포트가 영영 없다.
    """
    # 실패한 실행을 흉내 낸다 — BLOCKED 로 끝난 실행.
    run_id = db.execute(text("""
        INSERT INTO automation_run (org_id, policy_id, mode, status, trigger,
                                    started_at)
        VALUES (:o, :p, 'REPORT_ONLY', 'FAILED', 'SCHEDULED', now())
        RETURNING id
    """), {"o": str(org), "p": policy["id"]}).scalar_one()
    db.execute(text("""
        INSERT INTO automation_action (org_id, run_id, action_type, outcome)
        VALUES (:o, :r, 'DAILY_REPORT_BUILT', 'FAILED')
    """), {"o": str(org), "r": str(run_id)})
    db.commit()

    result = _run(db, org, actor, policy)
    acts = _actions(db, result.run_id)
    # 막히지 않고 실제로 만들어졌다.
    assert acts["DAILY_REPORT_BUILT"]["outcome"] == "REPORTED"


def test_report_date_is_kst(db: Session, org, actor, policy):
    """UTC 로 날짜를 세면 오전 9시 이전 실행이 전날 리포트를 만든다."""
    from app.core.timezone import now_local

    assert daily_report.report_date_now() == now_local().date()

    _run(db, org, actor, policy)
    notes = _notifications(db, org)
    assert notes[0]["dedupe_key"].endswith(now_local().date().isoformat())


# ── 전달 ───────────────────────────────────────────────────────


def test_no_recipient_is_not_reported_as_success(db: Session, actor):
    """받을 사람이 없으면 "전달함" 이라고 하지 않는다."""
    empty_org = make_organization(db)
    # 이 조직에는 ADMIN/OPERATOR/ACCOUNTING 이 아무도 없다.
    p = auto.upsert_policy(
        db, org_id=empty_org, actor_user_id=None,
        automation_type="DAILY_REPORT", enabled=True, mode="REPORT_ONLY",
        guardrails={})

    result = runners.run_policy(
        db, org_id=empty_org, policy=p, actor_user_id=None, trigger="MANUAL")

    acts = _actions(db, result.run_id)
    assert acts["DAILY_REPORT_DELIVERED"]["outcome"] == "BLOCKED"
    assert acts["DAILY_REPORT_DELIVERED"]["blocked_by"] == "NO_RECIPIENT"


def test_readonly_user_is_not_notified(db: Session, org, actor, policy):
    """읽기 전용 사용자에게는 안 보낸다 — 볼 수는 있어도 손댈 것이 없다."""
    reader = make_app_user(db, org_id=org, role="READONLY",
                           email="reader@example.com")
    _run(db, org, actor, policy)

    recipients = {str(n["recipient_user_id"]) for n in _notifications(db, org)}
    assert str(reader) not in recipients
    assert str(actor) in recipients


# ── 일관성 ─────────────────────────────────────────────────────


def test_report_numbers_match_exception_center(db: Session, org, actor, policy):
    """두 화면이 다른 숫자를 말하면 사용자는 둘 다 안 믿는다."""
    report = daily_report.build(db, org_id=org)
    center = exception_center.build(db, org_id=org)
    assert report["exception_summary"]["total"] == center["total"]


def test_partial_failure_still_produces_report(
    db: Session, org, actor, policy, monkeypatch,
):
    """한 구획이 터져도 리포트는 만들어진다."""
    def boom(*args, **kwargs):
        raise RuntimeError("정산 조회 실패")

    monkeypatch.setattr(daily_report, "_settlement", boom)

    result = _run(db, org, actor, policy)
    acts = _actions(db, result.run_id)
    assert acts["DAILY_REPORT_BUILT"]["outcome"] == "REPORTED"
    assert len(_notifications(db, org)) == 1
