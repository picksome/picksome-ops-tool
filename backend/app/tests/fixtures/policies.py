"""테스트용 정책 fixture (DB 없이 dataclass)."""

from decimal import Decimal
from uuid import UUID

from app.domain.policy import (
    AdCostPolicyRow,
    MarketplaceFeePolicyRow,
    PaymentFeePolicyRow,
    PolicySnapshotRef,
    ResolvedPolicies,
    ReturnRiskPolicyRow,
    TaxPolicyRow,
)

_UID = UUID("00000000-0000-0000-0000-000000000001")


def base_policies(
    *,
    mfee_rate: str = "0.1080",
    pfee_rate: str = "0.0250",
    ad_ratio: str | None = "0.0500",
    return_rate: str = "0.0300",
    vat_rate: str = "0.1000",
) -> ResolvedPolicies:
    mfp = MarketplaceFeePolicyRow(
        id=_UID,
        marketplace="COUPANG",
        category_path=None,
        fee_type="RATE",
        rate=Decimal(mfee_rate),
        fixed_amount=None,
        tier_json=None,
        version=1,
        reliability_status="CONFIRMED",
    )
    pfp = PaymentFeePolicyRow(
        id=_UID, method="BUSINESS_CARD", rate=Decimal(pfee_rate), version=1
    )
    tax = TaxPolicyRow(
        id=_UID,
        tax_type="GENERAL",
        business_type="JOINT_BUSINESS",
        vat_rate=Decimal(vat_rate),
        calculation_method="STANDARD_RATIO",
        default_vat_inclusion="INCLUDED",
        applies_to="전사",
        version=1,
    )
    rr = ReturnRiskPolicyRow(
        id=_UID, marketplace="COUPANG", category_path=None, return_rate=Decimal(return_rate), version=1
    )
    ad = (
        AdCostPolicyRow(
            id=_UID, marketplace="COUPANG", category_path=None,
            target_roas=None, default_ad_ratio=Decimal(ad_ratio), version=1,
        )
        if ad_ratio is not None
        else None
    )
    return ResolvedPolicies(
        tax=tax,
        marketplace_fee=mfp,
        payment_fee=pfp,
        ad_cost=ad,
        return_risk=rr,
        snapshot_refs=[PolicySnapshotRef("tax_policy", _UID, 1)],
        snapshot_values_json={},
    )
