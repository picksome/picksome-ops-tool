"""예외 센터 (읽기 전용).

권한은 넓게 둔다 — 여기 담긴 것은 "누가 뭘 해야 하나" 이지 금액이 아니다.
금액이 필요한 항목은 링크를 따라간 화면에서 각자의 권한이 다시 걸린다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import exception_center

router = APIRouter(prefix="/operations/exceptions", tags=["operations"])

_view = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING")


@router.get("")
def list_exceptions(
    category: str | None = Query(None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_view),
) -> dict:
    """확인이 필요한 것들.

    `category` 로 걸러도 **카테고리 요약은 전부 준다.** 걸러진 화면에서도
    다른 곳에 뭐가 쌓였는지 보여야 한다.
    """
    data = exception_center.build(db, org_id=user.org_id)
    if category:
        data["items"] = [i for i in data["items"] if i["category"] == category]
        data["filtered_by"] = category
    return envelope(data)
