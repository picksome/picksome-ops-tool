"""순수 마진 계산 (DB 의존 없음).

테스트 가능한 산술 로직만 모음. DB 로드/저장은 services/margin.py 에서 수행.
모든 금액은 int(원), 비율은 Decimal — float 사용 금지 (CLAUDE.md Calculation Rules).

엑셀의 마진 산식을 명명된 함수로 재작성. 함수 단위로 단위테스트 (tests/unit/test_calculation.py).

핵심 산식 (compute_expected_margin):
    sale_price                                          # 입력
    - marketplace_fee (RATE/FIXED/TIERED)
    - payment_fee     (sale_price * rate)
    - ad_cost         (extras 우선, 정책 default 후순위)
    - fulfillment_fee (margin_type 별 분기)
    - return_risk_cost(sale_price * return_rate)
    - vat             ((sale_price - marketplace_fee) * vat_rate)
    = gross_in
    margin_amount = gross_in - real_cost - vat
    margin_rate   = margin_amount / sale_price

reverse_calc_price_for_target_margin: 위 식의 sale_price 역산 (price_sim).
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.core.money import apply_rate, quantize_rate, to_krw
from app.domain.margin import ExpectedMarginInput, ExpectedMarginResult
from app.domain.policy import (
    MarketplaceFeePolicyRow,
    PaymentFeePolicyRow,
    ResolvedPolicies,
)

ZERO = Decimal(0)


def calc_marketplace_fee(sale_price: int, policy: MarketplaceFeePolicyRow | None) -> int:
    """마켓 수수료. policy 없으면 0."""
    if policy is None:
        return 0
    if policy.fee_type == "RATE":
        return apply_rate(sale_price, policy.rate or ZERO)
    if policy.fee_type == "FIXED":
        return policy.fixed_amount or 0
    if policy.fee_type == "TIERED":
        return apply_tiered_fee(sale_price, policy.tier_json or [])
    raise ValueError(f"unknown fee_type={policy.fee_type}")


def apply_tiered_fee(sale_price: int, tiers: list[dict[str, Any]]) -> int:
    """tier_json = [{min, max?, rate?, fixed?}]. 매칭 첫 row 적용."""
    for tier in tiers:
        lo = int(tier.get("min", 0))
        hi = tier.get("max")
        if sale_price >= lo and (hi is None or sale_price < int(hi)):
            if "rate" in tier:
                return apply_rate(sale_price, Decimal(str(tier["rate"])))
            if "fixed" in tier:
                return int(tier["fixed"])
    return 0


def calc_payment_fee(sale_price: int, policy: PaymentFeePolicyRow | None) -> int:
    """결제 PG 수수료. policy 없으면 0 (예: 계좌이체)."""
    if policy is None:
        return 0
    return apply_rate(sale_price, policy.rate)


def calc_ad_cost(
    sale_price: int,
    extras: dict[str, Any],
    ad_default_ratio: Decimal | None,
) -> int:
    """우선순위: extras.ad_ratio_override > extras.ad_cost(원 직접) > 정책 default."""
    if "ad_cost" in extras:
        return int(extras["ad_cost"])
    ratio_raw = extras.get("ad_ratio_override")
    if ratio_raw is None:
        ratio_raw = ad_default_ratio
    if ratio_raw is None:
        return 0
    return apply_rate(sale_price, Decimal(str(ratio_raw)))


def calc_fulfillment_fee(margin_type: str, policies: ResolvedPolicies, extras: dict[str, Any]) -> int:
    """타입별 분기. GENERAL_RESELL 은 0. ROCKET_GROWTH 는 정책 합산."""
    if margin_type == "GENERAL_RESELL":
        return 0
    pol = policies.fulfillment
    if pol is None:
        return int(extras.get("fulfillment_fee", 0))
    inbound = pol.inbound_fee or 0
    outbound = pol.outbound_fee or 0
    storage_days = int(extras.get("storage_days", 0))
    storage = (pol.storage_fee_per_day or 0) * storage_days
    return inbound + outbound + storage


def calc_return_risk_cost(sale_price: int, return_rate: Decimal | None) -> int:
    """반품 위험 비용 = sale_price * return_rate. 카테고리별 평균 반품률 적용."""
    if return_rate is None:
        return 0
    return apply_rate(sale_price, return_rate)


def calc_vat(taxable_amount: int, vat_rate: Decimal) -> int:
    """taxable_amount * vat_rate. taxable 은 함수 호출자가 결정.

    expected margin 계산에서는 (sale_price - marketplace_fee) * vat_rate 로 단순화 (세무사 검토 필요).
    """
    return apply_rate(taxable_amount, vat_rate)


def calc_total_cost_for_margin_type(
    margin_type: str,
    base_cost: int,
    extras: dict[str, Any],
) -> int:
    """마진 타입별 원가 보정.

    - PURCHASE_AGENCY: 공급가 + 구매대행 수수료 + 국제배송
    - OVERSEAS_DIRECT: 공급가(현지) * 환율 + 관세. 호출자가 환율 적용된 값을 전달하거나
      extras.customs / extras.fx_adjustment 로 조정 가능
    - ROCKET_GROWTH / GENERAL_RESELL: 입력 cost_amount 그대로
    """
    cost = base_cost
    if margin_type == "PURCHASE_AGENCY":
        cost += int(extras.get("agency_fee", 0))
        cost += int(extras.get("intl_shipping", 0))
    elif margin_type == "OVERSEAS_DIRECT":
        cost += int(extras.get("customs", 0))
        cost += int(extras.get("fx_adjustment", 0))
    return cost


def compute_expected_margin(
    input: ExpectedMarginInput, policies: ResolvedPolicies
) -> ExpectedMarginResult:
    """예상 마진 — 순수 함수.

    의사코드 (22_api_spec.md §4-2):
      marketplace_fee = sale_price * mfp.rate / fixed / tiered
      payment_fee     = sale_price * pfp.rate
      ad_cost         = sale_price * (override or default_ad_ratio)
      fulfillment_fee = ffp.inbound + outbound + storage*days
      return_risk_cost= sale_price * return_rate
      vat             = (sale_price - marketplace_fee) * vat_rate
      gross_in        = sale_price - 위 합
      margin_amount   = gross_in - cost - vat
      margin_rate     = margin_amount / sale_price
    """
    sp = input.sale_price
    if sp <= 0:
        raise ValueError("sale_price must be > 0")

    mfee = calc_marketplace_fee(sp, policies.marketplace_fee)
    pfee = calc_payment_fee(sp, policies.payment_fee)
    ad = calc_ad_cost(
        sp, input.extras, policies.ad_cost.default_ad_ratio if policies.ad_cost else None
    )
    fulfill = calc_fulfillment_fee(input.margin_type, policies, input.extras)
    rrisk = calc_return_risk_cost(
        sp, policies.return_risk.return_rate if policies.return_risk else None
    )
    vat_base = sp - mfee
    vat = calc_vat(vat_base, policies.tax.vat_rate)

    real_cost = calc_total_cost_for_margin_type(input.margin_type, input.cost_amount, input.extras)
    gross_in = sp - mfee - pfee - ad - fulfill - rrisk
    margin_amount = gross_in - real_cost - vat
    margin_rate = quantize_rate(Decimal(margin_amount) / Decimal(sp))

    warnings: list[str] = []
    if policies.marketplace_fee and policies.marketplace_fee.reliability_status == "NEEDS_REVIEW":
        warnings.append(
            f"marketplace_fee_policy {policies.marketplace_fee.id} 가 NEEDS_REVIEW 상태"
        )

    # 사용자 override 적용
    if input.override_fields:
        for key, value in input.override_fields.items():
            if key == "marketplace_fee":
                mfee = int(value)
            elif key == "payment_fee":
                pfee = int(value)
            elif key == "ad_cost":
                ad = int(value)
            elif key == "fulfillment_fee":
                fulfill = int(value)
            elif key == "return_risk_cost":
                rrisk = int(value)
            elif key == "vat":
                vat = int(value)
        if "marketplace_fee" in input.override_fields and "vat" not in input.override_fields:
            # 과세표준이 (sale_price - marketplace_fee) 라 수수료를 갈아끼우면 VAT 도
            # 따라 움직여야 한다. 재계산하지 않으면 정책 요율로 계산된 VAT 가 남아
            # override 한 수수료와 앞뒤가 안 맞는 결과가 나온다.
            # vat 를 명시적으로 override 한 경우엔 사용자 값이 우선이므로 건드리지 않는다.
            vat = calc_vat(sp - mfee, policies.tax.vat_rate)
        gross_in = sp - mfee - pfee - ad - fulfill - rrisk
        margin_amount = gross_in - real_cost - vat
        margin_rate = quantize_rate(Decimal(margin_amount) / Decimal(sp))
        warnings.append("사용자 수동 override 적용됨")

    return ExpectedMarginResult(
        marketplace_fee=mfee,
        payment_fee=pfee,
        ad_cost=ad,
        fulfillment_fee=fulfill,
        return_risk_cost=rrisk,
        vat=vat,
        margin_amount=margin_amount,
        margin_rate=margin_rate,
        applied_policies=policies,
        warnings=warnings,
    )


def reverse_calc_price_for_target_margin(
    target_rate: Decimal,
    cost_amount: int,
    margin_type: str,
    policies: ResolvedPolicies,
    extras: dict[str, Any],
) -> int:
    """`margin_rate = target_rate` 가 되는 sale_price 역산.

    정방향 식을 그대로 뒤집는다
    -----------------------
    ```
    margin = sp - mfee - pfee - ad - fulfill - rrisk - cost - vat
    vat    = (sp - mfee) * vat_rate
    ```

    `mfee = sp*mfee_rate + mfee_fixed` 로 두고 정리하면

    ```
    sp * (1 - mfee_rate - pay - ad - ret - vat_eff - target)
        = cost + fulfill + mfee_fixed * (1 - vat_rate)

    vat_eff = vat_rate * (1 - mfee_rate)
    ```

    `mfee_fixed` 에 `(1 - vat_rate)` 가 붙는 이유
    ----------------------------------------
    정액 수수료는 **과세표준을 깎는다**(`vat = (sp - mfee) * vat_rate`).
    그래서 수수료 1,000원은 실질 부담이 1,000원이 아니라
    `1,000 × (1 - 0.1) = 900원` 이다.

    예전에는 이 가중치가 빠져 분자가 과대해졌고, 역산가가 높게 나와 **실제
    마진이 목표를 넘었다**(정액 1,000원·VAT 10%에서 +0.64%p). VAT 0% 일 때만
    우연히 맞았다.

    TIERED 는 구간마다 풀어서 맞는 것을 고른다
    -------------------------------------
    구간별로 요율이 다르므로 식 하나로 풀 수 없다. 각 구간의 요율로 풀고,
    **그 해가 그 구간 안에 들어오는지** 확인한다. 들어오는 것이 답이다.

    어느 구간에서도 성립하지 않으면 값을 지어내지 않고 거절한다. 예전에는
    TIERED 를 통째로 무시해서 수수료가 0인 것처럼 계산했고, 목표 20% 로 역산한
    가격의 실제 마진이 11% 였다 — 판매자는 9%p 를 손해 보면서 그 사실을 모른다.
    """
    pol = policies
    fulfill = calc_fulfillment_fee(margin_type, pol, extras)
    real_cost = calc_total_cost_for_margin_type(margin_type, cost_amount, extras)

    payment_rate = pol.payment_fee.rate if pol.payment_fee else ZERO
    ad_rate_raw = extras.get("ad_ratio_override")
    if ad_rate_raw is None and pol.ad_cost:
        ad_rate_raw = pol.ad_cost.default_ad_ratio
    ad_rate = Decimal(str(ad_rate_raw)) if ad_rate_raw is not None else ZERO
    return_rate = pol.return_risk.return_rate if pol.return_risk else ZERO
    vat_rate = pol.tax.vat_rate

    def solve(mfee_rate: Decimal, mfee_fixed: int) -> Decimal:
        """한 가지 수수료 조합에 대한 해. 분모가 0 이하면 성립하지 않는다."""
        vat_effective = vat_rate * (Decimal(1) - mfee_rate)
        denom = (Decimal(1) - mfee_rate - payment_rate - ad_rate - return_rate
                 - vat_effective - target_rate)
        if denom <= 0:
            raise ValueError(
                f"target_rate {target_rate} 가 정책 비용 합을 초과 — 가격 역산 불가",
            )
        # 정액 수수료는 과세표준을 깎으므로 (1 - vat_rate) 가중치를 준다.
        numer = (Decimal(real_cost) + Decimal(fulfill)
                 + Decimal(mfee_fixed) * (Decimal(1) - vat_rate))
        return numer / denom

    mf = pol.marketplace_fee
    if mf is None:
        return to_krw(solve(ZERO, 0))
    if mf.fee_type == "RATE":
        return to_krw(solve(mf.rate or ZERO, 0))
    if mf.fee_type == "FIXED":
        return to_krw(solve(ZERO, mf.fixed_amount or 0))
    if mf.fee_type == "TIERED":
        return _solve_tiered(mf.tier_json or [], solve)
    raise ValueError(f"unknown fee_type={mf.fee_type}")


def _solve_tiered(
    tiers: list[dict[str, Any]],
    solve: "Callable[[Decimal, int], Decimal]",
) -> int:
    """구간별로 풀고 **자기 구간에 들어오는 해**를 고른다.

    구간 판정은 `apply_tiered_fee` 와 같아야 한다 — `min <= price < max`.
    다르면 역산가로 정방향 계산했을 때 다른 구간이 적용돼 결과가 어긋난다.
    """
    failures: list[str] = []
    for tier in tiers:
        lo = int(tier.get("min", 0))
        hi = tier.get("max")
        rate = Decimal(str(tier["rate"])) if "rate" in tier else ZERO
        fixed = int(tier["fixed"]) if "fixed" in tier else 0
        try:
            price = to_krw(solve(rate, fixed))
        except ValueError as exc:
            failures.append(str(exc))
            continue
        if price >= lo and (hi is None or price < int(hi)):
            return price
    if failures:
        raise ValueError(failures[0])
    # 어느 구간에도 안 맞는다 — 값을 지어내지 않는다.
    raise ValueError(
        "구간별 수수료(TIERED)로는 목표 마진을 만족하는 가격을 찾지 못했습니다. "
        "구간 경계 근처라 해가 자기 구간 밖으로 나갑니다.",
    )
