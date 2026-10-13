"""운영 이슈 센터 + worker 관측 (Phase 35~38).

이 파일이 고정하는 것
------------------
1. 같은 문제를 매 스캔마다 새로 만들지 않는다 (dedupe)
2. **사람이 닫은 이슈를 스캐너가 다시 열지 않는다** — 이 규칙이 깨지면
   사용자는 두 번째 스캔부터 이슈 센터를 신뢰하지 않는다
3. 조건이 사라지면 `auto_resolve` 유형만 자동 종료된다
4. 수동 이슈는 스캐너가 건드리지 않는다
5. IGNORED 는 사유 없이 불가능하다
6. worker 실행 이력이 남고, 중단된 RUNNING 이 정리된다
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationError
from app.services import operation_issue as issues
from app.services import worker_runtime as rt
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)

YM = "2026-01"
PERIOD_START = date(2026, 1, 1)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _month(db: Session, org: uuid.UUID) -> uuid.UUID:
    ms_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO monthly_settlement
          (id, org_id, business_key, year_month, status, close_state,
           total_revenue, total_direct_cost, total_common_expense, net_profit)
        VALUES (:id, :o, :bk, :ym, 'DRAFT', 'OPEN', 0, 0, 0, 0)
    """), {"id": str(ms_id), "o": str(org), "bk": f"MS-{ms_id.hex[:10]}", "ym": YM})
    return ms_id


