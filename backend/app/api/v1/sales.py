"""판매 주문 라우터 (docs/22_api_spec.md §5).

endpoints:
  GET    /sales              — 기간/마켓플레이스/상품/검색어 필터 + 페이지네이션
  GET    /sales/{id}         — 단건 조회
  POST   /sales              — 등록 (MANUAL import_source 고정)
  PATCH  /sales/{id}         — 부분 수정
  DELETE /sales/{id}         — 소프트 삭제

권한:
  read  (_read):  ADMIN / OPERATOR / ACCOUNTING / READONLY
  write (_write): ADMIN / OPERATOR / ACCOUNTING
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.sales import SaleOrderCreate, SaleOrderUpdate
from app.services.sales import (
    create_sale,
    delete_sale,
    get_sale,
    list_sales,
    update_sale,
)

router = APIRouter(prefix="/sales", tags=["sales"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")


@router.get("")
def list_(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    marketplace: str | None = Query(default=None),
    integration_account_id: UUID | None = Query(
        default=None,
        description="판매자 계정으로 좁힌다. 같은 판매처에 계정이 여럿일 때 marketplace 로는 나눌 수 없다 (M032)",
    ),
    product_id: UUID | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    result = list_sales(
        db,
        org_id=user.org_id,
        start_date=start_date,
        end_date=end_date,
        marketplace=marketplace,
        integration_account_id=integration_account_id,
        product_id=product_id,
        q=q,
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


@router.get("/{sale_id}")
def detail(
    sale_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(get_sale(db, org_id=user.org_id, sale_id=sale_id).model_dump())


@router.post("", status_code=201)
def create(
    body: SaleOrderCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = create_sale(db, org_id=user.org_id, actor=user.user_id, body=body)
    return envelope(out.model_dump())


@router.patch("/{sale_id}")
def update(
    sale_id: UUID,
    body: SaleOrderUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_sale(db, org_id=user.org_id, actor=user.user_id, sale_id=sale_id, body=body)
    return envelope(out.model_dump())


@router.delete("/{sale_id}", status_code=204, response_model=None)
def delete(
    sale_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> None:
    delete_sale(db, org_id=user.org_id, actor=user.user_id, sale_id=sale_id)
