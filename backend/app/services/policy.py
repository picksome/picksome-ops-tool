"""정책 조회 + snapshot 영구화.

CLAUDE.md Generalization Principle 박제 원칙의 핵심 구현 모듈.

`resolve_active_policies(ctx, db)`
  - PolicyContext (org_id, at, marketplace, category_path, margin_type, payment_method) 로
    effective 인 정책 row 7종을 조회 → ResolvedPolicies dataclass 반환.
  - 카테고리 매칭은 LIKE prefix (`category_path = ctx OR ctx LIKE category_path || '%'`).
  - tax_policy 는 필수 — 누락 시 즉시 RuntimeError (M002 seed 확인 신호).

`persist_policy_snapshot(owner_kind, owner_id, resolved, ...)`
  - 계산 결과 1건당 policy_snapshot 1행 + policy_snapshot_ref N행 + policy_snapshot_value 1행.
  - owner_kind: MARGIN_EXPECTED / MARGIN_ACTUAL / SETTLEMENT_MONTHLY 중 하나.
  - 이 함수가 호출돼야 과거 시점 계산 재현이 가능 (CLAUDE.md 박제 의무).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.policy import (
    AdCostPolicyRow,
    FulfillmentFeePolicyRow,
    MarketplaceFeePolicyRow,
    PaymentFeePolicyRow,
    PolicyContext,
    PolicySnapshotRef,
    ResolvedPolicies,
    ReturnRiskPolicyRow,
    TargetMarginPolicyRow,
    TaxPolicyRow,
)

# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

_SELECT_MFP = text("""
    SELECT id, marketplace, category_path, fee_type, rate, fixed_amount,
           tier_json, version, reliability_status, effective_from, source_type
      FROM marketplace_fee_policy
     WHERE org_id = :org_id
       AND marketplace = :marketplace
       AND effective_from <= :at
       AND (effective_to IS NULL OR effective_to >= :at)
       AND is_active = TRUE
       AND deleted_at IS NULL
       AND (category_path IS NULL
            OR category_path = :category_path
            OR :category_path LIKE category_path || '%')
     ORDER BY (category_path IS NOT NULL) DESC, effective_from DESC
     LIMIT 1
""")

_SELECT_PFP = text("""
    SELECT id, method, rate, version, reliability_status, effective_from, source_type
      FROM payment_fee_policy
     WHERE org_id = :org_id AND method = :method
       AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
       AND is_active = TRUE AND deleted_at IS NULL
     ORDER BY effective_from DESC LIMIT 1
""")

_SELECT_TAX = text("""
    SELECT id, tax_type, business_type, vat_rate, calculation_method,
           default_vat_inclusion, applies_to, version,
           reliability_status, effective_from, source_type
      FROM tax_policy
     WHERE org_id = :org_id
       AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
       AND is_active = TRUE AND deleted_at IS NULL
     ORDER BY (business_type IS NOT NULL) DESC, effective_from DESC LIMIT 1
""")

_SELECT_FULFILL = text("""
    SELECT id, provider, inbound_fee, outbound_fee, storage_fee_per_day,
           return_handling_fee, extra_json, version,
           reliability_status, effective_from, source_type
      FROM fulfillment_fee_policy
     WHERE org_id = :org_id
       AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
       AND is_active = TRUE AND deleted_at IS NULL
     ORDER BY effective_from DESC LIMIT 1
""")

_SELECT_RETURN_RISK = text("""
    SELECT id, marketplace, category_path, return_rate, version,
           reliability_status, effective_from, source_type
      FROM return_risk_policy
     WHERE org_id = :org_id
       AND (marketplace IS NULL OR marketplace = :marketplace)
       AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
       AND is_active = TRUE AND deleted_at IS NULL
     ORDER BY (marketplace IS NOT NULL) DESC, effective_from DESC LIMIT 1
""")

_SELECT_AD = text("""
    SELECT id, marketplace, category_path, target_roas, default_ad_ratio, version,
           reliability_status, effective_from, source_type
      FROM ad_cost_policy
     WHERE org_id = :org_id AND marketplace = :marketplace
       AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
       AND is_active = TRUE AND deleted_at IS NULL
     ORDER BY effective_from DESC LIMIT 1
