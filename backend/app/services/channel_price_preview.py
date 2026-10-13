"""가격 변경 마진 영향 미리보기.

**새 계산식을 만들지 않는다.** 기존 `calculate_expected_margin` 을 그대로 부른다.
미리보기 전용 계산을 따로 만들면 화면이 보여준 마진과 실제 저장되는 마진이
언젠가 갈라지고, 그건 사용자가 잘못된 근거로 가격을 정한다는 뜻이다.

이 함수가 하는 일은 세 가지뿐이다.
  ① 현재 적용가와 제안가를 각각 엔진에 넣는다
  ② 목표 마진율과 비교한다
  ③ 권한에 따라 민감값을 지운다

결과는 **판단 보조값**이지 실제 가격 데이터의 source of truth 가 아니다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.core.sensitive import can_view_sensitive_diff
from app.domain.channel_price import ChannelPriceIn
from app.services.channel_price import get_listing, resolve_price, today_kst

# 권한이 없을 때 지우는 값. **원가와 역산 가능한 값 전부**다.
# 마진율만 남겨도 판매가를 알면 원가가 역산된다.
_SENSITIVE_KEYS = (
    "cost_amount", "marketplace_fee", "payment_fee", "ad_cost", "fulfillment_fee",
    "return_risk_cost", "vat", "expected_settlement", "expected_net_profit",
    "margin_amount", "margin_rate", "margin_gap_pp", "break_even_price",
)


def _product_context(db: Session, org_id: UUID, product_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT margin_type, cost_amount, category_path
          FROM product
         WHERE id = :p AND org_id = :o AND deleted_at IS NULL
    """), {"p": str(product_id), "o": str(org_id)}).first()
    if row is None:
        raise ValidationError("상품을 찾을 수 없습니다.")
    return {"margin_type": row.margin_type, "cost_amount": row.cost_amount,
            "category_path": row.category_path}


def _variant_cost(db: Session, org_id: UUID, variant_id: UUID | None) -> int | None:
    """옵션별 원가가 있으면 그걸 쓴다. 없으면 상품 원가로 떨어진다."""
    if variant_id is None:
        return None
    return db.execute(text("""
        SELECT cost_amount FROM product_variant
         WHERE id = :v AND org_id = :o AND deleted_at IS NULL
    """), {"v": str(variant_id), "o": str(org_id)}).scalar()


def _run_engine(
    db: Session, *, org_id: UUID, product_id: UUID, variant_id: UUID | None,
    marketplace: str, sale_price: int | None, ctx: dict[str, Any],
) -> dict[str, Any] | None:
    """엔진 1회 실행. 판매가가 없으면(미입력) 계산하지 않는다 — **0 으로 대체하지 않는다.**"""
    if sale_price is None or sale_price <= 0:
        return None

    from app.api.v1.margin import _build_applied_policies
    from app.domain.margin import ExpectedMarginInput
    from app.services.margin import calculate_expected_margin

    cost = _variant_cost(db, org_id, variant_id)
    if cost is None:
        cost = ctx["cost_amount"]
    if cost is None:
        # 원가를 모르면 마진을 계산할 수 없다. 0 으로 가정하면 마진이 부풀어
        # 사용자가 실제보다 좋은 숫자를 보고 가격을 정한다.
        return {"unavailable_reason": "원가가 입력되지 않아 마진을 계산할 수 없습니다."}

    result = calculate_expected_margin(
        ExpectedMarginInput(
            org_id=org_id, product_id=product_id, product_variant_id=variant_id,
            margin_type=ctx["margin_type"], marketplace=marketplace,
            cost_amount=cost, sale_price=sale_price,
            payment_method=None, extras={}, override_fields={},
        ),
        db,
    )

    deductions = (result.marketplace_fee + result.payment_fee + result.ad_cost
                  + result.fulfillment_fee + result.return_risk_cost)
    return {
        "sale_price": sale_price,
        "cost_amount": cost,
        "marketplace_fee": result.marketplace_fee,
        "payment_fee": result.payment_fee,
        "ad_cost": result.ad_cost,
        "fulfillment_fee": result.fulfillment_fee,
        "return_risk_cost": result.return_risk_cost,
        "vat": result.vat,
        # 정산액 = 판매가 - 판매처가 떼어가는 것들. 세금은 따로 본다.
        "expected_settlement": sale_price - deductions,
        "expected_net_profit": result.margin_amount,
        "margin_rate": str(result.margin_rate),
        # 라우터와 **같은 직렬화 함수**를 쓴다. 여기서 따로 만들면
        # 화면이 참조하는 정책 표시가 두 종류가 된다.
        "applied_policies": {
            k: v.model_dump(mode="json")
            for k, v in _build_applied_policies(result.applied_policies).items()
        },
        "warnings": list(result.warnings),
    }


