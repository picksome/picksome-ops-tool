"""월별 정산 라우터 (D-285, docs/22_api_spec.md §7).

상태 전이:
    DRAFT ──confirm──> CONFIRMED ──lock──> LOCKED
                            ▲                 │
                            └──reopen─────────┘ (REOPENED → confirm 으로 복귀)

endpoints:
  POST /settlement/calculate          — preview 계산 (저장 없음, side-effect 0)
  POST /settlement                    — DRAFT 생성 + partner_settlement INSERT
  GET  /settlement                    — year_month/status 필터 목록
  GET  /settlement/{id}               — 상세 + distributions 펼침
  POST /settlement/{id}/confirm       — DRAFT/REOPENED → CONFIRMED
  POST /settlement/{id}/lock          — CONFIRMED → LOCKED + loss_carryover_ledger append
  POST /settlement/{id}/reopen        — CONFIRMED/LOCKED → REOPENED
                                         (D-285: reopen_count ≥ 한도 시 409 REOPEN_LIMIT_EXCEEDED)

권한:
  - write(_write): ADMIN/ACCOUNTING
  - read(_read): + OPERATOR/READONLY

모든 mutation 은 audit_log STATUS_CHANGE/CREATE 기록 + db.commit()."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.settlement_api import (
    AdCostAllocationOut,
    CalculateSettlementRequest,
    ConfirmRequest,
    PartnerDistributionOut,
    ReopenRequest,
    SettlementCalcOut,
    SettlementCreateRequest,
)
from app.services.ad_cost_allocation import allocate_ad_cost_for_period
from app.services.settlement import reopen_settlement
from app.services.settlement_crud import (
    calculate_preview,
    confirm_settlement,
    create_settlement,
    get_settlement,
    list_distributions,
    list_settlements,
    lock_settlement,
)

router = APIRouter(prefix="/settlement", tags=["settlement"])

_read = require_roles("ADMIN", "OPERATOR", "ACCOUNTING", "READONLY")
_write = require_roles("ADMIN", "ACCOUNTING")


@router.post("/calculate")
def calculate(
    body: CalculateSettlementRequest,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    result = calculate_preview(db, org_id=user.org_id, year_month=body.year_month)
    out = SettlementCalcOut(
        year_month=result.year_month,
        total_revenue=result.total_revenue,
        total_direct_cost=result.total_direct_cost,
        total_common_expense=result.total_common_expense,
        net_profit=result.net_profit,
        carryover_offset_applied=result.carryover_offset_applied,
        distributable_amount=result.distributable_amount,
        distributions=[
            PartnerDistributionOut(
                partner_id=d.partner_id,
                distribution_kind=d.distribution_kind,
                amount=d.amount,
                ratio_used=d.ratio_used,
            )
            for d in result.distributions
        ],
        warnings=result.warnings,
    )
    return envelope(out.model_dump())


@router.post("/allocate-ad-cost")
def allocate_ad_cost(
    body: CalculateSettlementRequest,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    """그 달 MARKETING 지출을 ad_cost=0 인 sales_order 에 매출비중 배분.

    /settlement/calculate (preview) 실행 전에 호출하는 걸 권장 — 실마진
    계산이 sales_order.ad_cost 를 그대로 읽기 때문.
    """
    result = allocate_ad_cost_for_period(user.org_id, body.year_month, db)
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="sales_order",
        domain_row_id=user.org_id,  # 배치 작업 — 특정 row 없음, org_id 로 대체
        action="UPDATE",
        actor_user_id=user.user_id,
        after={
            "year_month": result.year_month,
            "orders_updated": result.orders_updated,
            "total_marketing_expense": result.total_marketing_expense,
        },
    )
    db.commit()
    return envelope(
        AdCostAllocationOut(
            year_month=result.year_month,
            total_marketing_expense=result.total_marketing_expense,
            orders_updated=result.orders_updated,
            orders_skipped_existing=result.orders_skipped_existing,
        ).model_dump()
    )


@router.post("", status_code=201)
def create(
    body: SettlementCreateRequest,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    sid = create_settlement(
        db,
        org_id=user.org_id,
        actor=user.user_id,
        year_month=body.year_month,
        memo=body.memo,
    )
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="monthly_settlement",
        domain_row_id=sid,
        action="CREATE",
        actor_user_id=user.user_id,
        after={"year_month": body.year_month},
    )
    db.commit()
    return envelope(get_settlement(db, org_id=user.org_id, settlement_id=sid))


@router.get("")
def list_(
    year_month: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_settlements(
        db, org_id=user.org_id, year_month=year_month, status=status,
        limit=limit, offset=offset,
    )
    return envelope(items, meta={"limit": limit, "offset": offset, "total": total})


@router.get("/{settlement_id}")
def detail(
    settlement_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    row = get_settlement(db, org_id=user.org_id, settlement_id=settlement_id)
    row["distributions"] = list_distributions(db, settlement_id=settlement_id)
    return envelope(row)


@router.post("/{settlement_id}/confirm")
def confirm(
    settlement_id: UUID,
    body: ConfirmRequest,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    confirm_settlement(
        db, org_id=user.org_id, actor=user.user_id,
        settlement_id=settlement_id, note=body.note,
        acknowledged_warnings=body.acknowledged_warnings,
    )
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="monthly_settlement",
        domain_row_id=settlement_id,
        action="STATUS_CHANGE",
        actor_user_id=user.user_id,
        after={"status": "CONFIRMED"},
    )
    db.commit()
    return envelope(get_settlement(db, org_id=user.org_id, settlement_id=settlement_id))


@router.post("/{settlement_id}/lock")
def lock(
    settlement_id: UUID,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    lock_settlement(
        db, org_id=user.org_id, actor=user.user_id, settlement_id=settlement_id,
    )
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="monthly_settlement",
        domain_row_id=settlement_id,
        action="STATUS_CHANGE",
        actor_user_id=user.user_id,
        after={"status": "LOCKED"},
    )
    db.commit()
    return envelope(get_settlement(db, org_id=user.org_id, settlement_id=settlement_id))


@router.post("/{settlement_id}/reopen")
def reopen(
    settlement_id: UUID,
    body: ReopenRequest,
    user: CurrentUser = Depends(_write),
    db: Session = Depends(get_db),
) -> dict:
    reopen_settlement(settlement_id, user.user_id, body.reason, db)
    record_audit(
        db,
        org_id=user.org_id,
        domain_table="monthly_settlement",
        domain_row_id=settlement_id,
        action="STATUS_CHANGE",
        actor_user_id=user.user_id,
        reason=body.reason,
        after={"status": "REOPENED"},
    )
    db.commit()
    return envelope(get_settlement(db, org_id=user.org_id, settlement_id=settlement_id))
