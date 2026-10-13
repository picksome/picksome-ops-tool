"""재주문 판단.

무엇을 계산하나
------------
```
판매속도   = 기간 내 판매수량 / 기간일수          (개/일)
소진일수   = 현재고 / 판매속도                     (days of cover)
재고소진일 = 오늘 + 소진일수
발주기한   = 재고소진일 - 리드타임                 ← 이 날짜가 지나면 품절한다
권장수량   = (리드타임 + 목표커버) × 판매속도 - 현재고 - 미입고발주
```

`권장수량` 은 MOQ 로 올림한다. MOQ 는 `product_source.moq` 에 있는 **공급처가
정한 값**이지 우리가 만든 값이 아니다.

숫자를 지어내지 않는다
-------------------
이 모듈에는 기본 임계값이 없다. 관찰기간·목표커버일수는 **전부 호출자가
넘겨야 하는 필수 인자**다.

"보통 30일" 같은 기본값을 넣으면 그 순간부터 아무도 그 값을 자기 사업에 맞게
정하지 않는다. 그리고 그 숫자가 어디서 왔는지 나중에 아무도 모른다.

리드타임도 마찬가지다. `product_source.lead_time_days` 가 비어 있으면
평균값으로 때우지 않고 **추천하지 않는다.**

모자란 데이터를 0 으로 채우지 않는다
--------------------------------
```
판매 이력이 없다        → NO_SALES_HISTORY      (판매속도 0 이 아니다)
리드타임이 없다         → NO_LEAD_TIME
소싱처가 없다           → NO_SOURCE
원가를 모른다           → NO_COST
재고 원장에 기록이 없다  → NOT_TRACKED           (재고 0 이 아니다)
```

각각 다른 대응이 필요하다. "추천 불가" 로 뭉뚱그리면 사용자가 무엇을 채워야
하는지 알 수 없다.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError

#: 추천할 수 없는 이유. 화면이 "무엇을 채워야 하는지" 를 안내하는 근거다.
BLOCK_REASONS = {
    "NOT_TRACKED": "재고 원장에 기록이 없습니다.",
    "NO_SALES_HISTORY": "관찰 기간에 판매가 없어 판매속도를 계산할 수 없습니다.",
    "NO_SOURCE": "연결된 소싱처가 없습니다.",
    "NO_LEAD_TIME": "소싱처에 리드타임이 없습니다.",
    "NO_COST": "소싱 원가를 알 수 없습니다.",
}


def calculate(
    db: Session, *, org_id: UUID, velocity_days: int, coverage_days: int,
    as_of: date | None = None, limit: int = 200,
) -> dict[str, Any]:
    """재주문 후보 계산.

    `velocity_days` — 판매속도를 볼 기간(일). 사용자가 정한다.
    `coverage_days` — 발주 후 며칠치를 확보할지. 사용자가 정한다.

    둘 다 필수다. 기본값을 두지 않는 이유는 모듈 문서 참고.
    """
    if velocity_days <= 0:
        raise ValidationError("판매속도 관찰 기간은 1일 이상이어야 합니다.")
    if coverage_days <= 0:
        raise ValidationError("목표 커버 일수는 1일 이상이어야 합니다.")

    today = as_of or date.today()
    since = today - timedelta(days=velocity_days)

    # 재고·판매·소싱을 한 번에 모은다.
    #
    # `product_source` 는 `is_primary` 인 것 하나만 본다. 여러 곳에서 살 수
    # 있어도 재주문 기준은 주 소싱처다 — 아니면 어느 리드타임을 쓸지 정할 수
    # 없다.
    rows = db.execute(text("""
        WITH bal AS (
            SELECT product_id, product_variant_id,
                   SUM(quantity_delta)::int AS on_hand,
                   COUNT(*)::int AS movements
              FROM inventory_transaction
             WHERE org_id = :org
             GROUP BY product_id, product_variant_id
        ),
        sold AS (
            SELECT product_id, product_variant_id,
                   SUM(quantity)::int AS qty
              FROM sales_order
             WHERE org_id = :org AND deleted_at IS NULL
               AND sale_date >= :since AND sale_date <= :today
             GROUP BY product_id, product_variant_id
        ),
        inbound AS (
            SELECT l.product_id, l.product_variant_id,
                   SUM(l.quantity - l.received_quantity)::int AS qty
              FROM purchase_order_line l
              JOIN purchase_order o ON o.id = l.purchase_order_id
             WHERE l.org_id = :org AND o.deleted_at IS NULL
               AND o.status IN ('ORDERED', 'PARTIALLY_RECEIVED')
               AND l.quantity > l.received_quantity
             GROUP BY l.product_id, l.product_variant_id
        ),
        src AS (
            SELECT DISTINCT ON (product_id)
                   product_id, id AS source_id, supplier_site_id,
                   lead_time_days, moq, currency
              FROM product_source
             WHERE org_id = :org AND deleted_at IS NULL AND status = 'ACTIVE'
             ORDER BY product_id, is_primary DESC, created_at
        ),
        -- 소싱처의 **현재 유효한** 매입가. 발주 단가의 1순위다.
        --
        -- `product.cost_amount` 보다 이쪽을 먼저 쓰는 이유: 이건 "이 공급처에서
        -- 지금 사면 얼마" 라는 값이고, 발주는 정확히 그 행위다.
        src_cost AS (
            SELECT DISTINCT ON (c.product_source_id)
                   c.product_source_id, c.purchase_price, c.currency
              FROM product_source_cost_version c
             WHERE c.org_id = :org AND c.deleted_at IS NULL
               AND c.valid_from <= :today
               AND (c.valid_to IS NULL OR c.valid_to > :today)
             ORDER BY c.product_source_id, c.valid_from DESC
        )
        SELECT p.id AS product_id, b.product_variant_id,
               p.name_ko AS product_name, p.business_key AS product_key,
               v.variant_key, v.sku_external,
               b.on_hand, b.movements,
               COALESCE(s.qty, 0) AS sold_qty,
               COALESCE(i.qty, 0) AS inbound_qty,
               src.source_id, src.supplier_site_id, src.lead_time_days,
               src.moq, src.currency,
               ss.name AS supplier_name,
               sc.purchase_price AS source_price, sc.currency AS source_currency,
               p.cost_amount
          FROM bal b
          JOIN product p ON p.id = b.product_id AND p.deleted_at IS NULL
          LEFT JOIN product_variant v ON v.id = b.product_variant_id
          LEFT JOIN sold s ON s.product_id = b.product_id
                          AND s.product_variant_id IS NOT DISTINCT FROM b.product_variant_id
          LEFT JOIN inbound i ON i.product_id = b.product_id
                             AND i.product_variant_id IS NOT DISTINCT FROM b.product_variant_id
          LEFT JOIN src ON src.product_id = b.product_id
          LEFT JOIN src_cost sc ON sc.product_source_id = src.source_id
          LEFT JOIN supplier_site ss ON ss.id = src.supplier_site_id
         LIMIT :lim
    """), {
        "org": str(org_id), "since": since, "today": today, "lim": limit,
    }).mappings().all()

    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for r in rows:
        item = {
            "product_id": str(r["product_id"]),
            "product_variant_id": (
                str(r["product_variant_id"]) if r["product_variant_id"] else None
            ),
            "product_name": r["product_name"],
            "product_key": r["product_key"],
            "variant_key": r["variant_key"],
            "sku": r["sku_external"],
            "on_hand": r["on_hand"],
            "inbound": r["inbound_qty"],
            "sold_in_window": r["sold_qty"],
            "supplier_name": r["supplier_name"],
        }

        reason = _blocking_reason(r)
        if reason:
            blocked.append({**item, "block_reason": reason,
                            "block_message": BLOCK_REASONS[reason]})
            continue

        # 하루에 몇 개 나가나. Decimal 로 계산한다 — 0.3개/일 같은 값이 흔하고
        # float 로 누적하면 소진일수가 어긋난다.
        velocity = Decimal(r["sold_qty"]) / Decimal(velocity_days)
        lead = int(r["lead_time_days"])
        on_hand = int(r["on_hand"])
        inbound = int(r["inbound_qty"])

        # 미입고 발주도 결국 들어온다. 이걸 빼먹으면 이미 주문한 것을 또
        # 주문하라고 권하게 된다.
        effective = Decimal(on_hand + inbound)
        days_of_cover = effective / velocity
        stockout_date = today + timedelta(days=int(days_of_cover))
        # 이 날짜가 지나면 리드타임 안에 못 받아서 품절한다.
        reorder_by = stockout_date - timedelta(days=lead)

        target_qty = velocity * Decimal(lead + coverage_days)
        shortfall = target_qty - effective

        if shortfall <= 0:
            # 지금 발주할 이유가 없다. 목록에는 넣되 후보로 세지 않는다.
            continue

        moq = int(r["moq"]) if r["moq"] else 1
        raw_qty = int(shortfall.to_integral_value(rounding=ROUND_CEILING))
        # MOQ 배수로 올린다. 공급처가 정한 값이지 우리가 만든 값이 아니다.
        recommended = ((raw_qty + moq - 1) // moq) * moq

        # 어느 값을 발주 단가로 썼는지 **밝힌다.** 금액이 예상과 다를 때
        # 사용자가 어디를 고쳐야 하는지 알아야 한다.
        if r["source_price"] is not None:
            unit_price = int(r["source_price"])
            price_basis = "SOURCE_COST_VERSION"
            currency = r["source_currency"] or r["currency"] or "KRW"
        else:
            unit_price = int(r["cost_amount"])
            price_basis = "PRODUCT_COST"
            currency = r["currency"] or "KRW"

        candidates.append({
            **item,
            "daily_velocity": str(velocity.quantize(Decimal("0.01"))),
            "days_of_cover": int(days_of_cover),
            "stockout_date": stockout_date.isoformat(),
            "reorder_by_date": reorder_by.isoformat(),
            # 발주 기한이 지났으면 이미 늦은 것이다. 화면이 빨갛게 칠할 근거.
            "overdue": reorder_by <= today,
            "lead_time_days": lead,
            "moq": moq,
            "recommended_quantity": recommended,
            "unit_price": unit_price,
            "price_basis": price_basis,
            "estimated_amount": recommended * unit_price,
            "currency": currency,
            "product_source_id": str(r["source_id"]),
            "supplier_site_id": str(r["supplier_site_id"]),
        })

    # 급한 것부터. 발주 기한이 이른 순이다.
    candidates.sort(key=lambda c: (c["reorder_by_date"], -c["estimated_amount"]))

    return {
        "as_of": today.isoformat(),
        "velocity_days": velocity_days,
        "coverage_days": coverage_days,
        "candidates": candidates,
        "blocked": blocked,
        "summary": {
            "candidate_count": len(candidates),
            "overdue_count": sum(1 for c in candidates if c["overdue"]),
            "blocked_count": len(blocked),
            "estimated_total": sum(c["estimated_amount"] for c in candidates),
        },
    }


def _blocking_reason(row: Any) -> str | None:
    """추천할 수 없는 첫 번째 이유. 없으면 `None`.

    순서가 있다 — 소싱처가 없으면 리드타임도 없으므로, 사용자에게 먼저
    고쳐야 할 것을 알려 준다.
    """
    if row["movements"] == 0:
        return "NOT_TRACKED"
    if row["sold_qty"] == 0:
        # 판매속도 0 으로 계산하면 소진일수가 무한대가 된다. 그건 "영원히
        # 안 떨어진다" 가 아니라 "모른다" 다.
        return "NO_SALES_HISTORY"
    if row["source_id"] is None:
        return "NO_SOURCE"
    if row["lead_time_days"] is None:
        return "NO_LEAD_TIME"
    if row["source_price"] is None and row["cost_amount"] is None:
        # 소싱 원가 버전도 없고 상품 원가도 없다. 발주 금액을 지어낼 수 없다.
        return "NO_COST"
    return None


def group_by_supplier(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """공급처별로 묶는다. 발주는 공급처 단위로 나가기 때문이다.

    상품별로 흩어진 목록을 그대로 두면 사용자가 손으로 묶어야 한다.
    """
    groups: dict[str, dict[str, Any]] = {}
    for c in candidates:
        key = c["supplier_site_id"]
        g = groups.setdefault(key, {
            "supplier_site_id": key,
            "supplier_name": c["supplier_name"],
            "currency": c["currency"],
            "lines": [],
            "total_amount": 0,
        })
        g["lines"].append(c)
        g["total_amount"] += c["estimated_amount"]
    return sorted(groups.values(), key=lambda g: -g["total_amount"])
