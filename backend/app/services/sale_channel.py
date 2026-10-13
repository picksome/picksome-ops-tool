"""판매채널(sale_channel) 조회/검증 (M007, docs/27 D-287).

marketplace_fee_policy 등 6개 테이블의 marketplace 컬럼은 M007 에서 ENUM → TEXT 로
바뀌었다. DB ENUM 이 해주던 값 검증을 여기서 앱 레벨로 대신한다 — org 전용 채널 +
SYSTEM 공용 채널(org_id NULL) 을 함께 조회해서 판정.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.core.ids import new_id


def is_valid_channel_code(db: Session, *, org_id: UUID, code: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1 FROM sale_channel
             WHERE (org_id = :org OR org_id IS NULL)
               AND code = :code AND is_active = TRUE AND deleted_at IS NULL
             LIMIT 1
            """
        ),
        {"org": str(org_id), "code": code},
    ).first()
    return row is not None


def resolve_sale_channel(db: Session, *, org_id: UUID, code: str) -> str:
    """유효한 채널 코드면 그대로 반환, 아니면 ValidationError."""
    if not is_valid_channel_code(db, org_id=org_id, code=code):
        raise ValidationError(f"등록되지 않은 판매채널입니다: {code}")
    return code


def list_channels(db: Session, *, org_id: UUID, active_only: bool = True) -> list[dict]:
    """org 전용 + SYSTEM 공용 채널을 함께 반환 (표시용 — SYSTEM 먼저, code 순)."""
    where = "(org_id = :org OR org_id IS NULL) AND deleted_at IS NULL"
    if active_only:
        where += " AND is_active = TRUE"
    rows = db.execute(
        text(
            f"""
            SELECT id, org_id, code, display_name, is_system_default, integration_kind, is_active
              FROM sale_channel
             WHERE {where}
             ORDER BY is_system_default DESC, code
            """
        ),
        {"org": str(org_id)},
    ).all()
    return [dict(r._mapping) for r in rows]


def create_custom_channel(
    db: Session, *, org_id: UUID, actor: UUID, code: str, display_name: str
) -> UUID:
    """org 가 새 판매채널을 직접 추가(예: 오늘의집). 배포/마이그레이션 불필요."""
    if is_valid_channel_code(db, org_id=org_id, code=code):
        raise ValidationError(f"이미 존재하는 판매채널 코드입니다: {code}")

    new_cid = new_id()
    db.execute(
        text(
            """
            INSERT INTO sale_channel (
              id, org_id, code, display_name, is_system_default, integration_kind, is_active,
              created_by, updated_by
            ) VALUES (
              :id, :org, :code, :name, FALSE, 'NONE', TRUE, :actor, :actor
            )
            """
        ),
        {
            "id": str(new_cid),
            "org": str(org_id),
            "code": code,
            "name": display_name,
            "actor": str(actor),
        },
    )
    return new_cid
