"""worker CLI 진입점 (Phase 37).

사용법
-----
```bash
# 전체 한 바퀴 (외부 cron 이 이걸 부른다)
python -m app.scripts.run_workers --once

# 개별 실행
python -m app.scripts.run_workers --worker due_approvals
python -m app.scripts.run_workers --worker operation_issue_scan --org <UUID>

# 죽은 프로세스가 남긴 RUNNING 정리만
python -m app.scripts.run_workers --reclaim-only

# 상태 확인 (아무것도 실행하지 않는다)
python -m app.scripts.run_workers --health
```

왜 애플리케이션 안에 스케줄러를 두지 않나
------------------------------------
`app/services/scheduler.py` 의 APScheduler 는 **알림 발송**용이고 프로세스에
붙어 있다. 워커 프로세스를 여러 개 띄우면 같은 잡이 중복 실행된다.

이 CLI 는 그 문제를 바깥으로 밀어낸다 — 스케줄링은 외부(cron/systemd)가 하고,
중복 실행은 각 worker 의 advisory lock 이 막는다.

한 worker 의 실패가 다음을 막지 않는다
---------------------------------
`--once` 는 각 worker 를 독립적으로 실행한다. 하나가 터져도 나머지는 돈다.
종료 코드로 결과를 구분한다.

```
0  전부 성공
1  일부 실패 (나머지는 실행됨)
2  실행 자체가 불가능 (DB 접속 실패 등)
```
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any
from uuid import UUID

from app.db.session import SessionLocal
from app.services import worker_runtime as rt
from app.services.worker_runtime import WorkerOutcome

_log = logging.getLogger("run_workers")


# ──────────────────────────────────────────────────────────────────────────
# 각 worker 어댑터
#
# 업무 로직을 여기서 구현하지 않는다. 기존 서비스 함수를 부르고
# 결과를 공통 형태(WorkerOutcome)로 옮기기만 한다.
#
# 공통 시그니처는 `(db, org_id, *, trigger)` 다. 대부분은 `trigger` 를 쓰지
# 않지만 받기는 한다 — 어댑터마다 시그니처가 다르면 호출부가 어느 것이
# 무엇을 받는지 알아야 하고, 그 분기가 곧 버그가 된다.
# ──────────────────────────────────────────────────────────────────────────

def _run_due_approvals(db, org_id: UUID | None, *, trigger: str) -> WorkerOutcome:
    from app.services import approval_executor

    _ = trigger
    r = approval_executor.run_due_approvals(db, org_id=org_id)
    # 월 잠금 보류(held_by_lock)는 **실패가 아니다.** 자동으로 잠금을 풀지 않는 것이
    # 의도된 동작이라 skipped 로 센다. failure 로 세면 관리 화면의 실패 카운트가
    # 부풀어 진짜 오류가 묻힌다.
    return WorkerOutcome(
        processed=r.scanned,
        success=r.applied,
        failure=r.validation_failed + r.other_failed,
        skipped=r.held_by_lock + r.already_applied,
        summary=r.as_dict(),
    )


def _run_attachment_delete(db, org_id: UUID | None, *, trigger: str) -> WorkerOutcome:
    from app.services import expense_attachment

    _ = trigger
    r = expense_attachment.run_delete_worker(db, org_id=org_id)
    return WorkerOutcome(
        processed=r.scanned, success=r.deleted, failure=r.failed,
        skipped=r.retry_pending, summary=r.as_dict(),
    )


def _run_channel_publish(db, org_id: UUID | None, *, trigger: str) -> WorkerOutcome:
    from app.services import channel_publish

    _ = trigger
    r = channel_publish.run_publish_batch(db, org_id=org_id)
    # connector 미연결은 실패가 아니다. 기다린다고 해결되지 않는 정직한 상태이므로
    # skipped 로 센다 (M024 의 판단과 같다).
    return WorkerOutcome(
        processed=r.claimed,
        success=r.published,
        failure=r.failed,
        skipped=r.connector_unavailable + r.retry_pending,
        summary=r.as_dict(),
    )


def _run_period_recalc(db, org_id: UUID | None, *, trigger: str) -> WorkerOutcome:
    """재계산 필요 기간을 실제로 다시 집계한다.

    `period_recalc_flag` 는 M026 이 표시만 남기고 처리기를 두지 않았다.
    여기서 그 표시를 소비한다 — 표시만 쌓이고 아무도 처리하지 않으면
    마감 검증의 `PERIOD_RECALC_PENDING` 이 영원히 차단으로 남는다.
    """
    from app.services import period_recalc

    _ = trigger
    return period_recalc.process_dirty_periods(db, org_id=org_id)


def _run_issue_scan(db, org_id: UUID | None, *, trigger: str) -> WorkerOutcome:
    from app.services import operation_issue

    _ = trigger
    if org_id is not None:
        r = operation_issue.scan(db, org_id=org_id)
        return WorkerOutcome(
            processed=r.candidates, success=r.created + r.refreshed,
            failure=0, skipped=r.skipped_closed, summary=r.as_dict(),
        )
    r2 = operation_issue.scan_all_orgs(db)
    return WorkerOutcome(
        processed=int(r2["candidates"]),
        success=int(r2["created"]) + int(r2["refreshed"]),
        failure=len(r2["failures"]),
        skipped=int(r2["skipped_closed"]),
        summary=r2,
    )


def _run_automation_scan(db, org_id: UUID | None, *, trigger: str) -> WorkerOutcome:
    """주기가 된 자동화 정책을 실행한다 (M045).

    조직 전체를 도는 경우 **한 조직의 실패가 다른 조직을 막지 않는다.**
    자동화는 조직마다 정책이 다르고, 한 곳의 설정 오류로 전체가 멈추면
    아무도 자동화를 켜지 않게 된다.

    개별 정책의 결과는 `automation_run` 이 갖는다. 여기 요약은 "몇 개를
    돌렸고 몇 개가 실패했나" 까지다 — 다섯 정책의 결과를 한 worker_run 에
    뭉개면 무엇이 실패했는지 알 수 없다.
    """
    from sqlalchemy import text as _text

    from app.services import automation_runners as runners

    org_ids = [org_id] if org_id is not None else [
        UUID(str(r[0])) for r in db.execute(_text("""
            SELECT DISTINCT org_id FROM automation_policy
             WHERE enabled AND deleted_at IS NULL AND interval_minutes IS NOT NULL
        """))
    ]

    processed = success = failure = 0
    details: list[dict[str, Any]] = []
    for oid in org_ids:
        for policy in runners.due_policies(db, org_id=oid):
            processed += 1
            try:
                # 실행 주체를 그대로 넘긴다. 못박아 두면 기동 직후
                # 따라잡기(STARTUP)가 주기 실행으로 기록된다(M058).
                result = runners.run_policy(
                    db, org_id=oid, policy=policy, trigger=trigger,
                )
                details.append(result.as_dict())
                success += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failure += 1
                details.append({
                    "automation_type": policy["automation_type"],
                    "status": "FAILED", "error_code": type(exc).__name__,
                })

    return WorkerOutcome(
        processed=processed, success=success, failure=failure,
        summary={"runs": details},
    )


# worker 이름 → 어댑터.
#
# **공개 이름이다.** 관리자 화면의 수동 실행 endpoint(`api/v1/operations.py`)가
# 이 표를 그대로 쓴다. 화면용으로 같은 로직을 다시 쓰면 CLI 와 화면이 서로 다른
# 일을 하게 되고, 그 차이는 사고가 난 뒤에야 드러난다.
WORKER_ADAPTERS = {
    "due_approvals": _run_due_approvals,
    "attachment_delete": _run_attachment_delete,
    "channel_publish": _run_channel_publish,
    "period_recalc": _run_period_recalc,
    "operation_issue_scan": _run_issue_scan,
    "automation_scan": _run_automation_scan,
}

# 실행 순서에 의미가 있다.
#   승인 적용 → 그 결과로 기간이 더러워짐 → 재계산 → 그 상태로 이슈 스캔
# 스캔을 먼저 돌리면 방금 해결된 것이 이슈로 올라온다.
RUN_ORDER = (
    "due_approvals",
    "attachment_delete",
    "channel_publish",
    "period_recalc",
    "operation_issue_scan",
    # 자동화는 **맨 뒤**다. 마감 사전점검·마진 감시가 방금 갱신된 상태를
    # 봐야 한다 — 먼저 돌리면 재계산 전 숫자로 판단하고 경고를 낸다.
    "automation_scan",
)

# 이전 이름. 이 모듈 안에서만 쓴다.
_WORKERS = WORKER_ADAPTERS
_ORDER = RUN_ORDER


def run_all(
    db, *, org_id: UUID | None = None, trigger: str = "MANUAL",
) -> dict[str, Any]:
    """전체 한 바퀴. **한 worker 의 실패가 다음을 막지 않는다.**"""
    reclaimed = rt.reclaim_stale_runs(db)
    records: list[dict[str, Any]] = []
    failures = 0

    for name in _ORDER:
        fn = _WORKERS[name]
        try:
            rec = rt.run_worker(
                db, worker_name=name, org_id=org_id, trigger=trigger,
                fn=lambda f=fn: f(db, org_id, trigger=trigger),
            )
            records.append(rec.as_dict())
        except Exception as exc:  # noqa: BLE001 — 격리가 목적이다
            db.rollback()
            failures += 1
            code, message = rt.short_error(exc)
            _log.exception("worker %s 실패", name)
            records.append({
                "worker": name, "status": "FAILED",
                "error_code": code, "error_message": message,
            })

    return {
        "reclaimed_stale_runs": reclaimed,
        "workers": records,
        "failed_workers": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_workers",
        description="PICKSOME OPS 백그라운드 worker 실행기",
    )
    parser.add_argument("--once", action="store_true",
                        help="전체 worker 를 한 바퀴 실행하고 종료")
    parser.add_argument("--worker", choices=sorted(_WORKERS),
                        help="특정 worker 만 실행")
    parser.add_argument("--org", help="조직 UUID (없으면 전체 조직)")
    parser.add_argument("--reclaim-only", action="store_true",
                        help="중단된 실행 기록만 정리하고 종료")
    parser.add_argument("--health", action="store_true",
                        help="worker 상태만 출력. 아무것도 실행하지 않는다")
    parser.add_argument("--trigger", default="MANUAL",
                        choices=["MANUAL", "SCHEDULED", "API", "STARTUP"],
                        help="실행 주체 (cron 에서는 SCHEDULED 를 넘긴다)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    # worker 프로세스는 `create_app()` 을 지나지 않는다. 여기서 등록하지 않으면
    # 화면에서는 되는 주문 동기화가 자동 실행에서만 "연동 미구현" 으로 막힌다.
    from app.connectors.bootstrap import register_builtin

    register_builtin()

    org_id = UUID(args.org) if args.org else None

    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"DB 접속 실패: {exc}"}, ensure_ascii=False))
        return 2

    try:
        if args.health:
            payload = {
                "workers": rt.worker_health(db, org_id=org_id),
                "queues": rt.queue_depths(db, org_id=org_id),
            }
            print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
            return 0

        if args.reclaim_only:
            from app.services import order_sync as osync

            # 외부 동기화 기록도 같이 정리한다. 둘 중 하나만 치우면 관리
            # 화면의 절반이 계속 "지금 조회 중" 이라고 말한다.
            print(json.dumps({
                "reclaimed_stale_runs": rt.reclaim_stale_runs(db),
                "reclaimed_stale_sync_runs": osync.reclaim_stale_sync_runs(db),
            }, ensure_ascii=False))
            return 0

        if args.worker:
            rt.reclaim_stale_runs(db)
            fn = _WORKERS[args.worker]
            try:
                rec = rt.run_worker(
                    db, worker_name=args.worker, org_id=org_id, trigger=args.trigger,
                    fn=lambda: fn(db, org_id, trigger=args.trigger),
                )
            except Exception as exc:  # noqa: BLE001
                code, message = rt.short_error(exc)
                print(json.dumps(
                    {"worker": args.worker, "status": "FAILED",
                     "error_code": code, "error_message": message},
                    ensure_ascii=False,
                ))
                return 1
            print(json.dumps(rec.as_dict(), ensure_ascii=False, default=str, indent=2))
            return 0

        if not args.once:
            parser.print_help()
            return 0

        result = run_all(db, org_id=org_id, trigger=args.trigger)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 1 if result["failed_workers"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
