"""audit_log 기록 헬퍼.

docs/22_api_spec.md §8: **모든 mutation 은 audit_log 에 최소 1행** 남긴다.
- 사용자 액션: actor_user_id = 인증된 사용자 UUID
- 시스템 액션 (배치/cron/내부 호출): actor_user_id = NULL
- before/after JSON 은 변경된 컬럼만 추려서 저장 (개인정보/평문 비밀번호 금지)

action 종류는 enum_audit_action 에 정의된 11종 (M003 에서 EXPORT/PERMISSION_CHANGE/
IMPORT 추가, DB 와 1:1 동기화 완료).
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# enum_audit_action 과 1:1 매핑. 변경 시 마이그레이션 + 본 Literal 동기화 필요.
AuditAction = Literal[
    "CREATE", "UPDATE", "DELETE", "RESTORE", "STATUS_CHANGE", "POLICY_OVERRIDE",
    "LOGIN", "LOGOUT", "PERMISSION_CHANGE", "EXPORT", "IMPORT",
]

# raw SQL — ORM 모델이 없어도 audit 가 동작하도록 의도.
# JSONB 캐스팅은 sqlalchemy 가 dict 직렬화 시 quoting 이슈를 일으키지 않도록 명시.
_INSERT = text("""
    INSERT INTO audit_log (
      org_id, domain_table, domain_row_id, action,
      actor_user_id, before_json, after_json, reason, request_id
    ) VALUES (
      :org_id, :domain_table, :domain_row_id, :action,
      :actor_user_id, CAST(:before_json AS JSONB), CAST(:after_json AS JSONB),
      :reason, :request_id
    )
""")


def _json_or_null(value: dict[str, Any] | None) -> str | None:
    """dict → JSON 문자열. UUID/datetime 등은 default=str 로 직렬화.

    한국어 보존을 위해 ensure_ascii=False.
    """
    return json.dumps(value, ensure_ascii=False, default=str) if value is not None else None


def record_audit(
    db: Session,
    *,
    org_id: UUID,
    domain_table: str,
    domain_row_id: UUID,
    action: AuditAction,
    actor_user_id: UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    request_id: str | None = None,
) -> None:
    """audit_log 1행 INSERT (caller 의 트랜잭션에 합류).

    의사코드:
        record_audit(db, org_id=..., domain_table='expense', domain_row_id=...,
                     action='UPDATE', actor_user_id=current_user.id,
                     before={'amount': 1000}, after={'amount': 1200},
                     reason='오타 수정')

    주의:
    - commit 은 호출자가 책임 (다른 mutation 과 같은 트랜잭션 유지).
    - before/after 에 평문 비밀번호·주민번호 등을 넣지 말 것 (Security Rules).
    """
    db.execute(
        _INSERT,
        {
            "org_id": str(org_id),
            "domain_table": domain_table,
            "domain_row_id": str(domain_row_id),
            "action": action,
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "before_json": _json_or_null(before),
            "after_json": _json_or_null(after),
            "reason": reason,
            "request_id": request_id,
        },
    )