def _target_margin_rate(db: Session, org_id: UUID, at: date) -> Decimal | None:
    row = db.execute(text("""
        SELECT target_rate FROM target_margin_policy
         WHERE org_id = :o AND deleted_at IS NULL AND is_active
           AND effective_from <= :at AND (effective_to IS NULL OR effective_to > :at)
         ORDER BY effective_from DESC, version DESC LIMIT 1
    """), {"o": str(org_id), "at": at}).first()
    return row.target_rate if row else None


def preview_price_change(
    db: Session, *, org_id: UUID, listing_id: UUID, payload: ChannelPriceIn,
    role: str, at: date | None = None,
) -> dict[str, Any]:
    """가격 변경 전후 마진 영향.

    **판단 보조값이다.** 실제 적용값의 source of truth 가 아니며, 승인 요청에
    저장한다면 계산 시점과 정책 버전을 함께 남겨야 한다.
    """
    listing = get_listing(db, org_id=org_id, listing_id=listing_id)
    at = at or today_kst()
    ctx = _product_context(db, org_id, listing.product_id)
    marketplace = listing.marketplace or "COUPANG"

    current = resolve_price(db, org_id=org_id, listing_id=listing_id,
                            variant_id=payload.product_variant_id, at=at)

    # 제안가: 할인 기간이 계산 기준일을 포함하면 할인가, 아니면 정상가.
    proposed_price = payload.regular_price
    if payload.sale_price is not None and payload.sale_start_at is not None:
        started = payload.sale_start_at.date() <= at
        not_ended = payload.sale_end_at is None or payload.sale_end_at.date() > at
        if started and not_ended:
            proposed_price = payload.sale_price

    before = _run_engine(db, org_id=org_id, product_id=listing.product_id,
                         variant_id=payload.product_variant_id, marketplace=marketplace,
                         sale_price=current.resolved_price, ctx=ctx)
    after = _run_engine(db, org_id=org_id, product_id=listing.product_id,
                        variant_id=payload.product_variant_id, marketplace=marketplace,
                        sale_price=proposed_price, ctx=ctx)

    target = _target_margin_rate(db, org_id, at)
    gap_pp: str | None = None
    meets_target: bool | None = None
    if target is not None and after and "margin_rate" in after:
        gap = (Decimal(after["margin_rate"]) - target) * 100
        gap_pp = str(gap.quantize(Decimal("0.1")))
        meets_target = gap >= 0

    # 정책이 확정되지 않았으면 결과가 예상값임을 알려야 한다.
    warnings: list[str] = []
    for side in (before, after):
        if not side:
            continue
        warnings.extend(side.get("warnings", []))
        # 정책이 자동 수집됐고 아직 사람이 확인하지 않았으면 결과는 예상값이다.
        for key, pol in (side.get("applied_policies") or {}).items():
            if pol.get("reliability_status") not in (None, "VERIFIED", "APPROVED"):
                warnings.append(
                    f"{key} 정책이 검증되지 않았습니다({pol.get('reliability_status')})."
                )

    out: dict[str, Any] = {
        "listing_id": str(listing_id),
        "product_variant_id": (str(payload.product_variant_id)
                               if payload.product_variant_id else None),
        "at": at.isoformat(),
        "marketplace": marketplace,
        "currency": payload.currency,
        "before": before,
        "after": after,
        "target_margin_rate": str(target) if target is not None else None,
        "margin_gap_pp": gap_pp,
        "meets_target": meets_target,
        # 정책 미확정 경고. 화면이 "결과는 예상값입니다" 를 띄우는 근거다.
        "policy_warnings": sorted(set(warnings)),
        "policies_verified": len(warnings) == 0,
        # 계산 시점 — 승인 snapshot 에 저장한다면 이 값이 함께 가야 재현된다.
        "computed_at": at.isoformat(),
    }

    return redact_preview(out, allowed=can_view_sensitive_diff(role))


def redact_preview(preview: dict[str, Any], *, allowed: bool) -> dict[str, Any]:
    """권한이 없으면 **원가와 역산 가능한 값을 전부 지운다.**

    마진율만 남겨도 판매가를 알면 원가가 나온다. 키는 남겨서
    "값이 없음" 과 "볼 수 없음" 이 구분되게 한다.
    """
    if allowed:
        return {**preview, "sensitive_visible": True}

    out = dict(preview)
    for side in ("before", "after"):
        block = out.get(side)
        if isinstance(block, dict):
            out[side] = {
                k: (None if k in _SENSITIVE_KEYS else v) for k, v in block.items()
            }
    for k in ("margin_gap_pp", "target_margin_rate"):
        out[k] = None
    out["meets_target"] = None
    out["sensitive_visible"] = False
    out["redacted_message"] = "권한이 없어 마진 정보를 확인할 수 없습니다."
    return out
