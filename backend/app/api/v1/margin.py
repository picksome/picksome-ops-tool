"""마진 라우터 (docs/22_api_spec.md §5).

endpoints:
  POST /margin/calculate     — expected margin 계산. save=True 면 product_margin_expected
                                INSERT + 정책 snapshot 박제 + audit(CREATE)
  POST /margin/simulate      — 목표 마진율 배열 → 권장 등록가 역산
  POST /margin/actual        — sales_order 실현 마진. save=True 면 product_margin_actual
                                UPSERT(sales_order_id 기준) + audit(CREATE) — settlement.py
                                의 direct_cost 집계가 이 테이블을 읽으므로 월정산 반영 전제조건
  GET  /margin/expected      — 저장된 expected margin 목록 (product_id/marketplace 필터)
  GET  /margin/expected/{id} — 저장된 expected margin 상세 (snapshot_id 포함)

권한:
  - calc(_calc): ADMIN/OPERATOR/SOURCING/ACCOUNTING
  - read(_read): + READONLY

박제 원칙(D-283): save=True 시 services.policy.persist_policy_snapshot 가
정책 버전을 snapshot 으로 박제하여 과거 시점 재현 가능."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError, ValidationError
from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.domain.margin import (
    ActualMarginInput,
    ExpectedMarginInput,
    PriceSimInput,
)
from app.domain.margin_api import (
    ActualMarginOut,
    AppliedPolicyOut,
    CalculateActualRequest,
    CalculateExpectedRequest,
    ExpectedMarginOut,
    ExpectedRowOut,
    SimulateRequest,
    SimulateRow,
)
from app.domain.policy import ResolvedPolicies
from app.services.margin import (
    calculate_actual_margin,
    calculate_expected_margin,
    save_actual_margin,
    save_expected_margin,
)
from app.services.price_sim import simulate_recommended_prices
from app.services.sale_channel import resolve_sale_channel

router = APIRouter(prefix="/margin", tags=["margin"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")
_calc = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING")

_POLICY_FIELD_TO_KEY = {
    "marketplace_fee": "marketplace_fee_policy",
    "payment_fee": "payment_fee_policy",
    "tax": "tax_policy",
    "fulfillment": "fulfillment_fee_policy",
    "return_risk": "return_risk_policy",
    "ad_cost": "ad_cost_policy",
}


def _build_applied_policies(resolved: ResolvedPolicies) -> dict[str, AppliedPolicyOut]:
    """ResolvedPolicies → FE PolicyAutoField 가 참조하는 dict (table/version/effective_from/...)."""
    out: dict[str, AppliedPolicyOut] = {}
    for attr, key in _POLICY_FIELD_TO_KEY.items():
        row = getattr(resolved, attr)
        if row is None:
            continue
        out[key] = AppliedPolicyOut(
            table=key,
            version=row.version,
            effective_from=row.effective_from,
            reliability_status=row.reliability_status,
            source_type=row.source_type,
        )
    return out


@router.post("/calculate")
def calculate(
    body: CalculateExpectedRequest,
    user: CurrentUser = Depends(_calc),
    db: Session = Depends(get_db),
) -> dict:
    if body.save and body.product_id is None:
        raise ValidationError("save=True 시 product_id 가 필요합니다.")
    resolve_sale_channel(db, org_id=user.org_id, code=body.marketplace)

    input_obj = ExpectedMarginInput(
        org_id=user.org_id,
        product_id=body.product_id,
        product_variant_id=body.product_variant_id,
        margin_type=body.margin_type,
        marketplace=body.marketplace,
        cost_amount=body.cost_amount,
        sale_price=body.sale_price,
        payment_method=body.payment_method,
        extras=body.extras,
        override_fields=body.override_fields,
    )
    result = calculate_expected_margin(input_obj, db)

    saved_id: UUID | None = None
    if body.save:
        saved_id = save_expected_margin(input_obj, result, db)
        record_audit(
            db,
            org_id=user.org_id,
            domain_table="product_margin_expected",
            domain_row_id=saved_id,
            action="CREATE",
            actor_user_id=user.user_id,
            after={"product_id": str(body.product_id), "margin_rate": str(result.margin_rate)},
        )
        db.commit()

    out = ExpectedMarginOut(
        marketplace_fee=result.marketplace_fee,
        payment_fee=result.payment_fee,
        ad_cost=result.ad_cost,
        fulfillment_fee=result.fulfillment_fee,
        return_risk_cost=result.return_risk_cost,
        vat=result.vat,
        margin_amount=result.margin_amount,
        margin_rate=result.margin_rate,
        applied_policies=_build_applied_policies(result.applied_policies),
        warnings=result.warnings,
        saved_id=saved_id,
    )
    return envelope(out.model_dump())


@router.post("/simulate")
def simulate(
    body: SimulateRequest,
    user: CurrentUser = Depends(_calc),
    db: Session = Depends(get_db),
) -> dict:
    input_obj = PriceSimInput(
        org_id=user.org_id,
        margin_type=body.margin_type,
        marketplace=body.marketplace,
        cost_amount=body.cost_amount,
        extras=body.extras,
        payment_method=body.payment_method,
        target_rates=body.target_rates,
    )
    rows = simulate_recommended_prices(input_obj, db)
    out = [
        SimulateRow(
            target_rate=r.target_rate,
            recommended_price=r.recommended_price,
            expected_margin=ExpectedMarginOut(
                marketplace_fee=r.expected_margin.marketplace_fee,
                payment_fee=r.expected_margin.payment_fee,
                ad_cost=r.expected_margin.ad_cost,
                fulfillment_fee=r.expected_margin.fulfillment_fee,
                return_risk_cost=r.expected_margin.return_risk_cost,
                vat=r.expected_margin.vat,
                margin_amount=r.expected_margin.margin_amount,
                margin_rate=r.expected_margin.margin_rate,
                warnings=r.expected_margin.warnings,
            ),
        )
        for r in rows
    ]
    return envelope([o.model_dump() for o in out])


@router.post("/actual")
def actual(
    body: CalculateActualRequest,
    user: CurrentUser = Depends(_calc),
    db: Session = Depends(get_db),
) -> dict:
    input_obj = ActualMarginInput(
        org_id=user.org_id,
        sales_order_id=body.sales_order_id,
        settlement_record_id=body.settlement_record_id,
    )
    result = calculate_actual_margin(input_obj, db)

    saved_id: UUID | None = None
    if body.save:
        saved_id = save_actual_margin(input_obj, result, db)
        record_audit(
            db,
            org_id=user.org_id,
            domain_table="product_margin_actual",
            domain_row_id=saved_id,
            action="CREATE",
            actor_user_id=user.user_id,
            after={
                "sales_order_id": str(body.sales_order_id),
                "margin_rate": str(result.margin_rate),
            },
        )
        db.commit()

    out = ActualMarginOut(
        revenue=result.revenue,
        cost=result.cost,
        marketplace_fee=result.marketplace_fee,
        payment_fee=result.payment_fee,
        ad_cost=result.ad_cost,
        fulfillment_fee=result.fulfillment_fee,
        return_cost=result.return_cost,
        vat=result.vat,
        margin_amount=result.margin_amount,
        margin_rate=result.margin_rate,
        matched_expected_id=result.matched_expected_id,
        delta_margin_amount=result.delta_margin_amount,
        delta_margin_rate=result.delta_margin_rate,
        warnings=result.warnings,
        saved_id=saved_id,
    )
    return envelope(out.model_dump())


@router.get("/expected")
def expected_list(
    product_id: UUID | None = Query(default=None),
    marketplace: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    where = ["org_id = :o", "deleted_at IS NULL"]
    params: dict = {"o": str(user.org_id)}
    if product_id:
        where.append("product_id = :p")
        params["p"] = str(product_id)
    if marketplace:
        where.append("marketplace = :m")
        params["m"] = marketplace
    where_sql = " AND ".join(where)

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM product_margin_expected WHERE {where_sql}"),
        params,
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(f"""
          SELECT id, product_id, product_variant_id, margin_type, marketplace,
                 input_cost, input_sale_price, computed_margin_amount, computed_margin_rate,
                 valid_from, snapshot_id, created_at
            FROM product_margin_expected
           WHERE {where_sql}
           ORDER BY valid_from DESC, created_at DESC
           LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    ).all()
    items = [ExpectedRowOut(**r._mapping) for r in rows]
    return envelope(
        [i.model_dump() for i in items],
        meta={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/expected/{margin_id}")
def expected_detail(
    margin_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    row = db.execute(
        text("""
          SELECT id, product_id, product_variant_id, margin_type, marketplace,
                 input_cost, input_sale_price, computed_marketplace_fee,
                 computed_payment_fee, computed_ad_cost, computed_fulfillment_fee,
                 computed_return_risk_cost, computed_vat,
                 computed_margin_amount, computed_margin_rate,
                 snapshot_id, override_fields_json, valid_from, valid_to, created_at
            FROM product_margin_expected
           WHERE id = :i AND org_id = :o AND deleted_at IS NULL
        """),
        {"i": str(margin_id), "o": str(user.org_id)},
    ).first()
    if row is None:
        raise NotFoundError("expected margin 행을 찾을 수 없습니다.")
    return envelope(dict(row._mapping))
