"""감사 로그 라우터 (ADMIN 전용).

GET /admin/audit             — 필터/페이지네이션 조회
GET /admin/audit/export.csv  — CSV 다운로드 (한국어 헤더)
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services.audit_query import export_audit_logs_csv, list_audit_logs

router = APIRouter(prefix="/admin/audit", tags=["admin.audit"])

_admin = require_roles("ADMIN")


@router.get("")
def list_(
    from_dt: datetime | None = Query(default=None, alias="from"),
    to_dt: datetime | None = Query(default=None, alias="to"),
    domain_table: str | None = Query(default=None),
    action: str | None = Query(default=None),
    actor_user_id: UUID | None = Query(default=None),
    request_id: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_audit_logs(
        db,
        org_id=user.org_id,
        from_dt=from_dt,
        to_dt=to_dt,
        domain_table=domain_table,
        action=action,
        actor_user_id=actor_user_id,
        request_id=request_id,
        limit=limit,
        offset=offset,
    )
    return envelope(
        [i.model_dump() for i in items],
        meta={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/export.csv")
def export_csv(
    from_dt: datetime | None = Query(default=None, alias="from"),
    to_dt: datetime | None = Query(default=None, alias="to"),
    domain_table: str | None = Query(default=None),
    action: str | None = Query(default=None),
    actor_user_id: UUID | None = Query(default=None),
    request_id: str | None = Query(default=None),
    user: CurrentUser = Depends(_admin),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    csv_text = export_audit_logs_csv(
        db,
        org_id=user.org_id,
        from_dt=from_dt,
        to_dt=to_dt,
        domain_table=domain_table,
        action=action,
        actor_user_id=actor_user_id,
        request_id=request_id,
    )
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="audit_log",
        domain_row_id=user.user_id,
        action="EXPORT",
        actor_user_id=user.user_id,
        reason="audit_log CSV export",
    )
    db.commit()

    filename = f"audit_log_{datetime.utcnow():%Y%m%d_%H%M%S}.csv"
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
