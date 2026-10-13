"""audit_log 출력 schemas.

docs/22_api_spec.md §8 (감사) — GET /admin/audit, /admin/audit/export.
before/after JSON 은 변경된 컬럼만 (core/audit.record_audit 가 기록)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    domain_table: str
    domain_row_id: UUID
    action: str
    actor_user_id: UUID | None
    occurred_at: datetime
    before_json: dict[str, Any] | None = None
    after_json: dict[str, Any] | None = None
    reason: str | None = None
    request_id: str | None = None
