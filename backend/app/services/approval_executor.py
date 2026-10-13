"""미래 적용일 자동 적용 실행기 (M019).

무엇을 하나
-----------
`status='APPROVED'` 이고 `effective_from` 이 도래한 요청을 찾아 적용한다.

**적용 로직을 새로 쓰지 않는다.** `approval.apply()` 를 그대로 호출한다 —
수동 적용과 자동 적용이 다른 코드를 타면 언젠가 두 경로의 동작이 갈린다.
이 파일이 하는 일은 "누구를 언제 부를지" 와 "실패를 어떻게 기록할지" 뿐이다.

시간 기준
---------
`effective_from` 은 **DATE** 다(M018 스키마). 시각 정보가 없다.
그래서 **KST 기준 그 날짜 00:00 부터 적용 대상**으로 본다.
서버 로컬 시간대에 의존하지 않도록 KST 오늘 날짜를 직접 계산한다
(UTC 서버에서 `date.today()` 를 쓰면 한국 자정 직후 9시간 동안 하루 전 날짜가 나온다).

동시 실행
---------
여러 인스턴스가 동시에 돌아도 한 번만 적용돼야 한다.
`FOR UPDATE SKIP LOCKED` 로 배치를 선점하고, 실제 중복 방지는 `apply()` 안의
상태 CAS 와 `status='APPLIED'` 조기 반환이 최종 방어선이다.

실패 처리
---------
**자동 적용 실패를 반려로 바꾸지 않는다.** 승인 자체는 유효하다.
`status` 는 `APPROVED` 로 두고 `auto_apply_status` 와 시도 정보만 갱신한다.

  일시적 오류  → RETRYING, 지수 backoff, 제한된 횟수
  월 잠금      → HELD_BY_LOCK. **자동으로 잠금을 풀지 않는다.** 사람이 결정할 일이다.
  영구 오류    → PERMANENT_FAILURE (미지원 대상, 검증 실패, 대상 없음, stale conflict)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import (
    ApprovalExecutorAlreadyRunningError,
    ApprovalPermissionDeniedError,
    ApprovalTargetUnsupportedError,
    ApprovalVersionConflictError,
    ConflictError,
    DomainError,
    NotFoundError,
    SettlementLockedError,
    ValidationError,
)
from app.services import approval as svc
from app.services import notification_events

# 재시도 한도. 여기 도달하면 PERMANENT_FAILURE 로 바꾸고 사람이 보게 한다.
MAX_ATTEMPTS = 5
# 지수 backoff (분). attempt_count 인덱스로 사용.
_BACKOFF_MINUTES = (5, 15, 60, 240, 720)

# 재시도해도 소용없는 오류 — 대상·요청 자체가 잘못됐다.
_PERMANENT = (
    ApprovalTargetUnsupportedError,
    ApprovalVersionConflictError,
    ValidationError,
    NotFoundError,
    ApprovalPermissionDeniedError,
)


def today_kst() -> date:
    """KST 기준 오늘. 서버 시간대에 의존하지 않는다."""
    return (datetime.now(tz=UTC) + timedelta(hours=9)).date()


@dataclass(slots=True)
class BatchResult:
    """실행 결과. 로그와 API 응답이 같은 구조를 쓴다."""

    batch_id: str
    started_at: str
    finished_at: str | None = None
    scanned: int = 0
    applied: int = 0
    already_applied: int = 0
    held_by_lock: int = 0
    validation_failed: int = 0
    other_failed: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "scanned": self.scanned,
            "applied": self.applied,
            "already_applied": self.already_applied,
            "held_by_lock": self.held_by_lock,
            "validation_failed": self.validation_failed,
            "other_failed": self.other_failed,
            "details": self.details,
        }


def _claim_due_requests(
    db: Session, *, org_id: UUID | None, at: date, batch_size: int
) -> list[dict[str, Any]]:
    """적용 대상 선점.

    `FOR UPDATE SKIP LOCKED` — 다른 워커가 잡은 행은 건너뛴다.
    한 트랜잭션에서 너무 많이 처리하지 않도록 `batch_size` 로 제한한다.
    """
    where = [
        "status = 'APPROVED'",
        "deleted_at IS NULL",
        "effective_from IS NOT NULL",
        "effective_from <= :at",
        "(next_retry_at IS NULL OR next_retry_at <= now())",
        "(auto_apply_status IS NULL OR auto_apply_status IN ('PENDING','RETRYING','HELD_BY_LOCK'))",
    ]
    params: dict[str, Any] = {"at": at, "lim": batch_size}
    if org_id is not None:
        where.append("org_id = :org")
        params["org"] = str(org_id)

    rows = db.execute(text(f"""
        SELECT id, org_id, business_key, target_type, target_id, attempt_count
          FROM approval_request
         WHERE {' AND '.join(where)}
         ORDER BY effective_from, requested_at, id
         FOR UPDATE SKIP LOCKED
         LIMIT :lim
    """), params).mappings().all()
    return [dict(r) for r in rows]


def _record_failure(
    db: Session, *, request_id: UUID, org_id: UUID, exc: BaseException, attempt: int
) -> str:
    """실패 기록. 상태(`status`)는 건드리지 않는다.

    `last_error_message` 에는 **사용자용 문장만** 넣는다. SQL·비밀값·개인정보 금지.
    """
    if isinstance(exc, SettlementLockedError):
        auto_status = "HELD_BY_LOCK"
        next_retry = None  # 잠금이 풀릴 때까지 자동 재시도하지 않는다
    elif isinstance(exc, _PERMANENT):
        auto_status = "PERMANENT_FAILURE"
        next_retry = None
    elif attempt >= MAX_ATTEMPTS:
        auto_status = "PERMANENT_FAILURE"
        next_retry = None
    else:
        auto_status = "RETRYING"
        minutes = _BACKOFF_MINUTES[min(attempt, len(_BACKOFF_MINUTES) - 1)]
        next_retry = datetime.now(tz=UTC) + timedelta(minutes=minutes)

    code = getattr(exc, "code", type(exc).__name__)
    message = getattr(exc, "message", None) or "적용 중 오류가 발생했습니다."

    db.execute(text("""
        UPDATE approval_request
           SET auto_apply_status = :st,
               attempt_count = attempt_count + 1,
               last_attempt_at = now(),
               last_error_code = :code,
               last_error_message = :msg,
               next_retry_at = :retry,
               apply_error = :msg,
               updated_at = now()
         WHERE id = :id AND org_id = :org
    """), {"st": auto_status, "code": str(code)[:64], "msg": str(message)[:500],
           "retry": next_retry, "id": str(request_id), "org": str(org_id)})
    db.commit()

    # 알림은 **끝난 실패**에만 보낸다.
    #
    # RETRYING 은 다음 배치가 다시 시도한다 — 매 시도마다 알리면 하나의 문제가
    # 알림함을 채운다. HELD_BY_LOCK 은 실패가 아니라 의도된 보류다(월 잠금).
    #
    # PERMANENT_FAILURE 는 다르다. 승인자는 처리했다고 믿고 요청자는 반영됐다고
    # 믿는데 데이터는 그대로다. 사람이 개입하지 않으면 영원히 그 상태다.
    if auto_status == "PERMANENT_FAILURE":
        row = db.execute(text("""
            SELECT business_key, requested_by FROM approval_request
             WHERE id = :id AND org_id = :org
        """), {"id": str(request_id), "org": str(org_id)}).first()
        notification_events.approval_apply_failed(
            db, org_id=org_id, approval_id=request_id,
            business_key=str(row.business_key) if row else str(request_id),
            error_code=str(code)[:64],
            requester_user_id=UUID(str(row.requested_by)) if row and row.requested_by else None,
        )
        db.commit()

    return auto_status


# ──────────────────────────────────────────────────────────────
# 중복 실행 방지
# ──────────────────────────────────────────────────────────────
#
# `FOR UPDATE SKIP LOCKED` 는 **같은 건이 두 번 적용되는 것**을 막는다.
# 그런데 그것만으로는 배치가 동시에 여러 개 도는 것 자체는 막지 못한다 —
# 각자 다른 요청을 집어 가며 나란히 돌고, 로그·통계는 어느 쪽 것인지 뒤섞이고,
# 사용자가 버튼을 연타하면 그만큼 배치가 늘어난다.
#
# 그래서 조직 단위 **advisory lock** 으로 한 번에 하나만 돌게 한다.
#
# 왜 `approval_executor_run` 테이블이 아니라 advisory lock 인가
# ------------------------------------------------------------
# 실행 이력 테이블은 "지금 돌고 있나" 를 정확히 답하지 못한다. 프로세스가 죽으면
# RUNNING 행이 남아 영원히 잠긴다 — heartbeat 와 stale 회수 로직이 따라붙는다.
# advisory lock 은 연결이 끊기면 DB 가 자동으로 푼다. 마이그레이션도 필요 없다.
#
# 실행 **이력**이 필요해지면 그때 테이블을 추가한다. 그건 잠금과 별개 관심사다.
_LOCK_NAMESPACE = 0x4150524C  # 'APRL' — 다른 기능의 advisory lock 과 겹치지 않게

# 전역(org_id=None) 실행용 키. 어떤 org UUID 의 해시와도 겹치지 않도록 예약값을 쓴다.
_LOCK_GLOBAL_KEY = 0


def _lock_key_for(org_id: UUID | None) -> int:
    if org_id is None:
        return _LOCK_GLOBAL_KEY
    # advisory lock 의 두 번째 인자는 int4. UUID 를 32bit 로 접는다.
    # 충돌하면 서로 다른 조직이 같은 잠금을 나눠 쓰게 되는데, 그래도 **정확성은 유지**된다
    # (잠금은 성능·정돈 목적이고 중복 적용 방지는 SKIP LOCKED + CAS 가 담당한다).
    # 다만 0 은 전역 키라 피한다.
    folded = org_id.int % (2**31 - 1)
    return folded or 1


@contextmanager
def _executor_lock(db: Session, org_id: UUID | None) -> Iterator[None]:
    """조직 단위 실행 잠금. 이미 돌고 있으면 409.

    **Session 이 아니라 전용 connection** 에서 잡는다. advisory lock 은 연결 단위인데,
    executor 는 요청마다 커밋하고 Session 은 커밋 후 커넥션을 풀에 반납할 수 있다.
    그러면 잠금이 실행 도중 풀리거나(방어 실패) 반납된 커넥션에 남는다(잠금 누수).
    전용 connection 을 끝까지 쥐고 있다가 닫으면 둘 다 일어나지 않는다.
    """
    conn = db.get_bind().connect()
    key = _lock_key_for(org_id)
    try:
        acquired = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :k)"),
            {"ns": _LOCK_NAMESPACE, "k": key},
        ).scalar())
        if not acquired:
            raise ApprovalExecutorAlreadyRunningError(
                "자동 적용 배치가 이미 실행 중입니다. 끝난 뒤 다시 시도해 주세요.",
                detail={"org_id": str(org_id) if org_id else None},
            )
        yield
    finally:
        # 커넥션을 닫으면 세션 단위 advisory lock 은 DB 가 해제한다.
        # 명시적 unlock 을 먼저 시도하되, 실패해도 close 로 회수되므로 삼킨다.
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                         {"ns": _LOCK_NAMESPACE, "k": key})
        except Exception:  # noqa: BLE001 - close() 가 최종 회수 경로다
            pass
        conn.close()


def run_due_approvals(
    db: Session,
    *,
    org_id: UUID | None = None,
    at: date | None = None,
    batch_size: int = 50,
    system_actor_id: UUID | None = None,
) -> BatchResult:
    """적용일이 도래한 승인 요청을 적용한다.

    조직 단위 advisory lock 으로 감싼다 — 이미 실행 중이면
    `ApprovalExecutorAlreadyRunningError`(409). 프론트 버튼 disabled 는 방어가 아니다.

    `system_actor_id` 는 감사 로그의 actor. 지정하지 않으면 조직의 ADMIN 중 하나를 쓴다
    (`audit_log.actor_user_id` 가 app_user FK 라 NULL 이 아니면 실존해야 한다).
    """
    with _executor_lock(db, org_id):
        return _run_due_approvals_locked(
            db, org_id=org_id, at=at, batch_size=batch_size,
            system_actor_id=system_actor_id,
        )


def _run_due_approvals_locked(
    db: Session,
    *,
    org_id: UUID | None,
    at: date | None,
    batch_size: int,
    system_actor_id: UUID | None,
) -> BatchResult:
    at = at or today_kst()
    result = BatchResult(
        batch_id=uuid.uuid4().hex[:12],
        started_at=datetime.now(tz=UTC).isoformat(),
    )

    claimed = _claim_due_requests(db, org_id=org_id, at=at, batch_size=batch_size)
    result.scanned = len(claimed)
    if not claimed:
        db.commit()
        result.finished_at = datetime.now(tz=UTC).isoformat()
        return result

    for row in claimed:
        req_org = UUID(str(row["org_id"]))
        req_id = UUID(str(row["id"]))
        actor = system_actor_id or _system_actor_for(db, req_org)
        entry: dict[str, Any] = {
            "request_id": str(req_id), "business_key": row["business_key"],
            "target_type": row["target_type"],
        }

        try:
            # 적용 주체를 미리 SYSTEM 으로 표시한다. apply() 는 COALESCE 로 기존 값을
            # 보존하므로, 여기서 세워두면 수동(USER)과 자동(SYSTEM)이 구분된다.
            db.execute(text("""
                UPDATE approval_request SET applied_by_type = 'SYSTEM'
                 WHERE id = :id AND org_id = :org AND applied_by_type IS NULL
            """), {"id": str(req_id), "org": str(req_org)})

            # 수동 적용과 **같은 함수**를 쓴다. 별도 적용 로직을 만들지 않는다.
            applied = svc.apply(
                db, org_id=req_org, request_id=req_id,
                actor_user_id=actor, actor_role="ADMIN",
                correlation_id=f"auto-apply:{result.batch_id}",
            )
            db.execute(text("""
                UPDATE approval_request
                   SET auto_apply_status = 'DONE', applied_by_type = 'SYSTEM',
                       next_retry_at = NULL, apply_error = NULL, updated_at = now()
                 WHERE id = :id AND org_id = :org
            """), {"id": str(req_id), "org": str(req_org)})
            db.commit()

            result.applied += 1
            entry["result"] = "APPLIED"
            entry["status"] = applied["status"]

        except SettlementLockedError as exc:
            db.rollback()
            _record_failure(db, request_id=req_id, org_id=req_org, exc=exc,
                            attempt=row["attempt_count"])
            result.held_by_lock += 1
            entry["result"] = "HELD_BY_LOCK"
            entry["message"] = exc.message

        except (ValidationError, ApprovalTargetUnsupportedError,
                ApprovalVersionConflictError, NotFoundError) as exc:
            db.rollback()
            _record_failure(db, request_id=req_id, org_id=req_org, exc=exc,
                            attempt=row["attempt_count"])
            result.validation_failed += 1
            entry["result"] = "PERMANENT_FAILURE"
            entry["message"] = getattr(exc, "message", str(exc))

        except ConflictError as exc:
            # 이미 적용됐거나 다른 요청이 먼저 상태를 바꿨다 — 실패가 아니다.
            db.rollback()
            current = svc.get_request(db, org_id=req_org, request_id=req_id)
            if current["status"] == "APPLIED":
                result.already_applied += 1
                entry["result"] = "ALREADY_APPLIED"
            else:
                _record_failure(db, request_id=req_id, org_id=req_org, exc=exc,
                                attempt=row["attempt_count"])
                result.other_failed += 1
                entry["result"] = "CONFLICT"
                entry["message"] = getattr(exc, "message", str(exc))

        except DomainError as exc:
            db.rollback()
            _record_failure(db, request_id=req_id, org_id=req_org, exc=exc,
                            attempt=row["attempt_count"])
            result.other_failed += 1
            entry["result"] = "FAILED"
            entry["message"] = exc.message

        except Exception as exc:  # noqa: BLE001
            # 예상 못한 오류로 배치 전체가 멈추면 안 된다. 한 건만 실패로 기록하고 계속.
            db.rollback()
            _record_failure(db, request_id=req_id, org_id=req_org, exc=exc,
                            attempt=row["attempt_count"])
            result.other_failed += 1
            entry["result"] = "FAILED"
            entry["message"] = "적용 중 알 수 없는 오류가 발생했습니다."

        result.details.append(entry)

    result.finished_at = datetime.now(tz=UTC).isoformat()
    return result


def _system_actor_for(db: Session, org_id: UUID) -> UUID | None:
    """자동 적용의 감사 actor. 조직의 활성 ADMIN 중 하나.

    `audit_log.actor_user_id` 가 NULL 이면 "시스템 액션" 으로 해석되지만,
    승인 적용은 **누구의 승인으로** 반영됐는지가 중요해 사람을 남긴다.
    `applied_by_type='SYSTEM'` 이 자동 실행 사실을 따로 표시한다.
    """
    row = db.execute(text("""
        SELECT id FROM app_user
         WHERE org_id = :o AND role = 'ADMIN' AND is_active = TRUE AND deleted_at IS NULL
         ORDER BY created_at LIMIT 1
    """), {"o": str(org_id)}).first()
    return UUID(str(row.id)) if row else None
