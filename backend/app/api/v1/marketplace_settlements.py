"""마켓플레이스 정산 레코드 라우터 (docs/22_api_spec.md §5).

읽기 전용. 정산 레코드는 배치 import 로만 생성됨.

endpoints:
  GET  /marketplace-settlements       — 기간/마켓 필터 + 페이지네이션
  GET  /marketplace-settlements/{id}  — 단건 조회

권한: ADMIN / OPERATOR / ACCOUNTING / READONLY
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services.marketplace_settlement import (
    get_marketplace_settlement,
    list_marketplace_settlements,
)

router = APIRouter(prefix="/marketplace-settlements", tags=["marketplace-settlements"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")


@router.get("")
def list_(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    marketplace: str | None = Query(default=None),
    integration_account_id: UUID | None = Query(
        default=None,
        description="판매자 계정으로 좁힌다. 같은 판매처에 계정이 여럿일 때 marketplace 로는 나눌 수 없다 (M033)",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    result = list_marketplace_settlements(
        db,
        org_id=user.org_id,
        start_date=start_date,
        end_date=end_date,
        marketplace=marketplace,
        integration_account_id=integration_account_id,
        page=page,
        page_size=page_size,
    )
    return envelope(
        [i.model_dump() for i in result["items"]],
        meta={
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
        },
    )


@router.get("/{record_id}")
def detail(
    record_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(
        get_marketplace_settlement(db, org_id=user.org_id, record_id=record_id).model_dump()
    )
