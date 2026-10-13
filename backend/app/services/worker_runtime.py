"""worker 실행·관측 기반 (Phase 37~38).

무엇을 공통화하고 무엇을 하지 않는가
--------------------------------
**업무 로직은 합치지 않는다.** 승인 적용·첨부 삭제·가격 반영·이슈 스캔은
실패 성격도, 재시도 정책도, 잠금 범위도 다르다. 하나의 generic job 처리기로
묶으면 각자의 판단이 뭉개진다.

공통화하는 것은 **실행과 관측**뿐이다.

```
worker_runtime   언제 돌았나 · 몇 건 처리했나 · 실패했나 · 지금 돌고 있나
각 worker        무엇을 어떻게 처리하나
```

프로세스가 죽으면
--------------
`worker_run` 이 `RUNNING` 으로 남는다. 그 상태가 영구히 남아 "지금 실행 중"
으로 보이면 다음 실행이 막히거나 관리 화면이 거짓말을 한다.
그래서 `reclaim_stale_runs()` 가 오래된 RUNNING 을 `FAILED` 로 정리한다.

**advisory lock 은 각 worker 가 이미 갖고 있다.** 여기서 또 걸지 않는다 —
잠금 범위(조직 단위 / 전역)가 worker 마다 달라서 공통화하면 틀린다.

실패가 다음 worker 를 막지 않는다
-----------------------------
전체 runner 는 각 worker 를 독립적으로 실행한다. 하나가 터져도 나머지는 돈다.
그 사실을 종료 코드와 요약에 반영한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# RUNNING 으로 이 시간을 넘긴 run 은 프로세스가 죽은 것으로 본다.
# 가장 오래 걸리는 worker(가격 반영 배치)의 몇 배로 잡았다.
STALE_RUN_MINUTES = 60

WORKER_NAMES = (
    "due_approvals",
    "attachment_delete",
    "channel_publish",
    "period_recalc",
    "operation_issue_scan",
    # 자동화 정책 실행 (M045). 정책 하나하나의 결과는 `automation_run` 이
    # 갖고, 여기는 "그 배치가 언제 돌았나" 만 기록한다 — 다섯 정책의 결과를
    # 한 run 에 뭉개면 무엇이 실패했는지 알 수 없다.
    "automation_scan",
)


@dataclass(slots=True)
class WorkerOutcome:
    """worker 한 번 실행의 결과."""

    processed: int = 0
    success: int = 0
    failure: int = 0
    skipped: int = 0
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunRecord:
    worker_name: str
    run_id: UUID
    status: str
    duration_ms: int
    outcome: WorkerOutcome
    error_code: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker_name,
            "run_id": str(self.run_id),
            "status": self.status,
            "duration_ms": self.duration_ms,
            "processed": self.outcome.processed,
            "success": self.outcome.success,
            "failure": self.outcome.failure,
            "skipped": self.outcome.skipped,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "summary": self.outcome.summary,
        }


def short_error(exc: BaseException) -> tuple[str, str]:
    """오류 코드와 **짧은** 메시지.

    stack trace 를 저장하지 않는다. 관리 화면에 그대로 노출되면 내부 파일 경로와
    SQL 이 새어나간다. 상세는 애플리케이션 로그에 남는다.
    """
    code = type(exc).__name__
    message = str(exc).splitlines()[0] if str(exc) else code
    return code, message[:500]


def run_worker(
    db: Session, *, worker_name: str, fn: Callable[[], WorkerOutcome],
    org_id: UUID | None = None, trigger: str = "MANUAL",
) -> RunRecord:
    """worker 하나를 실행하고 이력을 남긴다.

    **예외를 삼키지 않는다.** 실패는 `FAILED` 로 기록하고 그대로 올린다 —
    호출자(전체 runner 또는 API)가 격리 여부를 결정한다.
    """
    if worker_name not in WORKER_NAMES:
        raise ValueError(
            f"등록되지 않은 worker 입니다: {worker_name}. "
            f"허용값: {sorted(WORKER_NAMES)}"
        )

    started = time.monotonic()
    run_id = db.execute(text("""
        INSERT INTO worker_run (org_id, worker_name, trigger, status)
        VALUES (:org, :name, :trg, 'RUNNING')
        RETURNING id
    """), {"org": str(org_id) if org_id else None, "name": worker_name,
           "trg": trigger}).scalar_one()
    db.commit()

    try:
        outcome = fn()
    except Exception as exc:
        db.rollback()
        code, message = short_error(exc)
        elapsed = int((time.monotonic() - started) * 1000)
        db.execute(text("""
            UPDATE worker_run
               SET status = 'FAILED', finished_at = now(), duration_ms = :ms,
                   error_code = :code, error_message = :msg
             WHERE id = :id
        """), {"id": str(run_id), "ms": elapsed, "code": code, "msg": message})
        db.commit()

        # 관리자에게 알린다. **알림 실패가 예외를 가리면 안 된다** —
        # 원래 오류를 그대로 올려야 호출자가 격리 여부를 판단할 수 있다.
        if org_id is not None:
            from app.services import notification_events

            notification_events.worker_failure(
                db, org_id=org_id, worker_name=worker_name,
                run_id=UUID(str(run_id)), error_code=code,
            )
            db.commit()
        raise

    elapsed = int((time.monotonic() - started) * 1000)
    db.execute(text("""
        UPDATE worker_run
           SET status = 'SUCCESS', finished_at = now(), duration_ms = :ms,
               processed_count = :p, success_count = :s,
               failure_count = :f, skipped_count = :k,
               summary_json = CAST(:summary AS JSONB)
         WHERE id = :id
    """), {
        "id": str(run_id), "ms": elapsed,
        "p": outcome.processed, "s": outcome.success,
        "f": outcome.failure, "k": outcome.skipped,
        "summary": _json(outcome.summary),
    })
    db.commit()
    return RunRecord(
        worker_name=worker_name, run_id=UUID(str(run_id)), status="SUCCESS",
        duration_ms=elapsed, outcome=outcome,
    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def reclaim_stale_runs(db: Session, *, minutes: int = STALE_RUN_MINUTES) -> int:
    """죽은 프로세스가 남긴 RUNNING 을 정리한다.

    영구 RUNNING 이 남으면 관리 화면이 "지금 실행 중" 이라고 거짓말을 하고,
    "마지막 성공이 언제인가" 를 물어도 답이 이상해진다.
    """
    rows = db.execute(text("""
        UPDATE worker_run
           SET status = 'FAILED', finished_at = now(),
               duration_ms = CAST(EXTRACT(EPOCH FROM (now() - started_at)) * 1000 AS INTEGER),
               error_code = 'STALE_RUN',
               error_message = '실행 기록이 완료되지 않았습니다(프로세스 중단 추정).'
         WHERE status = 'RUNNING'
           AND started_at < now() - make_interval(mins => :m)
        RETURNING id
    """), {"m": minutes}).scalars().all()
    db.commit()
    return len(rows)


# ──────────────────────────────────────────────────────────────────────────
# 관측
# ──────────────────────────────────────────────────────────────────────────

# worker 상태 코드.
#
# **화면이 임계값을 만들지 않게 하려고 서버가 판단한다.** 프론트엔드가
# "3시간 넘으면 빨간색" 같은 규칙을 각자 정하면, 그 숫자의 근거를 아무도 모르고
# 화면마다 달라진다. 여기 값은 전부 **객관적 사실**이거나 명시된 상수 기준이다.
STATE_NEVER_RAN = "NEVER_RAN"        # 한 번도 실행된 적 없음
STATE_RUNNING = "RUNNING"            # 지금 실행 중인 기록이 있음
STATE_STALE_RUNNING = "STALE_RUNNING"  # RUNNING 인데 STALE_RUN_MINUTES 초과 — 프로세스 중단 추정
STATE_LAST_RUN_FAILED = "LAST_RUN_FAILED"  # 마지막 실행이 실패
STATE_PENDING_QUEUE = "PENDING_QUEUE"      # 성공했지만 처리할 대기 건이 남아 있음
STATE_IDLE = "IDLE"                  # 마지막 실행 성공 + 대기 없음


def derive_state(
    *, never_ran: bool, running_now: int, has_stale_running: bool,
    last_started_at: Any, last_success_at: Any, last_failure_at: Any,
    queue_depth: int,
) -> str:
    """관측 사실 → 상태 코드. 시간 임계값은 `STALE_RUN_MINUTES` 하나뿐이다."""
    if never_ran:
        return STATE_NEVER_RAN
    if has_stale_running:
        return STATE_STALE_RUNNING
    if running_now > 0:
        return STATE_RUNNING
    # 마지막 실행이 실패였는가 — 시작 시각으로 비교한다. 성공 기록이 아예 없는데
    # 실패만 있으면 그것도 "마지막 실행 실패" 다.
    if last_failure_at is not None and (
        last_success_at is None or last_failure_at >= last_success_at
    ):
        return STATE_LAST_RUN_FAILED
    if queue_depth > 0:
        return STATE_PENDING_QUEUE
    return STATE_IDLE


def worker_health(db: Session, *, org_id: UUID | None = None) -> list[dict[str, Any]]:
    """worker 별 최근 상태 + 대기 깊이.

    **비밀정보와 stack trace 를 노출하지 않는다.** 오류 코드와 한 줄 메시지만.

    `state` 는 서버가 판단해서 준다 — 화면이 임계값을 지어내지 않게 하기 위해서다.
    """
    rows = db.execute(text("""
        SELECT w.worker_name,
               MAX(w.started_at) AS last_started_at,
               MAX(w.started_at) FILTER (WHERE w.status = 'SUCCESS') AS last_success_at,
               MAX(w.started_at) FILTER (WHERE w.status = 'FAILED')  AS last_failure_at,
               COUNT(*) FILTER (WHERE w.status = 'RUNNING') AS running_now,
               COUNT(*) FILTER (
                 WHERE w.status = 'RUNNING'
                   AND w.started_at < now() - make_interval(mins => :stale)
               ) AS stale_running,
               SUM(w.processed_count) AS processed_total,
               SUM(w.success_count)   AS success_total,
               SUM(w.failure_count)   AS failure_total
          FROM worker_run w
         WHERE (CAST(:org AS UUID) IS NULL
                OR w.org_id = CAST(:org AS UUID) OR w.org_id IS NULL)
         GROUP BY w.worker_name
    """), {"org": str(org_id) if org_id else None,
           "stale": STALE_RUN_MINUTES}).mappings().all()
    by_name = {r["worker_name"]: dict(r) for r in rows}

    queues = queue_depths(db, org_id=org_id)

    out = []
    for name in WORKER_NAMES:
        base = by_name.get(name, {
            "worker_name": name, "last_started_at": None, "last_success_at": None,
            "last_failure_at": None, "running_now": 0, "stale_running": 0,
            "processed_total": 0, "success_total": 0, "failure_total": 0,
        })
        last_error = db.execute(text("""
            SELECT error_code, error_message, started_at FROM worker_run
             WHERE worker_name = :n AND status = 'FAILED'
             ORDER BY started_at DESC LIMIT 1
        """), {"n": name}).mappings().first()
        queue_depth = queues.get(name, {}).get("depth", 0)
        never_ran = base["last_started_at"] is None
        out.append({
            **base,
            "last_error_code": last_error["error_code"] if last_error else None,
            "last_error_message": last_error["error_message"] if last_error else None,
            "queue_depth": queue_depth,
            "oldest_pending_age_seconds": queues.get(name, {}).get("oldest_age_seconds"),
            "never_ran": never_ran,
            "has_queue": name in _WORKERS_WITH_QUEUE,
            "state": derive_state(
                never_ran=never_ran,
                running_now=int(base.get("running_now") or 0),
                has_stale_running=int(base.get("stale_running") or 0) > 0,
                last_started_at=base["last_started_at"],
                last_success_at=base["last_success_at"],
                last_failure_at=base["last_failure_at"],
                queue_depth=queue_depth,
            ),
        })
    return out


# 대기 큐 개념이 있는 worker. 이슈 스캔은 주기 실행이라 "대기 0건" 이
# 좋은 소식도 나쁜 소식도 아니다 — 화면이 그걸 구분할 수 있어야 한다.
_WORKERS_WITH_QUEUE = frozenset({
    "due_approvals", "attachment_delete", "channel_publish", "period_recalc",
})


def queue_depths(db: Session, *, org_id: UUID | None = None) -> dict[str, dict[str, Any]]:
    """worker 별 대기 건수와 가장 오래된 대기 건의 나이.

    각 worker 의 큐가 서로 다른 테이블이라 쿼리를 따로 쓴다 —
    공통 job 테이블을 만들지 않기로 한 결과다.
    """
    p = {"org": str(org_id) if org_id else None}

    def one(sql: str) -> dict[str, Any]:
        row = db.execute(text(sql), p).mappings().first()
        if row is None:
            return {"depth": 0, "oldest_age_seconds": None}
        return {
            "depth": int(row["depth"] or 0),
            "oldest_age_seconds": (
                int(row["oldest_age"]) if row["oldest_age"] is not None else None
            ),
        }

    return {
        "due_approvals": one("""
            SELECT COUNT(*) AS depth,
                   EXTRACT(EPOCH FROM (now() - MIN(reviewed_at))) AS oldest_age
              FROM approval_request
             WHERE deleted_at IS NULL AND status = 'APPROVED'
               AND COALESCE(auto_apply_status, 'PENDING') IN ('PENDING','RETRYING')
               AND (CAST(:org AS UUID) IS NULL OR org_id = CAST(:org AS UUID))
        """),
        "attachment_delete": one("""
            SELECT COUNT(*) AS depth,
                   EXTRACT(EPOCH FROM (now() - MIN(updated_at))) AS oldest_age
              FROM expense_attachment
             WHERE deleted_at IS NULL AND status = 'PENDING_DELETE'
               AND (CAST(:org AS UUID) IS NULL OR org_id = CAST(:org AS UUID))
        """),
        "channel_publish": one("""
            SELECT COUNT(*) AS depth,
                   EXTRACT(EPOCH FROM (now() - MIN(created_at))) AS oldest_age
              FROM channel_price_publish_outbox
             WHERE status IN ('QUEUED','RETRY_PENDING')
               AND (CAST(:org AS UUID) IS NULL OR org_id = CAST(:org AS UUID))
        """),
        "period_recalc": one("""
            SELECT COUNT(*) AS depth,
                   EXTRACT(EPOCH FROM (now() - MIN(marked_at))) AS oldest_age
              FROM period_recalc_flag
             WHERE resolved_at IS NULL
               AND (CAST(:org AS UUID) IS NULL OR org_id = CAST(:org AS UUID))
        """),
        # 이슈 스캔은 큐가 없다. 주기적으로 도는 것이라 대기 개념이 성립하지 않는다.
        "operation_issue_scan": {"depth": 0, "oldest_age_seconds": None},
    }


def list_runs(
    db: Session, *, org_id: UUID | None = None,
    worker_name: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """실행 이력.

    **조직 범위를 강제한다.** 예전에는 필터가 없어서 한 조직의 관리자가 다른
    조직의 실행 기록(오류 메시지 포함)을 볼 수 있었다. `worker_health` 는 이미
    범위를 걸고 있었는데 이력만 빠져 있었다.

    `org_id IS NULL` 행은 전체 조직을 도는 cron 실행이라 모든 관리자에게 의미가
    있다 — `worker_health` 와 같은 규칙으로 포함한다.
    """
    where = ["(CAST(:org AS UUID) IS NULL OR org_id = CAST(:org AS UUID) OR org_id IS NULL)"]
    p: dict[str, Any] = {"lim": limit, "org": str(org_id) if org_id else None}
    if worker_name:
        where.append("worker_name = :n")
        p["n"] = worker_name
    clause = f"WHERE {' AND '.join(where)}"
    rows = db.execute(text(f"""
        SELECT id, org_id, worker_name, trigger, status, started_at, finished_at,
               duration_ms, processed_count, success_count, failure_count,
               skipped_count, error_code, error_message, summary_json
          FROM worker_run
          {clause}
         ORDER BY started_at DESC
         LIMIT :lim
    """), p).mappings().all()
    return [dict(r) for r in rows]
