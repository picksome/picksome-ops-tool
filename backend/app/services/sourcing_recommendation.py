"""소싱 의사결정 — 예상 원가 비교 · 권장가 · 민감도 (Sourcing Vertical Slice).

새 계산식을 만들지 않는다
---------------------
권장가는 기존 `price_sim.simulate_recommended_prices` 가 계산한다. 그 안에서
`reverse_calc_price_for_target_margin` 과 `compute_expected_margin` 이 돌고,
정책(수수료·결제수수료·세금)은 `resolve_active_policies` 가 가져온다.

여기서 하는 일은 **입력을 만들어 주는 것**뿐이다: 소싱처의 예상 landed cost 를
원가로 넣고, 결과에 근거(어느 원가 version·어느 환율·어느 정책)를 붙인다.

프론트에서 다시 계산하지 않는다 — 두 곳의 산식이 갈라지는 순간 어느 쪽이 맞는지
사용자가 알 수 없다.

가격 구간 이름을 지어내지 않는다
---------------------------
"공격적 판매가" 같은 이름은 근거가 없으면 만들지 않는다. 실제 근거가 있는 것은
`target_margin_policy` 가 가진 두 값뿐이다.

```
break_even          목표 마진 0%          (산식상 정의됨)
minimum_recommended warning_lower_rate    (정책이 정한 하한)
recommended         target_rate           (정책이 정한 목표)
```

정책이 없으면 그 줄은 나오지 않는다. 0% 수수료로 계산하지 않는 것과 같은 이유다.

과거를 건드리지 않는다
------------------
여기 계산은 전부 **읽기 전용**이다. 원가 version 도 환율도 만들지 않는다.
민감도 시나리오는 override 로만 돌린다 — DB 에 가짜 환율이 남으면 안 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.services import sourcing_core as sc

# 민감도 시나리오 기본 증분. **업무 임계값이 아니다** — "얼마 오르면 위험한가" 는
# 사업 판단이고 우리가 정할 수 없다. 그냥 흔히 보는 폭을 보여줄 뿐이다.
DEFAULT_FX_STEPS = (Decimal("0"), Decimal("0.03"), Decimal("0.05"))
DEFAULT_COST_STEPS = (Decimal("0"), Decimal("0.05"), Decimal("0.10"))


@dataclass(slots=True)
class Unavailable:
    """계산할 수 없는 이유. **0 으로 채우지 않는다.**"""

    missing: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"available": False, "missing": self.missing}


def _product_cost(db: Session, *, org_id: UUID, product_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT cost_amount, cost_currency, margin_type, name_ko
          FROM product
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(product_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("상품을 찾을 수 없습니다.")
    return dict(row)


# ──────────────────────────────────────────────────────────────────────────
# 현재 적용 원가와의 비교
# ──────────────────────────────────────────────────────────────────────────

def compare_to_product_cost(
    db: Session, *, org_id: UUID, product_source_id: UUID, at: date | None = None,
) -> dict[str, Any]:
    """예상 landed cost 와 **현재 적용 중인 상품 원가**를 나란히 놓는다.

    둘은 다른 것이다:

    ```
    product_source_cost_version   공급처에서 관측된 사실
    product.cost_amount           지금 계산에 쓰이고 있는 값
    ```

    **자동으로 맞추지 않는다.** 차이를 보여 주고 사람이 판단한다.
    적용하려면 `COST_CHANGE` 승인을 거친다.

    임계값을 만들지 않는다
    ------------------
    "15% 이상이면 위험" 같은 기준은 업무 판단이고 근거가 없다. 차이와 비율만
    사실대로 준다 — 위험 여부는 사람이 정한다.
    """
    src = sc.get_product_source(db, org_id=org_id, source_id=product_source_id)
    product = _product_cost(db, org_id=org_id, product_id=UUID(src["product_id"]))
    landed = sc.compute_landed_cost(
        db, org_id=org_id, product_source_id=product_source_id, at=at,
    )

    current = product["cost_amount"]
    out: dict[str, Any] = {
        "product_id": src["product_id"],
        "product_name": product["name_ko"],
        "supplier_name": src["supplier_name"],
        "current_cost_amount": current,
        "landed_cost": landed.as_dict(),
        "difference": None,
        "difference_rate": None,
    }
    if not landed.is_complete or current is None:
        # 한쪽을 모르면 차이도 모른다. 0 으로 두면 "차이 없음" 으로 읽힌다.
        return out

    diff = landed.total_krw - int(current)
    out["difference"] = diff
    if int(current) != 0:
        out["difference_rate"] = float(
            (Decimal(diff) / Decimal(int(current))).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )
    return out


# ──────────────────────────────────────────────────────────────────────────
# 대안 소싱처 비교
# ──────────────────────────────────────────────────────────────────────────

def compare_sources(
    db: Session, *, org_id: UUID, product_id: UUID, at: date | None = None,
) -> dict[str, Any]:
    """이 상품의 소싱처를 **나란히** 놓는다.

    순위를 매기지 않는다
    -----------------
    "최저 landed cost = 추천 공급처" 로 만들지 않는다. 실제 선택에는 여기 없는
    것들이 들어간다 — MOQ 를 감당할 현금, 리드타임과 시즌, 품질 클레임 이력,
    카드 결제 가능 여부. 숫자 하나로 줄을 세우면 그것들이 사라진다.

    그래서 이 함수는 **점수도 추천도 만들지 않는다.** 값을 사실대로 나란히 주고,
    정렬은 화면이 사용자의 요청에 따라 한다.

    계산 못 하는 칸은 비운다
    --------------------
    환율이나 원가 version 이 없으면 `landed_cost.is_complete` 가 false 다.
    그 자리를 0 으로 채우면 **가장 싼 공급처처럼** 보인다 — 정확히 반대다.
    """
    product = _product_cost(db, org_id=org_id, product_id=product_id)
    sources = sc.list_product_sources(db, org_id=org_id, product_id=product_id)
    current = product["cost_amount"]

    rows: list[dict[str, Any]] = []
    for src in sources:
        landed = sc.compute_landed_cost(
            db, org_id=org_id, product_source_id=UUID(src["id"]), at=at,
        )
        row: dict[str, Any] = {
            "product_source_id": src["id"],
            "supplier_name": src["supplier_name"],
            "supplier_code": src["supplier_code"],
            "country_code": src["country_code"],
            "currency": src["currency"],
            "is_primary": src["is_primary"],
            "status": src["status"],
            "moq": src["moq"],
            "lead_time_days": src["lead_time_days"],
            # 언제 확인한 값인가. 오래된 값과 어제 값을 같게 보이면 안 된다.
            "last_checked_at": (
                src["last_checked_at"].isoformat() if src["last_checked_at"] else None
            ),
            "landed_cost": landed.as_dict(),
            "difference": None,
            "difference_rate": None,
        }
        if landed.is_complete and current is not None:
            diff = landed.total_krw - int(current)
            row["difference"] = diff
            if int(current) != 0:
                row["difference_rate"] = float(
                    (Decimal(diff) / Decimal(int(current))).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                )
        rows.append(row)

    return {
        "product_id": str(product_id),
        "product_name": product["name_ko"],
        # 지금 계산에 실제로 쓰이는 값. 소싱처 원가와 다른 것이다.
        "current_cost_amount": current,
        "sources": rows,
        "comparable_count": sum(1 for r in rows if r["landed_cost"]["is_complete"]),
    }


# ──────────────────────────────────────────────────────────────────────────
# 권장가
# ──────────────────────────────────────────────────────────────────────────

def _target_rates(
    db: Session, *, org_id: UUID, margin_type: str, at: date,
) -> list[tuple[str, Decimal]]:
    """정책이 가진 근거 있는 목표만. 없으면 손익분기 하나뿐이다."""
    rows: list[tuple[str, Decimal]] = [("break_even", Decimal("0"))]

    row = db.execute(text("""
        SELECT target_rate, warning_lower_rate
          FROM target_margin_policy
         WHERE org_id = :org
           AND (margin_type IS NULL OR margin_type = :mt)
           AND effective_from <= :at AND (effective_to IS NULL OR effective_to >= :at)
           AND is_active = TRUE AND deleted_at IS NULL
         ORDER BY (margin_type IS NOT NULL) DESC, effective_from DESC
         LIMIT 1
    """), {"org": str(org_id), "mt": margin_type, "at": at}).mappings().first()

    if row:
        if row["warning_lower_rate"] is not None:
            rows.append(("minimum_recommended", Decimal(str(row["warning_lower_rate"]))))
        if row["target_rate"] is not None:
            rows.append(("recommended", Decimal(str(row["target_rate"]))))
    return rows


def recommend_prices(
    db: Session, *, org_id: UUID, product_source_id: UUID, marketplace: str,
    at: date | None = None, payment_method: str = "BUSINESS_CARD",
    cost_override_krw: int | None = None,
    fx_rate_override: Decimal | None = None,
    extra_target_rates: list[Decimal] | None = None,
) -> dict[str, Any]:
    """소싱 단계 권장 판매가.

    `cost_override_krw` 는 민감도 시나리오용이다 — 실제 원가 version 을 만들지 않는다.
    """
    day = at or date.today()
    src = sc.get_product_source(db, org_id=org_id, source_id=product_source_id)
    product = _product_cost(db, org_id=org_id, product_id=UUID(src["product_id"]))
    margin_type = product["margin_type"] or "GENERAL"

    if cost_override_krw is not None:
        cost = cost_override_krw
        landed_dict: dict[str, Any] | None = None
        cost_version_id = None
        fx_used = None
    else:
        landed = sc.compute_landed_cost(
            db, org_id=org_id, product_source_id=product_source_id, at=day,
            fx_rate_override=fx_rate_override,
        )
        if not landed.is_complete:
            # 원가를 모르면 권장가를 낼 수 없다. 0 으로 계산하면 "얼마에 팔아도
            # 남는다" 가 나오고 사용자는 그 값으로 가격을 정한다.
            return {"available": False, "missing": landed.missing,
                    "landed_cost": landed.as_dict()}
        cost = landed.total_krw
        landed_dict = landed.as_dict()
        fx_used = landed.fx
        cv = sc.cost_version_at(db, org_id=org_id,
                                product_source_id=product_source_id, at=day)
        cost_version_id = cv["id"] if cv else None

    targets = _target_rates(db, org_id=org_id, margin_type=margin_type, at=day)
    for extra in extra_target_rates or []:
        targets.append((f"target_{extra}", Decimal(str(extra))))

    from app.domain.margin import PriceSimInput
    from app.services.price_sim import simulate_recommended_prices

    try:
        rows = simulate_recommended_prices(
            PriceSimInput(
                org_id=org_id, margin_type=margin_type, marketplace=marketplace,
                cost_amount=cost, payment_method=payment_method,
                target_rates=[r for _, r in targets],
            ),
            db, at=day,
        )
    except RuntimeError as exc:
        # 세금 정책이 없으면 마진 엔진이 계산을 거부한다 — 맞는 동작이다.
        # 0% 세율로 계산하면 "이 값에 팔면 남는다" 가 나오고 그건 거짓이다.
        # 500 으로 터뜨리지 말고 "계산 불가 + 이유" 로 내려보낸다.
        return {"available": False, "missing": [_missing_policy_code(exc)],
                "detail": str(exc)}

    prices = [
        {
            "key": key,
            "target_rate": str(rate),
            "recommended_price": row.recommended_price,
            "expected_margin_amount": row.expected_margin.margin_amount,
            "expected_margin_rate": str(row.expected_margin.margin_rate),
        }
        for (key, rate), row in zip(targets, rows, strict=True)
    ]

    return {
        "available": True,
        "cost_krw": cost,
        "marketplace": marketplace,
        "margin_type": margin_type,
        "prices": prices,
        "landed_cost": landed_dict,
        # 나중에 "왜 권장가가 달라졌나" 에 답할 수 있어야 한다.
        "basis": _basis(db, org_id=org_id, margin_type=margin_type, marketplace=marketplace,
                        at=day, payment_method=payment_method,
                        cost_version_id=cost_version_id, fx=fx_used,
                        cost_overridden=cost_override_krw is not None),
    }


def _missing_policy_code(exc: Exception) -> str:
    """어떤 정책이 없어서 계산을 못 했는지. 화면이 "무엇을 등록하세요" 를 말할 수 있게."""
    text_ = str(exc)
    for name in ("tax_policy", "marketplace_fee_policy", "payment_fee_policy"):
        if name in text_:
            return f"POLICY:{name}"
    return "POLICY:UNKNOWN"


def _basis(
    db: Session, *, org_id: UUID, margin_type: str, marketplace: str, at: date,
    payment_method: str, cost_version_id: str | None, fx: dict[str, Any] | None,
    cost_overridden: bool,
) -> dict[str, Any]:
    """이 계산이 무엇을 근거로 했는가.

    정책 version 을 남기지 않으면 한 달 뒤 같은 화면이 다른 값을 보여줄 때
    무엇이 바뀌었는지 설명할 수 없다.
    """
    from app.domain.policy import PolicyContext
    from app.services.policy import resolve_active_policies

    ctx = PolicyContext(
        org_id=org_id, at=at, marketplace=marketplace,
        margin_type=margin_type, payment_method=payment_method,
    )
    policies = resolve_active_policies(ctx, db)

    return {
        "as_of": at.isoformat(),
        "source_cost_version_id": cost_version_id,
        "cost_overridden": cost_overridden,
        "fx": (
            {
                "rate": str(fx["rate"]),
                "source_type": fx.get("source_type"),
                "observed_at": fx["observed_at"].isoformat() if fx.get("observed_at") else None,
            }
            if fx else None
        ),
        # 어느 정책 행을 썼는지. 정책이 없으면 그 자리가 비어 있다는 사실 자체가 근거다.
        "policy_versions": [
            {"policy_table": r.policy_table, "policy_row_id": str(r.policy_row_id),
             "version": r.version}
            for r in policies.snapshot_refs
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# 민감도
# ──────────────────────────────────────────────────────────────────────────

def _bump(value: int, pct: Decimal) -> int:
    return int((Decimal(value) * (Decimal("1") + pct)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ))


def fx_sensitivity(
    db: Session, *, org_id: UUID, product_source_id: UUID, marketplace: str,
    sale_price: int, at: date | None = None,
    steps: tuple[Decimal, ...] = DEFAULT_FX_STEPS,
) -> dict[str, Any]:
    """환율이 오르면 이 판매가에서 마진이 얼마나 줄어드나.

    **시나리오다.** `fx_rate_snapshot` 에 아무것도 쓰지 않는다 — 가짜 환율이
    DB 에 남으면 나중에 그게 실제 관측치처럼 보인다.
    """
    day = at or date.today()
    cost = sc.cost_version_at(db, org_id=org_id, product_source_id=product_source_id, at=day)
    if cost is None:
        return Unavailable(["SOURCE_COST"]).as_dict()

    currency = cost["currency"]
    if currency == "KRW":
        # 원화 소싱은 환율 민감도가 없다. 억지로 시나리오를 만들지 않는다.
        return {"available": False, "missing": ["NOT_FOREIGN_CURRENCY"],
                "currency": currency}

    base_fx = sc.latest_fx_rate(db, org_id=org_id, base_currency=currency,
                                quote_currency="KRW")
    if base_fx is None:
        return Unavailable([f"FX_RATE:{currency}/KRW"]).as_dict()

    base_rate = Decimal(str(base_fx["rate"]))
    scenarios = []
    for step in steps:
        rate = (base_rate * (Decimal("1") + step)).quantize(Decimal("0.000001"))
        row = _margin_at(db, org_id=org_id, product_source_id=product_source_id,
                         marketplace=marketplace, sale_price=sale_price, at=day,
                         fx_rate_override=rate)
        scenarios.append({"step": str(step), "rate": str(rate), **row})

    return {"available": True, "currency": currency,
            "base_rate": str(base_rate), "scenarios": scenarios}


def cost_sensitivity(
    db: Session, *, org_id: UUID, product_source_id: UUID, marketplace: str,
    sale_price: int, at: date | None = None,
    steps: tuple[Decimal, ...] = DEFAULT_COST_STEPS,
) -> dict[str, Any]:
    """원가가 오르면 이 판매가에서 마진이 얼마나 줄어드나. 시나리오다."""
    day = at or date.today()
    landed = sc.compute_landed_cost(db, org_id=org_id,
                                    product_source_id=product_source_id, at=day)
    if not landed.is_complete:
        return Unavailable(landed.missing).as_dict()

    scenarios = []
    for step in steps:
        bumped = _bump(landed.total_krw, step)
        row = _margin_at(db, org_id=org_id, product_source_id=product_source_id,
                         marketplace=marketplace, sale_price=sale_price, at=day,
                         cost_override_krw=bumped)
        scenarios.append({"step": str(step), "cost_krw": bumped, **row})

    return {"available": True, "base_cost_krw": landed.total_krw, "scenarios": scenarios}


def _margin_at(
    db: Session, *, org_id: UUID, product_source_id: UUID, marketplace: str,
    sale_price: int, at: date,
    fx_rate_override: Decimal | None = None, cost_override_krw: int | None = None,
) -> dict[str, Any]:
    """한 시나리오의 마진. 기존 엔진을 그대로 쓴다."""
    from app.domain.margin import ExpectedMarginInput
    from app.services.margin import calculate_expected_margin

    src = sc.get_product_source(db, org_id=org_id, source_id=product_source_id)
    product = _product_cost(db, org_id=org_id, product_id=UUID(src["product_id"]))

    if cost_override_krw is not None:
        cost = cost_override_krw
    else:
        landed = sc.compute_landed_cost(
            db, org_id=org_id, product_source_id=product_source_id, at=at,
            fx_rate_override=fx_rate_override,
        )
        if not landed.is_complete:
            return {"available": False, "missing": landed.missing}
        cost = landed.total_krw

    try:
        result = calculate_expected_margin(
            ExpectedMarginInput(
                org_id=org_id, product_id=None,
                margin_type=product["margin_type"] or "GENERAL",
                marketplace=marketplace, cost_amount=cost, sale_price=sale_price,
                payment_method="BUSINESS_CARD",
            ),
            db, at=at,
        )
    except RuntimeError as exc:
        return {"available": False, "missing": [_missing_policy_code(exc)]}
    return {
        "available": True,
        "cost_krw": cost,
        "margin_amount": result.margin_amount,
        "margin_rate": str(result.margin_rate),
    }
