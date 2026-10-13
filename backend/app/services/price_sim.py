"""등록가 시뮬레이션 (US-103).

target_rates 별로 "이 마진율을 달성하려면 얼마에 팔아야 하는가" 역산.

흐름:
    1. PolicyContext → resolve_active_policies (정책 7종)
    2. target_rate 별로 reverse_calc_price_for_target_margin (1차 방정식)
    3. 역산된 권장가로 compute_expected_margin 재실행 (검증용 — 실제 마진율이 target 과 일치하는지)
    4. PriceSimRow 리스트 반환
"""

from datetime import date

from sqlalchemy.orm import Session

from app.domain.margin import (
    ExpectedMarginInput,
    PriceSimInput,
    PriceSimRow,
)
from app.domain.policy import PolicyContext
from app.services.calculation import (
    compute_expected_margin,
    reverse_calc_price_for_target_margin,
)
from app.services.policy import resolve_active_policies


def simulate_recommended_prices(
    input: PriceSimInput, db: Session, at: date | None = None
) -> list[PriceSimRow]:
    """target_rates 각각에 대해 권장가 + 검증용 expected_margin 산출."""
    ctx = PolicyContext(
        org_id=input.org_id,
        at=at or date.today(),
        marketplace=input.marketplace,
        margin_type=input.margin_type,
        payment_method=input.payment_method,
    )
    policies = resolve_active_policies(ctx, db)

    rows: list[PriceSimRow] = []
    for target in input.target_rates:
        price = reverse_calc_price_for_target_margin(
            target, input.cost_amount, input.margin_type, policies, input.extras
        )
        # product_id 는 시뮬레이션에서 의미 없음 — org_id 를 placeholder 로 재사용.
        # compute_expected_margin 은 product_id 를 read 하지 않으므로 안전.
        margin = compute_expected_margin(
            ExpectedMarginInput(
                org_id=input.org_id,
                product_id=None,  # 시뮬레이션은 product 비종속
                margin_type=input.margin_type,
                marketplace=input.marketplace,
                cost_amount=input.cost_amount,
                sale_price=price,
                payment_method=input.payment_method,
                extras=input.extras,
            ),
            policies,
        )
        rows.append(
            PriceSimRow(target_rate=target, recommended_price=price, expected_margin=margin)
        )
    return rows
