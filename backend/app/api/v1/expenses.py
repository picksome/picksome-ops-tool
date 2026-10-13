"""사업비용 라우터 (D-284, docs/22_api_spec.md §6).

endpoints:
  GET    /expenses              — 기간/카테고리/증빙상태/결제수단/결제자/검색어 필터 + 페이지네이션
  GET    /expenses/{id}         — 상세 (tax_policy_id snapshot + vat 결과 포함)
  POST   /expenses              — 등록 (vat_inclusion_type 별 부가세 계산은 service 단)
  PATCH  /expenses/{id}         — 부분 수정 (LOCKED 정산 연계 시 409)
  DELETE /expenses/{id}         — 삭제 (LOCKED 정산 연계 시 409)

D-284 vat_inclusion_type:
  INCLUDED  — amount 에 부가세 포함, 분리 계산 후 supply/vat 분리 저장
  EXCLUDED  — amount = 공급가, 부가세 별도 가산
  EXEMPT    — 면세, vat=0

권한:
  - write(_write): ADMIN/OPERATOR/ACCOUNTING
  - read(_read): + SOURCING/READONLY"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.expense import ExpenseAdjustmentCreate, ExpenseCreate, ExpenseUpdate
from app.services.expense import (
    create_expense,
    delete_expense,
    get_expense,
    list_expenses,
    update_expense,
)

router = APIRouter(prefix="/expenses", tags=["expenses"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "OPERATOR", "ACCOUNTING")


@router.get("")
def list_(
    from_dt: date | None = Query(default=None, alias="from"),
    to_dt: date | None = Query(default=None, alias="to"),
    category_id: UUID | None = Query(default=None),
    evidence_status: str | None = Query(default=None),
    q: str | None = Query(default=None, description="내용·메모 부분 일치"),
    payment_method: str | None = Query(default=None),
    payment_card_holder_id: UUID | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_expenses(
        db, org_id=user.org_id, from_dt=from_dt, to_dt=to_dt,
        category_id=category_id, evidence_status=evidence_status,
        q=q, payment_method=payment_method,
        payment_card_holder_id=payment_card_holder_id,
        limit=limit, offset=offset,
    )
    return envelope(
        [i.model_dump() for i in items],
        meta={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/{expense_id}")
def detail(
    expense_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(get_expense(db, org_id=user.org_id, expense_id=expense_id).model_dump())


@router.post("", status_code=201)
def create(
    body: ExpenseCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = create_expense(db, org_id=user.org_id, actor=user.user_id, body=body)
    return envelope(out.model_dump())


@router.patch("/{expense_id}")
def update(
    expense_id: UUID,
    body: ExpenseUpdate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    out = update_expense(
        db, org_id=user.org_id, actor=user.user_id, expense_id=expense_id, body=body,
    )
    return envelope(out.model_dump())


@router.get("/{expense_id}/lock-state")
def lock_state(
    expense_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """이 비용을 지금 고칠 수 있는가.

    화면이 저장 버튼을 누르기 **전에** 알아야 한다. 눌러서 409 를 맞고 나서
    "조정을 만드세요" 를 읽으면 이미 입력을 다 끝낸 뒤다.
    """
    from app.core.month_lock import get_close_state, year_month_of
    from app.services.expense import get_expense
    from app.services.expense_adjustment import requires_adjustment

    exp = get_expense(db, org_id=user.org_id, expense_id=expense_id)
    ym = year_month_of(exp.expense_date)
    return envelope({
        "expense_id": str(expense_id),
        "year_month": ym,
        "close_state": get_close_state(db, org_id=user.org_id, year_month=ym),
        # True 면 화면이 "정정 조정 요청" 으로 유도한다.
        "requires_adjustment": requires_adjustment(
            db, org_id=user.org_id, at=exp.expense_date),
    })


@router.get("/{expense_id}/adjustments")
def list_adjustments(
    expense_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """이 비용에 걸린 조정 이력."""
    from app.services.expense_adjustment import list_adjustments as do

    rows = do(db, org_id=user.org_id, original_expense_id=expense_id)
    return envelope([
        {**r, "id": str(r["id"]),
         "original_expense_id": str(r["original_expense_id"]) if r["original_expense_id"] else None,
         "adjustment_expense_id": str(r["adjustment_expense_id"]),
         "approval_request_id": str(r["approval_request_id"]) if r["approval_request_id"] else None,
         "created_by": str(r["created_by"]) if r["created_by"] else None,
         "created_at": r["created_at"].isoformat()}
        for r in rows
    ])


@router.post("/{expense_id}/adjustments", status_code=201)
def create_adjustment(
    expense_id: UUID,
    body: ExpenseAdjustmentCreate,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """잠긴 달 비용의 정정 조정.

    **원본은 건드리지 않는다.** 지금 열린 달에 조정 비용을 만들고 원본을 가리킨다.
    """
    from app.services.expense_adjustment import create_adjustment as do

    return envelope(do(
        db, org_id=user.org_id, actor=user.user_id,
        original_expense_id=expense_id,
        adjustment_type=body.adjustment_type,
        adjustment_reason=body.adjustment_reason,
        adjustment_date=body.adjustment_date,
        amount_delta=body.amount_delta,
        payload=body.payload,
    ))


@router.delete("/{expense_id}", status_code=204, response_model=None)
def delete(
    expense_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> None:
    delete_expense(db, org_id=user.org_id, actor=user.user_id, expense_id=expense_id)
