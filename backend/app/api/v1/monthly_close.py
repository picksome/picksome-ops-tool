"""월 마감 라우터 (Phase 2) + 이익 단계 조회 (Phase 1).

권한 (docs/43_ops_stability_plan.md §3)
  조회        ADMIN / OPERATOR / ACCOUNTING / READONLY
  검증·검토   ADMIN / OPERATOR / ACCOUNTING
  확정        ADMIN / ACCOUNTING
  잠금·해제   ADMIN 만

**권한은 프론트 표시가 아니라 여기서 강제된다.** 화면에서 버튼을 숨겨도
API 를 직접 부르면 통과하면 안 된다.
"""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db, get_request_id
from app.services import monthly_close as mc
from app.services import profit_status as ps

router = APIRouter(prefix="/monthly-close", tags=["monthly-close"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")
_work = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")
_confirm = require_roles("ADMIN", "ACCOUNTING")
_admin = require_roles("ADMIN")


class ConfirmBody(BaseModel):
    acknowledged_warnings: list[str] = Field(default_factory=list)


class UnlockBody(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    approved_by: UUID | None = None


class ReopenRequestBody(BaseModel):
    # 어댑터가 5자 이상을 요구한다. 라우터에서 먼저 거르면 오류가 필드에 붙는다.
    reason: str = Field(min_length=5, max_length=500)
    submit: bool = True


@router.get("")
def list_months(
    limit: int = Query(default=24, ge=1, le=120),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    rows = mc.list_months(db, org_id=user.org_id, limit=limit)
    return envelope(rows, {"total": len(rows)})


@router.get("/{year_month}")
def get_month(
    year_month: str,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    data = mc.get_month(db, org_id=user.org_id, year_month=year_month)
    data["profit_summary"] = ps.summarize_month(db, org_id=user.org_id, year_month=year_month)
    return envelope(data)


@router.post("/{year_month}/validate")
def validate(
    year_month: str,
    user: CurrentUser = Depends(_work),
    db: Session = Depends(get_db),
) -> dict:
    items = mc.run_validation(
        db, org_id=user.org_id, year_month=year_month, actor_user_id=user.user_id
    )
    payload = [asdict(i) for i in items]
    return envelope(
        payload,
        {
            "blocking": sum(1 for i in items if i.grade == "BLOCKING"),
            "warning": sum(1 for i in items if i.grade == "WARNING"),
            "info": sum(1 for i in items if i.grade == "INFO"),
        },
    )


@router.get("/{year_month}/check-runs")
def list_check_runs(
    year_month: str,
    limit: int = Query(default=20, ge=1, le=100),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """검증 실행 이력. 확정 직전에 무엇이 남아 있었는지 나중에 확인할 수 있어야 한다."""
    rows = mc.list_check_runs(db, org_id=user.org_id, year_month=year_month, limit=limit)
    return envelope(rows, {"total": len(rows)})


@router.get("/{year_month}/check-runs/{run_id}")
def get_check_run(
    year_month: str,
    run_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    run = mc.get_check_run(db, org_id=user.org_id, run_id=run_id)
    if run["year_month"] != year_month:
        # 경로의 달과 실제 run 의 달이 다르면 잘못된 링크다. 조용히 다른 달을 보여주지 않는다.
        raise NotFoundError("검증 실행 기록을 찾을 수 없습니다.")
    return envelope(run)


@router.get("/{year_month}/snapshot-preview")
def snapshot_preview(
    year_month: str,
    user: CurrentUser = Depends(_work),
    db: Session = Depends(get_db),
) -> dict:
    """확정하면 어떤 숫자가 박제되는지 미리 본다. 아무것도 바꾸지 않는다."""
    return envelope(mc.preview_snapshot(db, org_id=user.org_id, year_month=year_month))


@router.get("/{year_month}/snapshots")
def snapshot_history(
    year_month: str,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """세대 이력. 재오픈 전후 숫자를 비교할 수 있어야 한다."""
    rows = mc.snapshot_history(db, org_id=user.org_id, year_month=year_month)
    return envelope(rows, {"total": len(rows)})


@router.get("/{year_month}/drift")
def drift(
    year_month: str,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """확정 후 원본 데이터가 조용히 바뀌었는지 확인한다.

    snapshot 이 없으면 `null` 을 돌려준다 — "변경 없음" 과 구분되어야 한다.
    """
    return envelope(mc.check_drift(db, org_id=user.org_id, year_month=year_month))


@router.post("/{year_month}/request-review")
def request_review(
    year_month: str,
    user: CurrentUser = Depends(_work),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    return envelope(
        mc.request_review(
            db, org_id=user.org_id, year_month=year_month,
            actor_user_id=user.user_id, request_id=request_id,
        )
    )


@router.post("/{year_month}/confirm")
def confirm(
    year_month: str,
    body: ConfirmBody,
    user: CurrentUser = Depends(_confirm),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    return envelope(
        mc.confirm(
            db, org_id=user.org_id, year_month=year_month, actor_user_id=user.user_id,
            acknowledged_warnings=body.acknowledged_warnings, request_id=request_id,
        )
    )


@router.post("/{year_month}/lock")
def lock(
    year_month: str,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    return envelope(
        mc.lock(
            db, org_id=user.org_id, year_month=year_month,
            actor_user_id=user.user_id, request_id=request_id,
        )
    )


@router.post("/{year_month}/reopen-request")
def create_reopen_request(
    year_month: str,
    body: ReopenRequestBody,
    user: CurrentUser = Depends(_work),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    """월 재오픈 **승인 요청** 생성 (Phase 29).

    잠긴 달을 여는 것은 확정한 숫자를 무효화하는 행위다. 버튼 한 번으로 열지 않고
    승인을 거친다. 승인은 ADMIN 만 할 수 있다.

    화면이 정산 행의 UUID 를 몰라도 되도록 년월로 받아 여기서 찾는다.
    """
    return envelope(
        mc.request_reopen_approval(
            db, org_id=user.org_id, year_month=year_month,
            actor_user_id=user.user_id, reason=body.reason,
            submit=body.submit, request_id=request_id,
        )
    )


@router.get("/{year_month}/reopen-quota")
def reopen_quota(
    year_month: str,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """남은 재개 횟수. 화면이 미리 보여줄 수 있어야 한다(D-285 한도 기본 3회)."""
    return envelope(mc.reopen_limit_of(db, org_id=user.org_id, year_month=year_month))


@router.post("/{year_month}/unlock")
def unlock(
    year_month: str,
    body: UnlockBody,
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    return envelope(
        mc.unlock(
            db, org_id=user.org_id, year_month=year_month, actor_user_id=user.user_id,
            reason=body.reason, approved_by=body.approved_by, request_id=request_id,
        )
    )


# ── 이익 단계 (Phase 1) ────────────────────────────────────────
profit_router = APIRouter(prefix="/profit-status", tags=["profit-status"])


@profit_router.get("/{sales_order_id}")
def get_profit_status(
    sales_order_id: UUID,
    user: CurrentUser = Depends(require_roles("ADMIN", "OPERATOR", "ACCOUNTING")),
    db: Session = Depends(get_db),
) -> dict:
    """한 주문의 예상·잠정·정산·확정 이익과 미확정 사유."""
    return envelope(
        ps.get_profit_detail(db, org_id=user.org_id, sales_order_id=sales_order_id)
    )


@profit_router.get("")
def month_summary(
    year_month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    user: CurrentUser = Depends(require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")),
    db: Session = Depends(get_db),
) -> dict:
    """월 단위 단계별 합계. 대시보드가 예상이익과 확정이익을 섞지 않도록."""
    return envelope(ps.summarize_month(db, org_id=user.org_id, year_month=year_month))
