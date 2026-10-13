"""승인 워크플로 라우터 (M018, docs/43 §3·§8).

권한 (백엔드에서 강제 — 프론트 표시로 대체하지 않는다)
  생성/제출/취소  전 역할 (본인 요청)
  승인/반려/적용  target_type 별 정책 (services/approval._APPROVE_ROLES)
  전체 조회       ADMIN / ACCOUNTING / OPERATOR
  본인 요청 조회  전 역할

HTTP
  403 권한 없음 / 404 없음·타 조직 / 409 잘못된 전이·stale·중복 대상
  422 검증 실패(미지원 target_type 포함)
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.core.sensitive import can_view_sensitive_diff, redact_approval_row
from app.deps import CurrentUser, get_db, get_request_id
from app.services import approval as svc
from app.services import approval_targets as targets

router = APIRouter(prefix="/approvals", tags=["approvals"])

_any = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "SOURCING", "READONLY")
_actor = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "SOURCING")
_review = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")


class CreateBody(BaseModel):
    target_type: str
    target_id: UUID
    request_type: str
    proposed_changes: dict[str, Any]
    reason: str | None = None
    effective_from: date | None = None
    submit: bool = False


class UpdateDraftBody(BaseModel):
    proposed_changes: dict[str, Any] | None = None
    reason: str | None = None
    effective_from: date | None = None


class ReviewBody(BaseModel):
    comment: str | None = None


class RejectBody(BaseModel):
    comment: str = Field(min_length=2, max_length=1000)


class CancelBody(BaseModel):
    reason: str | None = None


def _redact(user: CurrentUser, row: dict) -> dict:
    """상태 전이 응답도 같은 규칙으로 가린다.

    조회만 가리면 승인 버튼을 눌러 돌아온 응답에 원가가 그대로 실린다.
    """
    return redact_approval_row(row, allowed=can_view_sensitive_diff(user.role))


@router.get("/target-types")
def target_types(user: CurrentUser = Depends(_any)) -> dict:
    """지원하는 승인 대상. 화면이 하드코딩하지 않도록 서버가 알려준다."""
    _ = user
    return envelope({
        t: targets.supported_request_types(t) for t in targets.supported_target_types()
    })


@router.get("/field-ownership")
def field_ownership(user: CurrentUser = Depends(_any)) -> dict:
    """어떤 필드가 어떤 요청 유형에 속하는가 + 무엇이 민감값인가.

    **프론트가 이 표를 자체 상수로 들고 있으면 언젠가 백엔드와 어긋난다.**
    어긋나면 사용자가 원가를 고치고 저장을 눌렀을 때에야 403 을 보게 된다 —
    입력을 다 끝낸 다음에. 서버가 알려주면 화면이 처음부터 올바르게 나눠 보여준다.

    권한 검사를 대신하지 않는다. 이건 **화면 구성용 힌트**고, 실제 차단은
    `update_product()` 의 `ApprovalRequiredError` 와 승인 서비스가 한다.
    """
    from app.core.sensitive import SENSITIVE_FIELDS, can_view_sensitive_diff

    out: dict[str, Any] = {}
    for t in targets.supported_target_types():
        adapter = targets.get_adapter(t)
        out[t] = {
            "request_types": {
                rt: sorted(targets.owned_fields(t, rt))
                for rt in targets.supported_request_types(t)
            },
            "approval_fields": sorted(adapter.allowed_fields),
            "sensitive_fields": sorted(SENSITIVE_FIELDS.get(t, frozenset())),
        }
    return envelope(out, meta={"can_view_sensitive": can_view_sensitive_diff(user.role)})


@router.get("")
def list_approvals(
    status: str | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    request_type: str | None = None,
    requested_by: UUID | None = None,
    reviewed_by: UUID | None = None,
    mine: bool = Query(default=False, description="내가 요청한 건만"),
    pending_for_me: bool = Query(default=False, description="내가 승인해야 할 건"),
    q: str | None = None,
    from_dt: date | None = Query(default=None, alias="from"),
    to_dt: date | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    limit: int | None = None,
    offset: int | None = None,
    user: CurrentUser = Depends(_any),
    db: Session = Depends(get_db),
) -> dict:
    eff_limit = limit if limit is not None else page_size
    eff_offset = offset if offset is not None else (page - 1) * page_size

    rows, total = svc.list_requests(
        db, org_id=user.org_id,
        status=status, target_type=target_type, target_id=target_id,
        request_type=request_type,
        requested_by=user.user_id if mine else requested_by,
        reviewed_by=reviewed_by,
        pending_for_me=user.user_id if pending_for_me else None,
        q=q, from_dt=from_dt, to_dt=to_dt,
        limit=eff_limit, offset=eff_offset,
    )
    # ⚠️ 민감값은 **서버에서 지워서** 보낸다. 값을 실어 보낸 뒤 프론트에서 숨기면
    #    개발자 도구·API 직접 호출로 그대로 보인다.
    allowed = can_view_sensitive_diff(user.role)
    redacted = [redact_approval_row(r, allowed=allowed) for r in rows]
    return envelope(redacted, {
        "total": total, "limit": eff_limit, "offset": eff_offset,
        "sensitive_visible": allowed,
    })


@router.get("/{request_id}")
def get_approval(
    request_id: UUID,
    user: CurrentUser = Depends(_any),
    db: Session = Depends(get_db),
) -> dict:
    row = svc.get_request(db, org_id=user.org_id, request_id=request_id)
    # 목록만 가리고 상세를 열어두면 상세 API 를 직접 부르는 것으로 뚫린다.
    return envelope(redact_approval_row(row, allowed=can_view_sensitive_diff(user.role)))


@router.post("")
def create_approval(
    body: CreateBody,
    user: CurrentUser = Depends(_actor),
    db: Session = Depends(get_db),
    correlation_id: str = Depends(get_request_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    return envelope(_redact(user, svc.create_request(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        target_type=body.target_type, target_id=body.target_id,
        request_type=body.request_type, proposed_changes=body.proposed_changes,
        reason=body.reason, effective_from=body.effective_from, submit=body.submit,
        idempotency_key=idempotency_key, request_id=correlation_id,
    )))


@router.patch("/{request_id}")
def update_draft(
    request_id: UUID,
    body: UpdateDraftBody,
    user: CurrentUser = Depends(_actor),
    db: Session = Depends(get_db),
    correlation_id: str = Depends(get_request_id),
) -> dict:
    return envelope(_redact(user, svc.update_draft(
        db, org_id=user.org_id, actor_user_id=user.user_id, request_id=request_id,
        proposed_changes=body.proposed_changes, reason=body.reason,
        effective_from=body.effective_from, correlation_id=correlation_id,
    )))


@router.post("/{request_id}/submit")
def submit_approval(
    request_id: UUID,
    user: CurrentUser = Depends(_actor),
    db: Session = Depends(get_db),
    correlation_id: str = Depends(get_request_id),
) -> dict:
    return envelope(_redact(user, svc.submit(
        db, org_id=user.org_id, request_id=request_id,
        actor_user_id=user.user_id, actor_role=user.role, correlation_id=correlation_id,
    )))


@router.post("/{request_id}/approve")
def approve_approval(
    request_id: UUID,
    body: ReviewBody,
    user: CurrentUser = Depends(_review),
    db: Session = Depends(get_db),
    correlation_id: str = Depends(get_request_id),
) -> dict:
    return envelope(_redact(user, svc.approve(
        db, org_id=user.org_id, request_id=request_id,
        actor_user_id=user.user_id, actor_role=user.role,
        comment=body.comment, correlation_id=correlation_id,
    )))


@router.post("/{request_id}/reject")
def reject_approval(
    request_id: UUID,
    body: RejectBody,
    user: CurrentUser = Depends(_review),
    db: Session = Depends(get_db),
    correlation_id: str = Depends(get_request_id),
) -> dict:
    return envelope(_redact(user, svc.reject(
        db, org_id=user.org_id, request_id=request_id,
        actor_user_id=user.user_id, actor_role=user.role,
        comment=body.comment, correlation_id=correlation_id,
    )))


@router.post("/{request_id}/cancel")
def cancel_approval(
    request_id: UUID,
    body: CancelBody,
    user: CurrentUser = Depends(_actor),
    db: Session = Depends(get_db),
    correlation_id: str = Depends(get_request_id),
) -> dict:
    return envelope(_redact(user, svc.cancel(
        db, org_id=user.org_id, request_id=request_id,
        actor_user_id=user.user_id, actor_role=user.role,
        reason=body.reason, correlation_id=correlation_id,
    )))


@router.post("/run-due", tags=["approvals"])
def run_due(
    batch_size: int = Query(default=50, ge=1, le=500),
    user: CurrentUser = Depends(require_roles("ADMIN")),
    db: Session = Depends(get_db),
) -> dict:
    """적용일이 도래한 승인 요청을 일괄 적용 (보호된 admin batch endpoint).

    cron/워커가 없어도 이 엔드포인트를 주기 호출하면 자동 적용이 동작한다.
    여러 인스턴스가 동시에 불러도 `FOR UPDATE SKIP LOCKED` 로 한 번만 적용된다.
    """
    from app.services.approval_executor import run_due_approvals

    result = run_due_approvals(db, org_id=user.org_id, batch_size=batch_size)
    return envelope(result.as_dict())


@router.post("/{request_id}/apply")
def apply_approval(
    request_id: UUID,
    user: CurrentUser = Depends(_review),
    db: Session = Depends(get_db),
    correlation_id: str = Depends(get_request_id),
) -> dict:
    """승인된 변경을 실제로 반영. 적용 시점에 모든 조건을 다시 검증한다."""
    return envelope(_redact(user, svc.apply(
        db, org_id=user.org_id, request_id=request_id,
        actor_user_id=user.user_id, actor_role=user.role, correlation_id=correlation_id,
    )))
