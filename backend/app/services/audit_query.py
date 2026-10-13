"""audit_log 조회 / CSV export 서비스 (ADMIN 전용).

docs/22_api_spec.md §8 — audit_log 검색 인덱스 `ix_audit_org_table_row_time`, `ix_audit_org_time` 활용.
필터: 기간 / domain_table / action / actor_user_id / request_id.

CSV 컬럼명은 한국어 (CLAUDE.md Tax Report 규칙과 동일 정책).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.audit import AuditLogOut


def _build_where(
    *,
    org_id: UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
    domain_table: str | None,
    action: str | None,
    actor_user_id: UUID | None,
    request_id: str | None,
) -> tuple[str, dict[str, Any]]:
    where = ["org_id = :org_id"]
    params: dict[str, Any] = {"org_id": str(org_id)}
    if from_dt is not None:
        where.append("occurred_at >= :from_dt")
        params["from_dt"] = from_dt
    if to_dt is not None:
        where.append("occurred_at < :to_dt")
        params["to_dt"] = to_dt
    if domain_table:
        where.append("domain_table = :domain_table")
        params["domain_table"] = domain_table
    if action:
        where.append("action = :action")
        params["action"] = action
    if actor_user_id is not None:
        where.append("actor_user_id = :actor_user_id")
        params["actor_user_id"] = str(actor_user_id)
    if request_id:
        where.append("request_id = :request_id")
        params["request_id"] = request_id
    return " AND ".join(where), params


_BASE_COLS = (
    "id, domain_table, domain_row_id, action, actor_user_id, occurred_at, "
    "before_json, after_json, reason, request_id"
)


def list_audit_logs(
    db: Session,
    *,
    org_id: UUID,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    domain_table: str | None = None,
    action: str | None = None,
    actor_user_id: UUID | None = None,
    request_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditLogOut], int]:
    where, params = _build_where(
        org_id=org_id,
        from_dt=from_dt,
        to_dt=to_dt,
        domain_table=domain_table,
        action=action,
        actor_user_id=actor_user_id,
        request_id=request_id,
    )
    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM audit_log WHERE {where}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(f"""
          SELECT {_BASE_COLS}
            FROM audit_log
           WHERE {where}
           ORDER BY occurred_at DESC
           LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    ).all()
    items = [AuditLogOut(**r._mapping) for r in rows]
    return items, total


def export_audit_logs_csv(
    db: Session,
    *,
    org_id: UUID,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    domain_table: str | None = None,
    action: str | None = None,
    actor_user_id: UUID | None = None,
    request_id: str | None = None,
    max_rows: int = 100_000,
) -> str:
    """CSV 문자열을 반환. 컬럼명은 한국어 (CLAUDE.md tax_report 규칙 준수)."""
    where, params = _build_where(
        org_id=org_id,
        from_dt=from_dt,
        to_dt=to_dt,
        domain_table=domain_table,
        action=action,
        actor_user_id=actor_user_id,
        request_id=request_id,
    )
    rows = db.execute(
        text(f"""
          SELECT {_BASE_COLS}
            FROM audit_log
           WHERE {where}
           ORDER BY occurred_at DESC
           LIMIT :limit
        """),
        {**params, "limit": max_rows},
    ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "감사ID", "도메인테이블", "도메인행ID", "액션", "수행자ID",
        "발생시각", "이전상태", "이후상태", "사유", "요청ID",
    ])
    for r in rows:
        m = r._mapping
        writer.writerow([
            str(m["id"]),
            m["domain_table"],
            str(m["domain_row_id"]),
            m["action"],
            str(m["actor_user_id"]) if m["actor_user_id"] else "",
            m["occurred_at"].isoformat() if m["occurred_at"] else "",
            _json_to_text(m["before_json"]),
            _json_to_text(m["after_json"]),
            m["reason"] or "",
            m["request_id"] or "",
        ])
    return buf.getvalue()


def _json_to_text(v: Any) -> str:
    if v is None:
        return ""
    import json

    return json.dumps(v, ensure_ascii=False, default=str)
