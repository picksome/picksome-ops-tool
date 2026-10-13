"""현금흐름 라우터 (Phase 6).

권한
  조회        ADMIN / ACCOUNTING
  기준 잔액   ADMIN / ACCOUNTING
  수동 기록   ADMIN / ACCOUNTING

**현금흐름은 지급 장부와 같은 등급의 민감 정보다.** 통장 잔액과 파트너 지급액이
그대로 드러나므로 OPERATOR·SOURCING·READONLY 는 보지 못한다 — 지급 장부
(`/payouts`) 와 같은 기준이다.

이 라우터는 원장을 만들지 않는다
-----------------------------
현금흐름은 `payout_payment` 등 기존 source 를 읽어 **계산**한 결과다
(`services/cashflow.py`, `docs/54_cashflow_semantics.md`).
쓰기가 있는 것은 사람이 직접 입력해야만 알 수 있는 두 가지뿐이다:
기준 잔액과 수동 현금 기록.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services import cashflow as cf

router = APIRouter(prefix="/cashflow", tags=["cashflow"])

_read = require_roles(*cf.VIEW_ROLES)
_write = require_roles(*cf.VIEW_ROLES)


@router.get("")
def get_cashflow(
    preset: str | None = Query(None, description="30d | 60d | 90d"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """기간 요약 + 날짜별 타임라인 + 출처별 원본 줄.

    `balance_available=false` 면 기준 잔액이 없어 잔액을 계산할 수 없다는 뜻이다.
    화면은 이때 0원이 아니라 "기준 잔액 미설정" 을 보여준다.
    """
    return envelope(cf.build_summary(
        db, org_id=user.org_id, preset=preset, date_from=date_from, date_to=date_to,
    ))


# ──────────────────────────────────────────────────────────────
# 기준 잔액
# ──────────────────────────────────────────────────────────────

class BaselineIn(BaseModel):
    as_of_date: date
    # 음수 허용 — 마이너스 통장을 쓸 수 있다.
    amount: int
    memo: str | None = Field(None, max_length=200)


@router.get("/baseline")
def get_baseline(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """가장 최근 기준 잔액. 입력한 적이 없으면 `null`."""
    return envelope(cf.get_baseline(db, org_id=user.org_id))


@router.put("/baseline")
def put_baseline(
    payload: BaselineIn,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """기준 잔액 입력/수정. 같은 날짜는 덮어쓴다."""
    return envelope(cf.set_baseline(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        as_of_date=payload.as_of_date, amount=payload.amount, memo=payload.memo,
    ))


# ──────────────────────────────────────────────────────────────
# 수동 현금 기록
# ──────────────────────────────────────────────────────────────

class ManualEventIn(BaseModel):
    direction: str = Field(..., description="INFLOW | OUTFLOW")
    status: str = Field(..., description="EXPECTED | CONFIRMED | ACTUAL")
    # 항상 양수. 방향은 direction 이 정한다.
    amount: int = Field(..., gt=0)
    occurred_at: date | None = None
    expected_at: date | None = None
    category: str | None = Field(None, max_length=60)
    memo: str | None = Field(None, max_length=300)
    source_type: str | None = None
    source_id: UUID | None = None


class ManualEventDeleteIn(BaseModel):
    # 사유 없이 지우면 나중에 금액이 왜 달라졌는지 설명할 수 없다.
    reason: str = Field(..., min_length=1, max_length=300)


@router.get("/manual-events")
def list_manual_events(
    date_from: date = Query(...),
    date_to: date = Query(...),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    rows = cf.list_manual_events(
        db, org_id=user.org_id, date_from=date_from, date_to=date_to,
    )
    return envelope(rows, {"total": len(rows)})


@router.post("/manual-events", status_code=201)
def create_manual_event(
    payload: ManualEventIn,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """수동 현금 기록.

    은행 API 가 없으므로 **입금 쪽에서 ACTUAL 을 만들 수 있는 유일한 경로**다.
    이미 지급 기록이 있는 건을 또 넣으면 409 — 출금이 두 배로 잡히기 때문이다.
    """
    return envelope(cf.create_manual_event(
        db, org_id=user.org_id, actor_user_id=user.user_id,
        direction=payload.direction, status=payload.status, amount=payload.amount,
        occurred_at=payload.occurred_at, expected_at=payload.expected_at,
        category=payload.category, memo=payload.memo,
        source_type=payload.source_type, source_id=payload.source_id,
    ))


@router.delete("/manual-events/{event_id}")
def delete_manual_event(
    event_id: UUID,
    payload: ManualEventDeleteIn,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """행을 지우지 않고 취소 표시. 무엇을 지웠는지 남아야 한다
    (지급 기록 취소 `DELETE /payouts/payments/{id}` 와 같은 모양)."""
    cf.delete_manual_event(
        db, org_id=user.org_id, actor_user_id=user.user_id, event_id=event_id,
        reason=payload.reason,
    )
    return envelope({"deleted": True})