def _zero_cost_sale(db: Session, org: uuid.UUID) -> uuid.UUID:
    """마감 검증에서 BLOCKING 이 나오는 조건 하나."""
    sid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO sales_order
          (id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
           product_id, product_snapshot_name, quantity, sale_price, purchase_cost, line_no)
        VALUES (:id, :o, :bk, :d, 'COUPANG', :ono, :pid, '상품', 1, 10000, 0, 1)
    """), {"id": str(sid), "o": str(org), "bk": f"SO-{sid.hex[:10]}", "d": PERIOD_START,
           "ono": f"ORD-{sid.hex[:10]}",
           "pid": str(make_product(db, org_id=org, cost_amount=0))})
    return sid


# ──────────────────────────────────────────────────────────────────────────
# 스캐너
# ──────────────────────────────────────────────────────────────────────────

def test_scan_creates_issues_from_close_checks(db: Session, org: uuid.UUID):
    """마감 검증 카탈로그를 재사용한다. 검사 로직을 두 벌 만들지 않는다."""
    _month(db, org)
    _zero_cost_sale(db, org)

    r = issues.scan(db, org_id=org, year_months=[YM])
    assert r.created > 0

    rows, _ = issues.list_issues(db, org_id=org, open_only=True)
    codes = {i["issue_type"] for i in rows}
    assert "ZERO_COST_SALE" in codes
    zero = next(i for i in rows if i["issue_type"] == "ZERO_COST_SALE")
    assert zero["severity"] == "BLOCKING"
    assert zero["year_month"] == YM
    assert YM in zero["title"]


def test_scan_with_actor_does_not_crash(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """actor 를 넘긴 스캔이 500 나지 않는다.

    회귀: 스캔 요약을 `record_audit(domain_row_id=None)` 으로 남기려 했는데
    `audit_log.domain_row_id` 가 NOT NULL 이라 **API 경로에서만** 터졌다.
    서비스를 actor 없이 부르는 테스트만 있어서 못 잡았고 E2E 가 잡았다.
    """
    _month(db, org)
    _zero_cost_sale(db, org)

    r = issues.scan(db, org_id=org, actor_user_id=actor, year_months=[YM])
    assert r.created > 0

    rows, _ = issues.list_issues(db, org_id=org, open_only=True)
    assert rows


def test_rescan_refreshes_instead_of_duplicating(db: Session, org: uuid.UUID):
    """같은 문제로 이슈가 두 개 생기면 목록이 금방 쓰레기가 된다."""
    _month(db, org)
    _zero_cost_sale(db, org)

    first = issues.scan(db, org_id=org, year_months=[YM])
    second = issues.scan(db, org_id=org, year_months=[YM])

    assert first.created > 0
    assert second.created == 0
    assert second.refreshed == first.created

    rows, total = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    assert total == 1
    assert rows[0]["last_detected_at"] >= rows[0]["first_detected_at"]


def test_scanner_does_not_reopen_what_a_person_closed(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """**이 파일에서 가장 중요한 테스트.**

    조건이 여전히 참이라고 해서 사람이 무시한 이슈를 다시 열면,
    사용자는 두 번째 스캔부터 이슈 센터를 보지 않는다.
    """
    _month(db, org)
    _zero_cost_sale(db, org)
    issues.scan(db, org_id=org, year_months=[YM])

    rows, _ = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    issue_id = uuid.UUID(str(rows[0]["id"]))
    issues.update_issue(
        db, org_id=org, issue_id=issue_id, actor_user_id=actor,
        status="IGNORED", resolution_note="증정 건이라 원가 0원이 정상입니다",
    )

    # 조건은 그대로다 — 원가 0원 판매가 여전히 있다
    r = issues.scan(db, org_id=org, year_months=[YM])
    assert r.skipped_closed >= 1

    after = issues.get_issue(db, org_id=org, issue_id=issue_id)
    assert after["status"] == "IGNORED"
    assert after["resolution_note"] == "증정 건이라 원가 0원이 정상입니다"

    # 새 이슈를 만들지도 않았다
    _, total = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    assert total == 1


def test_auto_resolve_when_condition_disappears(db: Session, org: uuid.UUID):
    """조건이 사라지면 자동으로 닫힌다 — 데이터 조건이 명확한 유형만."""
    _month(db, org)
    sale_id = _zero_cost_sale(db, org)
    issues.scan(db, org_id=org, year_months=[YM])

    rows, _ = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    assert rows[0]["status"] == "OPEN"

    # 원가를 채웠다 — 조건이 사라진다
    db.execute(text("UPDATE sales_order SET purchase_cost = 3000 WHERE id = :i"),
               {"i": str(sale_id)})

    r = issues.scan(db, org_id=org, year_months=[YM])
    assert r.auto_resolved >= 1

    rows, _ = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    assert rows[0]["status"] == "RESOLVED"
    assert rows[0]["resolved_at"] is not None


def test_manual_issue_is_not_touched_by_scanner(
    db: Session, org: uuid.UUID, actor: uuid.UUID
):
    """사람이 만든 일을 기계가 닫으면 안 된다."""
    _month(db, org)
    manual = issues.create_manual_issue(
        db, org_id=org, actor_user_id=actor, issue_type="MANUAL_TODO",
        severity="WARNING", title="쿠팡 정산서 요청하기",
    )
    issues.scan(db, org_id=org, year_months=[YM])

    after = issues.get_issue(db, org_id=org, issue_id=uuid.UUID(str(manual["id"])))
    assert after["status"] == "OPEN"
    assert after["created_by_scanner"] is False
    assert after["auto_resolve"] is False


def test_reopened_issue_can_be_detected_again(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """잘못 닫은 이슈를 다시 열 수 있고, 그 뒤에는 스캔이 갱신한다."""
    _month(db, org)
    _zero_cost_sale(db, org)
    issues.scan(db, org_id=org, year_months=[YM])
    rows, _ = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    issue_id = uuid.UUID(str(rows[0]["id"]))

    issues.update_issue(db, org_id=org, issue_id=issue_id, actor_user_id=actor,
                        status="RESOLVED", resolution_note="처리함")
    issues.update_issue(db, org_id=org, issue_id=issue_id, actor_user_id=actor,
                        status="OPEN")

    after = issues.get_issue(db, org_id=org, issue_id=issue_id)
    assert after["status"] == "OPEN"
    # 다시 열었으므로 종료 정보가 지워져야 한다 — 남으면 "열려 있는데 해결됨" 이 된다
    assert after["resolved_at"] is None

    r = issues.scan(db, org_id=org, year_months=[YM])
    assert r.refreshed >= 1


# ──────────────────────────────────────────────────────────────────────────
# 상태 변경 규칙
# ──────────────────────────────────────────────────────────────────────────

def test_ignoring_requires_a_reason(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """사유 없이 무시한 이슈는 나중에 아무도 왜 무시했는지 설명하지 못한다."""
    _month(db, org)
    _zero_cost_sale(db, org)
    issues.scan(db, org_id=org, year_months=[YM])
    rows, _ = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")

    with pytest.raises(ValidationError) as exc:
        issues.update_issue(
            db, org_id=org, issue_id=uuid.UUID(str(rows[0]["id"])),
            actor_user_id=actor, status="IGNORED",
        )
    assert "사유" in str(exc.value)


def test_invalid_transition_is_rejected(db: Session, org: uuid.UUID, actor: uuid.UUID):
    _month(db, org)
    _zero_cost_sale(db, org)
    issues.scan(db, org_id=org, year_months=[YM])
    rows, _ = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    issue_id = uuid.UUID(str(rows[0]["id"]))

    issues.update_issue(db, org_id=org, issue_id=issue_id, actor_user_id=actor,
                        status="RESOLVED", resolution_note="처리함")
    with pytest.raises(ConflictError):
        # RESOLVED → IN_PROGRESS 는 허용하지 않는다. 다시 열고 나서 진행한다.
        issues.update_issue(db, org_id=org, issue_id=issue_id,
                            actor_user_id=actor, status="IN_PROGRESS")


def test_assignment_and_due_date(db: Session, org: uuid.UUID, actor: uuid.UUID):
    _month(db, org)
    _zero_cost_sale(db, org)
    issues.scan(db, org_id=org, year_months=[YM])
    rows, _ = issues.list_issues(db, org_id=org, issue_type="ZERO_COST_SALE")
    issue_id = uuid.UUID(str(rows[0]["id"]))

    assignee = make_app_user(db, org_id=org, role="OPERATOR")
    yesterday = date.today() - timedelta(days=1)
    updated = issues.update_issue(
        db, org_id=org, issue_id=issue_id, actor_user_id=actor,
        assigned_to=assignee, due_date=yesterday, status="IN_PROGRESS",
    )
    assert updated["assigned_to"] == assignee

    overdue, total = issues.list_issues(db, org_id=org, overdue_only=True)
    assert total == 1
    assert overdue[0]["is_overdue"] is True

    mine, total = issues.list_issues(db, org_id=org, assigned_to=assignee)
    assert total == 1


def test_summary_counts_by_severity(db: Session, org: uuid.UUID):
    _month(db, org)
    _zero_cost_sale(db, org)
    issues.scan(db, org_id=org, year_months=[YM])

    s = issues.summary(db, org_id=org)
    assert s["open_total"] > 0
    assert s["blocking"] >= 1
    assert s["unassigned"] == s["open_total"]
    assert any(t["issue_type"] == "ZERO_COST_SALE" for t in s["by_type"])


def test_locked_month_is_not_scanned(db: Session, org: uuid.UUID, actor: uuid.UUID):
    """고칠 수 없는 것을 이슈로 올리지 않는다."""
    _month(db, org)
    db.execute(text("""
        UPDATE monthly_settlement SET close_state = 'LOCKED', status = 'LOCKED',
               confirmed_at = now(), confirmed_by = :a, locked_at = now(), locked_by = :a
         WHERE org_id = :o AND year_month = :ym
    """), {"o": str(org), "ym": YM, "a": str(actor)})

    months = issues._recent_open_months(db, org_id=org)
    assert YM not in months


# ──────────────────────────────────────────────────────────────────────────
# worker 관측
# ──────────────────────────────────────────────────────────────────────────

def test_worker_run_records_success(db: Session, org: uuid.UUID):
    def _ok() -> rt.WorkerOutcome:
        return rt.WorkerOutcome(processed=5, success=4, failure=1,
                                summary={"note": "테스트"})

    rec = rt.run_worker(db, worker_name="operation_issue_scan", org_id=org, fn=_ok)
    assert rec.status == "SUCCESS"

    runs = rt.list_runs(db, worker_name="operation_issue_scan", limit=1)
    assert runs[0]["processed_count"] == 5
    assert runs[0]["success_count"] == 4
    assert runs[0]["failure_count"] == 1
    assert runs[0]["finished_at"] is not None


def test_worker_run_records_failure_without_stack_trace(db: Session, org: uuid.UUID):
    """관리 화면에 stack trace 가 노출되면 내부 경로와 SQL 이 새어나간다."""
    def _boom() -> rt.WorkerOutcome:
        raise RuntimeError("첫 줄 오류\n두 번째 줄은 저장되지 않아야 한다")

    with pytest.raises(RuntimeError):
        rt.run_worker(db, worker_name="due_approvals", org_id=org, fn=_boom)

    runs = rt.list_runs(db, worker_name="due_approvals", limit=1)
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["error_code"] == "RuntimeError"
    assert runs[0]["error_message"] == "첫 줄 오류"
    assert "두 번째 줄" not in (runs[0]["error_message"] or "")


def test_unknown_worker_name_is_rejected(db: Session, org: uuid.UUID):
    """오타를 '새 worker' 로 해석하면 관측이 조용히 갈라진다."""
    with pytest.raises(ValueError):
        rt.run_worker(db, worker_name="typo_worker", org_id=org,
                      fn=lambda: rt.WorkerOutcome())


def test_stale_running_run_is_reclaimed(db: Session, org: uuid.UUID):
    """프로세스가 죽으면 RUNNING 이 영구히 남아 화면이 거짓말을 한다."""
    db.execute(text("""
        INSERT INTO worker_run (org_id, worker_name, trigger, status, started_at)
        VALUES (:o, 'channel_publish', 'MANUAL', 'RUNNING', now() - INTERVAL '3 hours')
    """), {"o": str(org)})

    n = rt.reclaim_stale_runs(db, minutes=60)
    assert n >= 1

    runs = rt.list_runs(db, worker_name="channel_publish", limit=1)
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["error_code"] == "STALE_RUN"


def test_recent_running_run_is_not_reclaimed(db: Session, org: uuid.UUID):
    """정상 실행 중인 것을 죽었다고 판정하면 안 된다."""
    db.execute(text("""
        INSERT INTO worker_run (org_id, worker_name, trigger, status, started_at)
        VALUES (:o, 'period_recalc', 'MANUAL', 'RUNNING', now() - INTERVAL '1 minute')
    """), {"o": str(org)})

    rt.reclaim_stale_runs(db, minutes=60)
    runs = rt.list_runs(db, worker_name="period_recalc", limit=1)
    assert runs[0]["status"] == "RUNNING"


def test_worker_health_lists_every_worker_even_if_never_run(db: Session, org: uuid.UUID):
    """한 번도 안 돈 worker 를 목록에서 빼면 '설정을 안 했다' 를 알 수 없다."""
    health = rt.worker_health(db, org_id=org)
    names = {h["worker_name"] for h in health}
    assert names == set(rt.WORKER_NAMES)
    assert all("queue_depth" in h for h in health)


# ──────────────────────────────────────────────────────────────────────────
# worker 상태 판정 (Phase 52)
#
# **상태를 서버가 판단한다.** 화면이 "몇 시간 넘으면 오류" 같은 임계값을 각자
# 정하면 그 숫자의 근거를 아무도 모르고 화면마다 달라진다.
# ──────────────────────────────────────────────────────────────────────────

def _state_of(db: Session, org: uuid.UUID, worker: str) -> str:
    health = rt.worker_health(db, org_id=org)
    return next(h["state"] for h in health if h["worker_name"] == worker)


def test_never_run_worker_is_not_reported_as_normal(db: Session, org: uuid.UUID):
    """한 번도 안 돈 것과 정상은 다르다. 섞으면 스케줄러 미연결을 못 알아챈다."""
    assert _state_of(db, org, "channel_publish") == rt.STATE_NEVER_RAN


def test_successful_run_with_empty_queue_is_idle(db: Session, org: uuid.UUID):
    rt.run_worker(db, worker_name="channel_publish", org_id=org,
                  fn=lambda: rt.WorkerOutcome())
    assert _state_of(db, org, "channel_publish") == rt.STATE_IDLE


def test_last_run_failed_is_visible_even_after_earlier_success(
    db: Session, org: uuid.UUID
):
    """예전에 성공한 적 있다고 지금 실패를 덮으면 안 된다."""
    rt.run_worker(db, worker_name="due_approvals", org_id=org,
                  fn=lambda: rt.WorkerOutcome())

    def _boom() -> rt.WorkerOutcome:
        raise RuntimeError("실패")

    with pytest.raises(RuntimeError):
        rt.run_worker(db, worker_name="due_approvals", org_id=org, fn=_boom)

    assert _state_of(db, org, "due_approvals") == rt.STATE_LAST_RUN_FAILED


def test_stale_running_is_distinguished_from_running(db: Session, org: uuid.UUID):
    """오래 남은 RUNNING 은 '실행 중' 이 아니라 '중단 추정' 이다."""
    db.execute(text("""
        INSERT INTO worker_run (org_id, worker_name, trigger, status, started_at)
        VALUES (:o, 'period_recalc', 'MANUAL', 'RUNNING', now() - INTERVAL '3 hours')
    """), {"o": str(org)})
    assert _state_of(db, org, "period_recalc") == rt.STATE_STALE_RUNNING

    db.execute(text("DELETE FROM worker_run WHERE org_id = :o"), {"o": str(org)})
    db.execute(text("""
        INSERT INTO worker_run (org_id, worker_name, trigger, status, started_at)
        VALUES (:o, 'period_recalc', 'MANUAL', 'RUNNING', now() - INTERVAL '1 minute')
    """), {"o": str(org)})
    assert _state_of(db, org, "period_recalc") == rt.STATE_RUNNING


def test_queue_concept_is_marked_per_worker(db: Session, org: uuid.UUID):
    """이슈 스캔은 큐가 없다 — '대기 0건' 이 좋은 소식도 나쁜 소식도 아니다."""
    health = {h["worker_name"]: h for h in rt.worker_health(db, org_id=org)}
    assert health["operation_issue_scan"]["has_queue"] is False
    assert health["due_approvals"]["has_queue"] is True


def test_run_history_does_not_leak_other_orgs(db: Session, org: uuid.UUID):
    """다른 조직의 실행 기록(오류 메시지 포함)이 보이면 안 된다.

    `worker_health` 는 조직 범위를 걸고 있었는데 `list_runs` 만 빠져 있었다.
    """
    other = make_organization(db)
    rt.run_worker(db, worker_name="attachment_delete", org_id=other,
                  fn=lambda: rt.WorkerOutcome(processed=99))

    mine = rt.list_runs(db, org_id=org, worker_name="attachment_delete", limit=50)
    assert all(r["org_id"] != other for r in mine)
    assert not any(r["processed_count"] == 99 for r in mine)


def test_global_cron_runs_are_visible_to_every_org(db: Session, org: uuid.UUID):
    """org_id 가 없는 실행은 전체 조직을 도는 cron 이다 — 모든 관리자에게 의미가 있다."""
    rt.run_worker(db, worker_name="operation_issue_scan", org_id=None,
                  fn=lambda: rt.WorkerOutcome(processed=7))

    rows = rt.list_runs(db, org_id=org, worker_name="operation_issue_scan", limit=50)
    assert any(r["org_id"] is None and r["processed_count"] == 7 for r in rows)


def test_manual_run_endpoint_reuses_cli_adapters():
    """관리 화면 실행 버튼이 CLI 와 **같은 코드**를 부른다.

    화면용 구현을 따로 만들면 cron 이 하는 일과 버튼이 하는 일이 갈라지고,
    그 차이는 사고가 난 뒤에야 드러난다.
    """
    from app.scripts.run_workers import RUN_ORDER, WORKER_ADAPTERS

    assert set(WORKER_ADAPTERS) == set(rt.WORKER_NAMES)
    assert set(RUN_ORDER) == set(rt.WORKER_NAMES)
