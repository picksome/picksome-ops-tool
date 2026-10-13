"""판매채널(sale_channel) 라우터 (M007, docs/27 D-287).

endpoints:
  GET  /sale-channels   — org 전용 + SYSTEM 공용 채널 목록 (오늘의집처럼 직접 추가한 것 포함)
  POST /sale-channels   — org 가 신규 채널 추가. 배포/마이그레이션 불필요.

권한: 정책 라우터와 동일 — 쓰기(_write) ADMIN/OPERATOR, 읽기(_read) + ACCOUNTING/READONLY.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.sale_channel_api import SaleChannelCreate, SaleChannelOut
from app.services.sale_channel import create_custom_channel, list_channels

router = APIRouter(prefix="/sale-channels", tags=["sale-channels"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR")


@router.get("")
def list_(
    active_only: bool = True,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    rows = list_channels(db, org_id=user.org_id, active_only=active_only)
    out = [SaleChannelOut(**r) for r in rows]
    return envelope([o.model_dump() for o in out])


@router.post("", status_code=201)
def create(
    body: SaleChannelCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    new_id = create_custom_channel(
        db, org_id=user.org_id, actor=user.user_id, code=body.code, display_name=body.display_name
    )
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="sale_channel",
        domain_row_id=new_id,
        action="CREATE",
        actor_user_id=user.user_id,
        after={"code": body.code, "display_name": body.display_name},
    )
    db.commit()
    rows = list_channels(db, org_id=user.org_id, active_only=False)
    created = next(r for r in rows if r["id"] == new_id)
    return envelope(SaleChannelOut(**created).model_dump())
