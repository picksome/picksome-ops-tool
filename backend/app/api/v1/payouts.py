"""확정 수익배분 · 지급 장부 라우터 (Phase 31~34).

권한
  배분 조회      ADMIN / ACCOUNTING / OPERATOR
  배분 확정·승인 ADMIN / ACCOUNTING
  지급 조회      ADMIN / ACCOUNTING
  지급 승인      ADMIN
  지급 기록      ADMIN / ACCOUNTING

**지급 금액은 민감 정보다.** OPERATOR·SOURCING·READONLY 는 지급 장부를 보지 못한다 —
누가 얼마를 가져가는지는 운영 업무에 필요하지 않다.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db, get_request_id
from app.services import distribution as dist
from app.services import payout as payout_svc

router = APIRouter(prefix="/payouts", tags=["payouts"])
distribution_router = APIRouter(prefix="/distributions", tags=["distributions"])

_dist_read = require_roles("ADMIN", "ACCOUNTING", "OPERATOR")
_dist_write = require_roles("ADMIN", "ACCOUNTING")
_pay_read = require_roles("ADMIN", "ACCOUNTING")
_pay_write = require_roles("ADMIN", "ACCOUNTING")
_pay_approve = require_roles("ADMIN")


# ──────────────────────────────────────────────────────────────
# 확정 수익배분
# ──────────────────────────────────────────────────────────────

@distribution_router.get("/{year_month}/preview")
def preview_distribution(
    year_month: str,
    user: CurrentUser = Depends(_dist_read),
    db: Session = Depends(get_db),
) -> dict:
    """확정하면 누가 얼마를 받는지 미리 본다. 저장하지 않는다.

    월이 아직 확정되지 않았으면 `is_provisional=true` 로 내려간다 —
    화면이 이 값을 "확정 배분액" 이라고 부르면 안 된다.
    """
    return envelope(dist.preview(db, org_id=user.org_id, year_month=year_month))


@distribution_router.get("/{year_month}")
def get_distribution(
    year_month: str,
    user: CurrentUser = Depends(_dist_read),
    db: Session = Depends(get_db),
) -> dict:
    """현재 유효한 확정 배분. 없으면 null."""
    return envelope(dist.get_current_run(db, org_id=user.org_id, year_month=year_month))


@distribution_router.get("/{year_month}/history")
def distribution_history(
    year_month: str,
    user: CurrentUser = Depends(_dist_read),
    db: Session = Depends(get_db),
) -> dict:
    """세대 이력. 재오픈으로 배분이 어떻게 달라졌는지 비교할 수 있어야 한다."""
    rows = dist.list_runs(db, org_id=user.org_id, year_month=year_month)
    return envelope(rows, {"total": len(rows)})


@distribution_router.post("/{year_month}")
def create_distribution(
    year_month: str,
    user: CurrentUser = Depends(_dist_write),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    """확정 배분 생성. **월 확정 snapshot 이 있어야 한다.**"""
    return envelope(
        dist.create_run(
            db, org_id=user.org_id, year_month=year_month,
            actor_user_id=user.user_id, request_id=request_id,
        )
    )


@distribution_router.post("/{year_month}/approve")
def approve_distribution(
    year_month: str,
    run_id: UUID = Query(..., description="승인할 배분 run"),
    user: CurrentUser = Depends(_dist_write),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    return envelope(
        dist.approve_run(
            db, org_id=user.org_id, run_id=run_id,
            actor_user_id=user.user_id, request_id=request_id,
        )
    )


# ──────────────────────────────────────────────────────────────
# 지급 장부
# ──────────────────────────────────────────────────────────────

class BatchCreateBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    period_year_month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    memo: str | None = None


class FromDistributionBody(BaseModel):
    due_date: date | None = None


class ItemBody(BaseModel):
    item_type: str
    payee_name: str = Field(min_length=1, max_length=200)
    due_amount: int = Field(gt=0)
    payee_partner_id: UUID | None = None
    source_expense_id: UUID | None = None
    due_date: date | None = None
    memo: str | None = None


class PaymentBody(BaseModel):
    payment_date: date
    amount: int = Field(gt=0)
    payment_reference: str | None = Field(default=None, max_length=200)
    memo: str | None = None
    evidence_attachment_id: UUID | None = None


class CancelPaymentBody(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class ReimbursementBody(BaseModel):
    expense_ids: list[UUID] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    due_date: date | None = None


@router.get("")
def list_batches(
    status: str | None = Query(default=None),
    year_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(_pay_read),
    db: Session = Depends(get_db),
) -> dict:
    rows, total = payout_svc.list_batches(
        db, org_id=user.org_id, status=status, year_month=year_month,
        limit=limit, offset=offset,
    )
    return envelope(rows, {"total": total, "limit": limit, "offset": offset})


@router.post("")
def create_batch(
    body: BatchCreateBody,
    user: CurrentUser = Depends(_pay_write),
    db: Session = Depends(get_db),
) -> dict:
    batch_id = payout_svc.create_batch(
        db, org_id=user.org_id, actor_user_id=user.user_id, title=body.title,
        period_year_month=body.period_year_month, memo=body.memo,
    )
    return envelope(payout_svc.get_batch(db, org_id=user.org_id, batch_id=batch_id))


@router.post("/from-distribution/{year_month}")
def create_from_distribution(
    year_month: str,
    body: FromDistributionBody,
    user: CurrentUser = Depends(_pay_write),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    """승인된 확정 배분에서 지급 배치를 만든다."""
    return envelope(
        payout_svc.create_batch_from_distribution(
            db, org_id=user.org_id, year_month=year_month,
            actor_user_id=user.user_id, due_date=body.due_date, request_id=request_id,
        )
    )


@router.get("/reimbursement-candidates")
def reimbursement_candidates(
    year_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    card_holder_id: UUID | None = Query(default=None),
    user: CurrentUser = Depends(_pay_read),
    db: Session = Depends(get_db),
) -> dict:
    """개인카드로 결제한 사업용 비용 중 아직 상환 항목이 없는 것."""
    rows = payout_svc.list_reimbursement_candidates(
        db, org_id=user.org_id, year_month=year_month, card_holder_id=card_holder_id,
    )
    return envelope(
        rows,
        {"total": len(rows), "total_amount": sum(int(r["gross_amount"]) for r in rows)},
    )


@router.post("/reimbursements")
def create_reimbursement(
    body: ReimbursementBody,
    user: CurrentUser = Depends(_pay_write),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    """개인카드 상환 배치 생성.

    ⚠️ 새 비용을 만들지 않는다. 원본 비용이 이미 손익에 반영되어 있다.
    """
    return envelope(
        payout_svc.create_reimbursement_batch(
            db, org_id=user.org_id, actor_user_id=user.user_id,
            expense_ids=body.expense_ids, title=body.title, due_date=body.due_date,
            request_id=request_id,
        )
    )


@router.get("/{batch_id}")
def get_batch(
    batch_id: UUID,
    user: CurrentUser = Depends(_pay_read),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(payout_svc.get_batch(db, org_id=user.org_id, batch_id=batch_id))


@router.post("/{batch_id}/items")
def add_item(
    batch_id: UUID,
    body: ItemBody,
    user: CurrentUser = Depends(_pay_write),
    db: Session = Depends(get_db),
) -> dict:
    payout_svc.add_item(
        db, org_id=user.org_id, actor_user_id=user.user_id, batch_id=batch_id,
        item_type=body.item_type, payee_name=body.payee_name,
        due_amount=body.due_amount, payee_partner_id=body.payee_partner_id,
        source_expense_id=body.source_expense_id, due_date=body.due_date,
        memo=body.memo,
    )
    return envelope(payout_svc.get_batch(db, org_id=user.org_id, batch_id=batch_id))


@router.post("/{batch_id}/approve")
def approve_batch(
    batch_id: UUID,
    user: CurrentUser = Depends(_pay_approve),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    return envelope(
        payout_svc.approve_batch(
            db, org_id=user.org_id, batch_id=batch_id,
            actor_user_id=user.user_id, request_id=request_id,
        )
    )


@router.get("/items/{item_id}/payments")
def list_payments(
    item_id: UUID,
    user: CurrentUser = Depends(_pay_read),
    db: Session = Depends(get_db),
) -> dict:
    rows = payout_svc.list_payments(db, org_id=user.org_id, item_id=item_id)
    return envelope(rows, {"total": len(rows)})


@router.post("/items/{item_id}/payments")
def record_payment(
    item_id: UUID,
    body: PaymentBody,
    user: CurrentUser = Depends(_pay_write),
    db: Session = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> dict:
    """실제 지급 1건 기록. 부분 지급을 지원하고 과지급은 거부한다."""
    return envelope(
        payout_svc.record_payment(
            db, org_id=user.org_id, actor_user_id=user.user_id, item_id=item_id,
            payment_date=body.payment_date, amount=body.amount,
            payment_reference=body.payment_reference, memo=body.memo,
            evidence_attachment_id=body.evidence_attachment_id, request_id=request_id,
        )
    )


@router.delete("/payments/{payment_id}")
def cancel_payment(
    payment_id: UUID,
    body: CancelPaymentBody,
    user: CurrentUser = Depends(_pay_write),
    db: Session = Depends(get_db),
) -> dict:
    """잘못 입력한 지급 기록 취소. 행을 지우지 않고 취소 표시만 한다."""
    payout_svc.cancel_payment(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        payment_id=payment_id, reason=body.reason,
    )
    return envelope({"cancelled": True})
