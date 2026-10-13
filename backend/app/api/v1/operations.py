"""운영 이슈 센터 + worker 상태 (Phase 35~38).

권한
  이슈 조회·처리   ADMIN / OPERATOR / ACCOUNTING
  이슈 읽기 전용   READONLY (목록만)
  worker 상태·실행 ADMIN

worker 상태에는 **비밀정보와 stack trace 를 노출하지 않는다.**
오류 코드와 한 줄 메시지만 내려간다 (`worker_runtime.short_error`).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db, get_request_id
from app.services import operation_issue as issues
from app.services import worker_runtime as rt

router = APIRouter(prefix="/operations", tags=["operations"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")
_work = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")
_admin = require_roles("ADMIN")


class IssueUpdateBody(BaseModel):
    status: str | None = None
    assigned_to: UUID | None = None
    clear_assignee: bool = False
    due_date: date | None = None
    # IGNORED 로 바꾸려면 필수다. 서비스가 검사한다.
    resolution_note: str | None = Field(default=None, max_length=2000)


class ManualIssueBody(BaseModel):
    issue_type: str = Field(min_length=1, max_length=80)
    severity: str = Field(pattern="^(BLOCKING|WARNING|INFO)$")
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    year_month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    assigned_to: UUID | None = None
    due_date: date | None = None


# ──────────────────────────────────────────────────────────────
# 이슈
# ──────────────────────────────────────────────────────────────

@router.get("/issues")
def list_issues(
    status: list[str] | None = Query(default=None),
    severity: list[str] | None = Query(default=None),
    issue_type: str | None = Query(default=None),
    assigned_to: UUID | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    year_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    keyword: str | None = Query(default=None, max_length=100),
    open_only: bool = Query(default=False),
    overdue_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    rows, total = issues.list_issues(
        db, org_id=user.org_id, status=status, severity=severity,
        issue_type=issue_type, assigned_to=assigned_to, entity_type=entity_type,
        entity_id=entity_id, year_month=year_month, keyword=keyword,
        open_only=open_only, overdue_only=overdue_only, limit=limit, offset=offset,
    )
    return envelope(rows, {"total": total, "limit": limit, "offset": offset})


@router.get("/issues/summary")
def issue_summary(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """대시보드 배지용 요약. 클릭하면 필터된 목록으로 이동한다."""
    return envelope(issues.summary(db, org_id=user.org_id))


@router.get("/issues/{issue_id}")
def get_issue(
    issue_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(issues.get_issue(db, org_id=user.org_id, issue_id=issue_id))


@router.patch("/issues/{issue_id}")
def update_issue(
    issue_id: UUID,
    body: IssueUpdateBody,
    user: CurrentUser = Depends(_work),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    return envelope(
        issues.update_issue(
            db, org_id=user.org_id, issue_id=issue_id, actor_user_id=user.user_id,
            status=body.status, assigned_to=body.assigned_to,
            clear_assignee=body.clear_assignee, due_date=body.due_date,
            resolution_note=body.resolution_note, request_id=request_id,
        )
    )


@router.post("/issues")
def create_issue(
    body: ManualIssueBody,
    user: CurrentUser = Depends(_work),
    db: Session = Depends(get_db),
) -> dict:
    """사람이 직접 만든 이슈. 스캐너가 자동으로 닫지 않는다."""
    return envelope(
        issues.create_manual_issue(
            db, org_id=user.org_id, actor_user_id=user.user_id,
            issue_type=body.issue_type, severity=body.severity, title=body.title,
            description=body.description, entity_type=body.entity_type,
            entity_id=body.entity_id, year_month=body.year_month,
            assigned_to=body.assigned_to, due_date=body.due_date,
        )
    )


@router.post("/issues/scan")
def scan_issues(
    user: CurrentUser = Depends(_work),
    db: Session = Depends(get_db),
) -> dict:
    """수동 스캔. 사람이 닫은 이슈를 다시 열지 않는다."""
    def _scan() -> rt.WorkerOutcome:
        r = issues.scan(db, org_id=user.org_id, actor_user_id=user.user_id)
        return rt.WorkerOutcome(
            processed=r.candidates, success=r.created + r.refreshed,
            failure=0, skipped=r.skipped_closed, summary=r.as_dict(),
        )

    record = rt.run_worker(
        db, worker_name="operation_issue_scan", org_id=user.org_id,
        trigger="API", fn=_scan,
    )
    return envelope(record.as_dict())


# ──────────────────────────────────────────────────────────────
# worker 상태
# ──────────────────────────────────────────────────────────────

@router.get("/workers")
def worker_status(
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    """worker 별 마지막 실행·성공·실패, 대기 깊이, 가장 오래된 대기 건의 나이."""
    return envelope({
        "workers": rt.worker_health(db, org_id=user.org_id),
        "stale_run_threshold_minutes": rt.STALE_RUN_MINUTES,
    })


@router.get("/workers/runs")
def worker_runs(
    worker_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    rows = rt.list_runs(
        db, org_id=user.org_id, worker_name=worker_name, limit=limit,
    )
    return envelope(rows, {"total": len(rows)})


@router.post("/workers/reclaim-stale")
def reclaim_stale(
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    """중단된 프로세스가 남긴 RUNNING 기록을 정리한다.

    영구 RUNNING 이 남으면 관리 화면이 "지금 실행 중" 이라고 거짓말을 한다.
    """
    return envelope({"reclaimed": rt.reclaim_stale_runs(db)})


@router.post("/workers/{worker_name}/run")
def run_worker_once(
    worker_name: str,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    """worker 한 번 실행 (ADMIN 전용).

    **업무 로직을 여기서 다시 쓰지 않는다.** CLI(`app/scripts/run_workers.py`)가
    쓰는 어댑터 표를 그대로 가져다 쓴다 — 화면용 구현을 따로 만들면 cron 이 하는 일과
    관리자가 누르는 버튼이 서로 다른 일을 하게 되고, 그 차이는 사고가 난 뒤에 드러난다.

    **이 조직 범위로만 돈다.** 전체 조직 실행은 cron 의 일이다 — 관리 화면에서
    다른 조직의 데이터를 건드릴 수 있으면 조직 격리가 깨진다.

    실패해도 500 으로 터뜨리지 않는다. `worker_run` 에 FAILED 로 남기고
    오류 코드와 한 줄 메시지만 돌려준다(stack trace·SQL 노출 금지).
    """
    from app.scripts.run_workers import WORKER_ADAPTERS

    if worker_name not in WORKER_ADAPTERS:
        raise NotFoundError(f"알 수 없는 worker 입니다: {worker_name}")

    fn = WORKER_ADAPTERS[worker_name]
    try:
        record = rt.run_worker(
            db, worker_name=worker_name, org_id=user.org_id,
            trigger="API", fn=lambda: fn(db, user.org_id, trigger="API"),
        )
    except Exception as exc:  # noqa: BLE001 — 실패를 화면에 사실대로 전달한다
        db.rollback()
        code, message = rt.short_error(exc)
        return envelope({
            "worker": worker_name, "status": "FAILED",
            "error_code": code, "error_message": message,
        })
    return envelope(record.as_dict())