""")

_SELECT_TARGET = text("""
    SELECT id, category_path, margin_type, target_rate, warning_lower_rate, version
      FROM target_margin_policy
     WHERE org_id = :org_id
       AND (margin_type IS NULL OR margin_type = :margin_type)
       AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
       AND is_active = TRUE AND deleted_at IS NULL
     ORDER BY (margin_type IS NOT NULL) DESC, effective_from DESC LIMIT 1
""")


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def resolve_active_policies(ctx: PolicyContext, db: Session) -> ResolvedPolicies:
    """effective 정책 7종 조회 → ResolvedPolicies. tax 누락 시 RuntimeError."""
    params = {
        "org_id": str(ctx.org_id),
        "at": ctx.at,
        "marketplace": ctx.marketplace,
        "category_path": ctx.category_path or "",
        "method": ctx.payment_method or "BUSINESS_CARD",
        "margin_type": ctx.margin_type,
    }

    refs: list[PolicySnapshotRef] = []
    snapshot_values: dict[str, Any] = {}

    def grab(table: str, sql, mapper):
        # row → dataclass 변환 + snapshot 박제용 ref/value 동시 수집.
        # mapper 호출 전에 ref/value 를 먼저 쌓는 것은 의도적 — None 반환 시 영향 없음.
        row = db.execute(sql, params).first()
        if row is None:
            return None
        d = _row_to_dict(row)
        refs.append(PolicySnapshotRef(table, UUID(str(d["id"])), int(d["version"])))
        snapshot_values[table] = {k: _jsonable(v) for k, v in d.items()}
        return mapper(d)

    # tax 는 필수 (없으면 예외)
    tax_row = grab("tax_policy", _SELECT_TAX, _map_tax)
    if tax_row is None:
        raise RuntimeError(
            f"tax_policy missing for org={ctx.org_id} at {ctx.at}. M002 seed 누락 확인 필요"
        )

    resolved = ResolvedPolicies(
        tax=tax_row,
        marketplace_fee=(
            grab("marketplace_fee_policy", _SELECT_MFP, _map_mfp) if ctx.marketplace else None
        ),
        payment_fee=grab("payment_fee_policy", _SELECT_PFP, _map_pfp),
        fulfillment=grab("fulfillment_fee_policy", _SELECT_FULFILL, _map_fulfill),
        return_risk=grab("return_risk_policy", _SELECT_RETURN_RISK, _map_rr),
        ad_cost=grab("ad_cost_policy", _SELECT_AD, _map_ad) if ctx.marketplace else None,
        target_margin=grab("target_margin_policy", _SELECT_TARGET, _map_target),
        snapshot_refs=refs,
        snapshot_values_json=snapshot_values,
    )
    return resolved


def persist_policy_snapshot(
    owner_kind: Literal["MARGIN_EXPECTED", "MARGIN_ACTUAL", "SETTLEMENT_MONTHLY"],
    owner_id: UUID,
    resolved: ResolvedPolicies,
    org_id: UUID,
    db: Session,
) -> UUID:
    """계산 결과(owner_kind/owner_id) 와 사용된 정책 묶음을 박제.

    반환 UUID 는 owner 테이블의 snapshot_id 컬럼에 저장 (예: product_margin_expected).
    호출자가 commit 책임.
    """
    row = db.execute(
        text(
            """
            INSERT INTO policy_snapshot (org_id, owner_kind, owner_id)
            VALUES (:org_id, :owner_kind, :owner_id)
            RETURNING id
            """
        ),
        {"org_id": str(org_id), "owner_kind": owner_kind, "owner_id": str(owner_id)},
    ).first()
    snapshot_id = UUID(str(row[0]))

    for ref in resolved.snapshot_refs:
        db.execute(
            text(
                """
                INSERT INTO policy_snapshot_ref (snapshot_id, policy_table, policy_row_id, version)
                VALUES (:sid, :tbl, :rid, :ver)
                """
            ),
            {
                "sid": str(snapshot_id),
                "tbl": ref.policy_table,
                "rid": str(ref.policy_row_id),
                "ver": ref.version,
            },
        )

    db.execute(
        text(
            """
            INSERT INTO policy_snapshot_value (snapshot_id, values_json)
            VALUES (:sid, CAST(:values AS JSONB))
            """
        ),
        {
            "sid": str(snapshot_id),
            "values": json.dumps(resolved.snapshot_values_json, default=_jsonable),
        },
    )
    return snapshot_id


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def _map_mfp(d: dict[str, Any]) -> MarketplaceFeePolicyRow:
    return MarketplaceFeePolicyRow(
        id=UUID(str(d["id"])),
        marketplace=d["marketplace"],
        category_path=d["category_path"],
        fee_type=d["fee_type"],
        rate=Decimal(str(d["rate"])) if d["rate"] is not None else None,
        fixed_amount=int(d["fixed_amount"]) if d["fixed_amount"] is not None else None,
        tier_json=d["tier_json"],
        version=int(d["version"]),
        reliability_status=d["reliability_status"],
        effective_from=d["effective_from"],
        source_type=d["source_type"],
    )


def _map_pfp(d: dict[str, Any]) -> PaymentFeePolicyRow:
    return PaymentFeePolicyRow(
        id=UUID(str(d["id"])),
        method=d["method"],
        rate=Decimal(str(d["rate"])),
        version=int(d["version"]),
        reliability_status=d["reliability_status"],
        effective_from=d["effective_from"],
        source_type=d["source_type"],
    )


def _map_tax(d: dict[str, Any]) -> TaxPolicyRow:
    return TaxPolicyRow(
        id=UUID(str(d["id"])),
        tax_type=d["tax_type"],
        business_type=d["business_type"],
        vat_rate=Decimal(str(d["vat_rate"])),
        calculation_method=d["calculation_method"],
        default_vat_inclusion=d["default_vat_inclusion"],
        applies_to=d["applies_to"],
        version=int(d["version"]),
        reliability_status=d["reliability_status"],
        effective_from=d["effective_from"],
        source_type=d["source_type"],
    )


def _map_fulfill(d: dict[str, Any]) -> FulfillmentFeePolicyRow:
    return FulfillmentFeePolicyRow(
        id=UUID(str(d["id"])),
        provider=d["provider"],
        inbound_fee=int(d["inbound_fee"]) if d["inbound_fee"] is not None else None,
        outbound_fee=int(d["outbound_fee"]) if d["outbound_fee"] is not None else None,
        storage_fee_per_day=(
            int(d["storage_fee_per_day"]) if d["storage_fee_per_day"] is not None else None
        ),
        return_handling_fee=(
            int(d["return_handling_fee"]) if d["return_handling_fee"] is not None else None
        ),
        extra_json=d["extra_json"],
        version=int(d["version"]),
        reliability_status=d["reliability_status"],
        effective_from=d["effective_from"],
        source_type=d["source_type"],
    )


def _map_rr(d: dict[str, Any]) -> ReturnRiskPolicyRow:
    return ReturnRiskPolicyRow(
        id=UUID(str(d["id"])),
        marketplace=d["marketplace"],
        category_path=d["category_path"],
        return_rate=Decimal(str(d["return_rate"])),
        version=int(d["version"]),
        reliability_status=d["reliability_status"],
        effective_from=d["effective_from"],
        source_type=d["source_type"],
    )


def _map_ad(d: dict[str, Any]) -> AdCostPolicyRow:
    return AdCostPolicyRow(
        id=UUID(str(d["id"])),
        marketplace=d["marketplace"],
        category_path=d["category_path"],
        target_roas=Decimal(str(d["target_roas"])) if d["target_roas"] is not None else None,
        default_ad_ratio=(
            Decimal(str(d["default_ad_ratio"])) if d["default_ad_ratio"] is not None else None
        ),
        version=int(d["version"]),
        reliability_status=d["reliability_status"],
        effective_from=d["effective_from"],
        source_type=d["source_type"],
    )


def _map_target(d: dict[str, Any]) -> TargetMarginPolicyRow:
    return TargetMarginPolicyRow(
        id=UUID(str(d["id"])),
        category_path=d["category_path"],
        margin_type=d["margin_type"],
        target_rate=Decimal(str(d["target_rate"])),
        warning_lower_rate=(
            Decimal(str(d["warning_lower_rate"])) if d["warning_lower_rate"] is not None else None
        ),
        version=int(d["version"]),
    )


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date,)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if dataclasses.is_dataclass(v):
        return dataclasses.asdict(v)
    return v
