"""Golden 검증 보조 — 원천을 모으고 **따로 계산해서** 차이를 보여준다.

사용자가 무엇을 안 하게 되나
-------------------------
```
엑셀을 직접 연다        안 한다
DB 를 조회한다          안 한다
수수료를 계산기로 센다   안 한다
마진을 손으로 더한다     안 한다
```

남는 일은 **화면에 나온 원천값과 차이를 보고 승인할지 정하는 것**뿐이다.

이 모듈이 절대 하지 않는 것
------------------------
**`app.services.margin` 을 import 하지 않는다.** 그 함수를 불러 기대값을
만들면, 그 함수가 틀려도 기대값이 같이 틀려서 검증이 항상 통과한다.

여기서는 원천 숫자를 받아 **산식을 다시 적어** 계산한다. 두 구현이 어긋나면
그게 곧 신호다 — 그 어긋남을 잡으려고 만든 물건이다.

`test_golden_evidence.py` 가 이 격리를 소스 수준에서 고정한다.

무엇이 독립인가
-------------
```
정산서 금액      쿠팡이 준 것         ← 우리가 만든 값이 아니다
장부 매입액      엑셀 초기 장부       ← 정산서와 다른 출처다
정책 요율        사람이 넣은 정책     ← 계산 결과가 아니다
```

이 셋으로 마진을 다시 세워 저장된 마진과 맞대어 본다.

추정은 추정이라고 말한다
--------------------
정산서가 없는 주문은 수수료를 정책 요율로 센다. 그건 **독립된 사실이
아니라 같은 정책을 따로 적용한 것**이다. 산술 실수는 잡지만 정책 자체가
틀린 것은 못 잡는다 — 그래서 `ESTIMATED` 로 표시하고, 사용자가 무엇을
승인하는지 알게 한다.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError

ACTUAL, ESTIMATED, UNKNOWN = "ACTUAL", "ESTIMATED", "UNKNOWN"

#: 손익 근거로 쓸 수 있는 정산 매칭 상태 (`settlement_components` 와 같은 값).
_POSTABLE = ("MATCHED_EXACT", "MATCHED_MANUAL")


# ══════════════════════════════════════════════════════════════════════
# 독립 산술 — 여기서만 계산한다
# ══════════════════════════════════════════════════════════════════════
#
# 이 구획은 production 계산기를 **모른다.** 산식을 다시 적는 것이 목적이고,
# 두 구현이 어긋나는 순간이 곧 잡고 싶은 순간이다.

def krw(value: Decimal | int) -> int:
    """원 단위 정수.

    반올림 규칙을 여기 다시 적는다(HALF_UP). `core.money.to_krw` 를 부르지
    않는 이유는 이 모듈의 존재 이유와 같다 — 그쪽 규칙이 바뀌면 **차이로
    드러나야** 한다. 조용히 따라가면 안 된다.
    """
    if isinstance(value, int):
        return value
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def independent_net_revenue(
    *, product_revenue: int, delivery_revenue: int, seller_coupon: int,
) -> int:
    """순매출.

    ```
    순매출 = 상품매출 + 배송비매출 - 판매자쿠폰
    ```

    실측 67행·20주문에서 차이 0원으로 확인된 식이다(docs/83 D1·D2).
    """
    return product_revenue + delivery_revenue - seller_coupon


def independent_fee_from_policy(*, revenue: int, rate: Decimal) -> int:
    """정책 요율로 센 수수료. 정산서가 없을 때만 쓴다."""
    if revenue <= 0:
        return 0
    return krw(Decimal(revenue) * rate)


def independent_margin(
    *, net_revenue: int, cost: int, shipping_cost: int, marketplace_fee: int,
    ad_cost: int, other_known_costs: int = 0,
) -> int:
    """공헌이익.

    ```
    마진 = 순매출 - 원가 - 매입배송비 - 판매수수료 - 광고비 - 그 외 아는 비용
    ```

    **반품비·풀필먼트비·결제수수료·부가세는 넣지 않는다.** 값이 0 이라서가
    아니라 **원천이 없어서 모르기 때문**이다. 0 으로 넣고 계산하면 그 사실이
    지워진다 — 호출부가 `UNKNOWN` 으로 따로 알린다.
    """
    return (net_revenue - cost - shipping_cost - marketplace_fee
            - ad_cost - other_known_costs)


#: 화면과 승인 modal 에 그대로 보여줄 산식. 사람이 읽고 검산할 수 있어야 한다.
INDEPENDENT_FORMULA = (
    "순매출 = 상품매출 + 배송비매출 - 판매자쿠폰\n"
    "마진   = 순매출 - 원가 - 매입배송비 - 판매수수료 - 광고비"
)

#: 아직 원천이 없어 독립 계산에 넣지 못하는 항목.
UNPRICED_COMPONENTS = ("return_cost", "fulfillment_fee", "payment_fee", "vat")


# ══════════════════════════════════════════════════════════════════════
# 원천 수집
# ══════════════════════════════════════════════════════════════════════

def _fact(
    value: int | None, source: str, confidence: str,
    *, source_ref: str | None = None, source_date: date | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """성분 하나의 사실. **모르면 value 를 None 으로 둔다 — 0 이 아니다.**"""
    return {
        "value": value, "source": source, "confidence": confidence,
        "source_ref": source_ref,
        "source_date": source_date.isoformat() if source_date else None,
        "note": note,
    }


def _settlement_facts(db: Session, *, org_id: UUID, sales_order_id: UUID) -> dict[str, Any] | None:
    """정산서가 준 것. 없으면 None — **0 이 아니다.**

    파일 이름·시트·행 번호까지 함께 가져온다. 사용자가 "어디서 나온
    숫자냐" 를 물었을 때 엑셀을 열지 않고도 답할 수 있어야 한다(§24).
    원본 파일을 노출하지는 않는다.
    """
    rows = db.execute(text("""
        SELECT m.row_type, m.line_reference, m.gross_revenue, m.marketplace_fee,
               m.other_deductions, m.net_settlement, m.settlement_period_start,
               m.settlement_period_end, m.import_batch_id,
               b.summary_json ->> 'source_name' AS file_name,
               b.summary_json ->> 'sheet_name'  AS sheet_name
          FROM marketplace_settlement_record m
          LEFT JOIN batch_run b ON b.id = m.import_batch_id
         WHERE m.org_id = :o AND m.deleted_at IS NULL
           AND m.matched_sales_order_id = :s
           AND m.matching_status = ANY(:st)
         ORDER BY (m.row_type = 'PRODUCT_SALE') DESC, m.line_reference
    """), {"o": str(org_id), "s": str(sales_order_id),
           "st": list(_POSTABLE)}).mappings().all()
    if not rows:
        return None

    product = sum(int(r["gross_revenue"]) for r in rows if r["row_type"] == "PRODUCT_SALE")
    delivery = sum(int(r["gross_revenue"]) for r in rows if r["row_type"] != "PRODUCT_SALE")
    coupon = sum(int(r["other_deductions"] or 0) for r in rows)
    fee = sum(int(r["marketplace_fee"] or 0) for r in rows)
    vendor_net = sum(int(r["net_settlement"] or 0) for r in rows)

    head = rows[0]
    file_name = head["file_name"] or "정산서 파일"
    sheet = head["sheet_name"]
    # 사람이 파일에서 찾아갈 수 있는 최소 좌표. 상품 행의 라인 번호를 쓴다.
    product_ref = next(
        (r["line_reference"] for r in rows if r["row_type"] == "PRODUCT_SALE"), None)
    where = f"{file_name}" + (f" · {sheet}" if sheet else "")
    if product_ref:
        where += f" · 행 {product_ref}"

    return {
        "product_revenue": product,
        "delivery_revenue": delivery,
        "seller_coupon": coupon,
        "marketplace_fee": fee,
        "vendor_net_settlement": vendor_net,
        "rows": len(rows),
        "where": where,
        "file_name": file_name,
        "sheet_name": sheet,
        "period_start": head["settlement_period_start"],
        "period_end": head["settlement_period_end"],
        "row_refs": [
            {"row_type": r["row_type"], "line_reference": r["line_reference"],
             "gross_revenue": int(r["gross_revenue"]),
             "marketplace_fee": int(r["marketplace_fee"] or 0),
             "other_deductions": int(r["other_deductions"] or 0),
             "net_settlement": int(r["net_settlement"] or 0)}
            for r in rows
        ],
    }


def _fee_policy(
    db: Session, *, org_id: UUID, marketplace: str, at: date,
) -> dict[str, Any] | None:
    """그 시점에 유효한 마켓 수수료 정책. 없으면 None.

    정책을 **읽기만** 한다. 정책 해석 서비스를 부르지 않는 이유는 이 모듈의
    독립성 때문이다 — 그쪽이 잘못 고르면 여기서 차이로 드러나야 한다.
    """
    row = db.execute(text("""
        SELECT id, fee_type, rate, fixed_amount, effective_from, effective_to
          FROM marketplace_fee_policy
         WHERE org_id = :o AND deleted_at IS NULL
           AND marketplace = :mk
           AND effective_from <= :at
           AND (effective_to IS NULL OR effective_to >= :at)
         ORDER BY effective_from DESC
         LIMIT 1
    """), {"o": str(org_id), "mk": marketplace, "at": at}).mappings().first()
    return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════════
# 한 후보의 검증 묶음
# ══════════════════════════════════════════════════════════════════════

def build(db: Session, *, org_id: UUID, sales_order_id: UUID) -> dict[str, Any]:
    """한 주문의 원천 · 독립 검산 · 차이를 한 번에.

    **개인정보를 꺼내지 않는다.** 검증에 필요한 것은 주문 식별자와 금액이다.
    """
    so = db.execute(text("""
        SELECT so.id, so.business_key, so.marketplace, so.marketplace_order_no,
               so.line_no, so.sale_date, so.quantity, so.sale_price,
               so.purchase_cost, so.shipping_fee, so.ad_cost, so.import_source,
               so.product_snapshot_name, so.profit_status,
               pma.id AS margin_id, pma.actual_revenue, pma.actual_cost,
               pma.actual_shipping_cost, pma.actual_marketplace_fee,
               pma.actual_ad_cost, pma.actual_return_cost,
               pma.actual_fulfillment_fee, pma.actual_payment_fee, pma.actual_vat,
               pma.actual_margin_amount, pma.actual_margin_rate,
               pma.calculation_snapshot_json AS snap
          FROM sales_order so
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
         WHERE so.id = :s AND so.org_id = :o AND so.deleted_at IS NULL
    """), {"s": str(sales_order_id), "o": str(org_id)}).mappings().first()
    if so is None:
        raise NotFoundError("판매 건을 찾을 수 없습니다.")

    settlement = _settlement_facts(db, org_id=org_id, sales_order_id=sales_order_id)
    ledger_ref = f"판매장부 {so['business_key']}"
    if so["import_source"] == "EXCEL_IMPORT":
        ledger_ref += " (엑셀 초기 장부)"

    facts: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    warnings: list[str] = []

    # ── 매출 ────────────────────────────────────────────────────────
    if settlement:
        facts["product_revenue"] = _fact(
            settlement["product_revenue"], "SETTLEMENT", ACTUAL,
            source_ref=settlement["where"], source_date=settlement["period_start"])
        facts["delivery_revenue"] = _fact(
            settlement["delivery_revenue"], "SETTLEMENT", ACTUAL,
            source_ref=settlement["where"], source_date=settlement["period_start"])
        facts["seller_coupon"] = _fact(
            settlement["seller_coupon"], "SETTLEMENT", ACTUAL,
            source_ref=settlement["where"], source_date=settlement["period_start"])
    else:
        # 정산서가 없다. 장부 판매금액을 매출로 본다 — 배송비·쿠폰은 **모른다.**
        facts["product_revenue"] = _fact(
            int(so["sale_price"]), "ORDER_LEDGER", ESTIMATED,
            source_ref=ledger_ref, source_date=so["sale_date"],
            note="정산서가 아직 없어 장부 판매금액을 그대로 본다")
        facts["delivery_revenue"] = _fact(
            None, "NOT_AVAILABLE", UNKNOWN,
            note="정산서가 없어 배송비 매출을 따로 알 수 없다")
        facts["seller_coupon"] = _fact(
            None, "NOT_AVAILABLE", UNKNOWN,
            note="정산서가 없어 판매자 쿠폰을 알 수 없다")

    # ── 수수료 ──────────────────────────────────────────────────────
    if settlement:
        facts["marketplace_fee"] = _fact(
            settlement["marketplace_fee"], "SETTLEMENT", ACTUAL,
            source_ref=settlement["where"], source_date=settlement["period_start"])
    else:
        policy = _fee_policy(db, org_id=org_id, marketplace=so["marketplace"],
                             at=so["sale_date"])
        if policy is None:
            facts["marketplace_fee"] = _fact(
                None, "POLICY_MISSING", UNKNOWN,
                note="이 시점 마켓 수수료 정책이 없어 추정할 수 없다")
            blockers.append("수수료를 셀 근거가 없다 (정책 없음)")
        elif policy["fee_type"] != "RATE":
            # 요율이 아닌 정책은 여기서 다시 세지 않는다. 지어내지 않는다.
            facts["marketplace_fee"] = _fact(
                None, "POLICY_UNSUPPORTED", UNKNOWN,
                note=f"{policy['fee_type']} 방식 정책은 독립 검산이 준비돼 있지 않다")
            blockers.append("수수료 정책 방식을 독립 검산이 다루지 못한다")
        else:
            rate = Decimal(str(policy["rate"]))
            facts["marketplace_fee"] = _fact(
                independent_fee_from_policy(
                    revenue=int(so["sale_price"]), rate=rate),
                "POLICY", ESTIMATED,
                source_ref=f"{so['marketplace']} 수수료 정책 {rate * 100:.2f}%",
                source_date=policy["effective_from"],
                note="정산서가 없어 정책 요율로 센 값이다 — 실제 수수료가 아니다")

    # ── 원가 · 그 외 장부값 ──────────────────────────────────────────
    if so["purchase_cost"] is not None:
        facts["cost"] = _fact(
            int(so["purchase_cost"]), "ORDER_PURCHASE_COST", ACTUAL,
            source_ref=ledger_ref, source_date=so["sale_date"],
            note="그 주문에서 실제로 지출한 매입액")
    else:
        master = db.execute(text("""
            SELECT COALESCE(pv.cost_amount, p.cost_amount) AS cost
              FROM sales_order so
              LEFT JOIN product_variant pv ON pv.id = so.product_variant_id
              LEFT JOIN product p ON p.id = so.product_id
             WHERE so.id = :s
        """), {"s": str(sales_order_id)}).scalar()
        if master is None:
            facts["cost"] = _fact(None, "NOT_AVAILABLE", UNKNOWN,
                                  note="주문 매입액도 마스터 원가도 없다")
            blockers.append("원가를 셀 근거가 없다")
        else:
            facts["cost"] = _fact(
                int(master) * int(so["quantity"]), "PRODUCT_MASTER", ESTIMATED,
                source_ref="상품 마스터 원가 × 수량",
                note="그 주문의 실제 매입액이 아니라 마스터 평균이다")

    facts["shipping_cost"] = _fact(
        int(so["shipping_fee"] or 0), "ORDER_LEDGER", ACTUAL,
        source_ref=ledger_ref, source_date=so["sale_date"],
        note="매입 배송비 — 정산서의 배송비 매출과 다른 축이다")
    facts["ad_cost"] = _fact(
        int(so["ad_cost"] or 0), "ORDER_LEDGER", ACTUAL,
        source_ref=ledger_ref, source_date=so["sale_date"])

    # 아직 원천이 없는 것들. **0 으로 계산에 넣지 않는다.**
    for name in UNPRICED_COMPONENTS:
        facts[name] = _fact(
            None, "NOT_AVAILABLE", UNKNOWN,
            note="원천이 아직 없어 모른다 — 0원이라는 뜻이 아니다")

    # ── 독립 계산 ───────────────────────────────────────────────────
    calc = _independent_block(facts, settlement)
    if not calc["calculable"]:
        blockers.append("독립 검산에 필요한 값이 빠져 있다")

    # ── 비교 ────────────────────────────────────────────────────────
    comparison = _compare(calc, so)

    # ── 클레임 ──────────────────────────────────────────────────────
    claims = db.execute(text("""
        SELECT claim_type, claim_status, financial_effect, claim_quantity, claim_amount
          FROM sales_claim
         WHERE org_id = :o AND sales_order_id = :s AND deleted_at IS NULL
    """), {"o": str(org_id), "s": str(sales_order_id)}).mappings().all()
    if claims:
        warnings.append(
            "이 주문에는 취소·반품이 있다. 환불 정산 실표본이 없어 환불 손익은 "
            "검증되지 않았다 — 이 마진을 최종 확정으로 보면 안 된다")

    evidence_state = (
        "SOURCE_EVIDENCE_INCOMPLETE" if blockers else "READY")

    return {
        "order": {
            "sales_order_id": str(so["id"]),
            "business_key": so["business_key"],
            "marketplace": so["marketplace"],
            "marketplace_order_no": so["marketplace_order_no"],
            "line_no": so["line_no"],
            "sale_date": so["sale_date"],
            "quantity": so["quantity"],
            "product_name": so["product_snapshot_name"],
            "import_source": so["import_source"],
            "profit_status": so["profit_status"],
            "ledger_sale_price": int(so["sale_price"]),
        },
        "settlement": settlement,
        "facts": facts,
        "independent": calc,
        "comparison": comparison,
        "formula": INDEPENDENT_FORMULA,
        "golden_confidence": _golden_confidence(facts),
        "evidence_state": evidence_state,
        "blockers": blockers,
        "warnings": warnings,
        "claims": [
            {"claim_type": c["claim_type"], "claim_status": c["claim_status"],
             "financial_effect": c["financial_effect"],
             "claim_quantity": c["claim_quantity"],
             "claim_amount": int(c["claim_amount"]) if c["claim_amount"] is not None else None}
            for c in claims
        ],
        "refund_financial": "NOT_VERIFIED" if claims else None,
        "checklist": _checklist(facts, settlement, comparison, claims),
        # 저장된 계산에 근거가 박혀 있나. 없으면 승인 전에 알린다.
        "stored_calculation_has_evidence": isinstance(
            (so["snap"] or {}).get("components"), dict),
        "margin_calculated": so["margin_id"] is not None,
    }


def _independent_block(
    facts: dict[str, dict[str, Any]], settlement: dict[str, Any] | None,
) -> dict[str, Any]:
    """원천값으로 다시 센 결과. 빠진 값이 있으면 계산하지 않는다."""
    def val(name: str) -> int | None:
        return facts[name]["value"]

    product = val("product_revenue")
    delivery = val("delivery_revenue")
    coupon = val("seller_coupon")
    fee = val("marketplace_fee")
    cost = val("cost")
    shipping = val("shipping_cost")
    ad = val("ad_cost")

    # 정산서가 없으면 배송비·쿠폰이 UNKNOWN 이다. 그 둘을 0 으로 **가정하고**
    # 계산하되, 가정했다는 사실을 남긴다. 조용히 0 을 넣으면 안 된다.
    assumptions: list[str] = []
    if delivery is None:
        delivery = 0
        assumptions.append("배송비 매출을 0으로 가정했다 (정산서 없음)")
    if coupon is None:
        coupon = 0
        assumptions.append("판매자 쿠폰을 0으로 가정했다 (정산서 없음)")

    if product is None or fee is None or cost is None:
        return {
            "calculable": False,
            "net_revenue": None, "margin": None,
            "assumptions": assumptions,
            "unpriced": list(UNPRICED_COMPONENTS),
            "reason": "매출·수수료·원가 중 모르는 값이 있어 다시 셀 수 없다",
        }

    net_revenue = independent_net_revenue(
        product_revenue=product, delivery_revenue=delivery, seller_coupon=coupon)
    margin = independent_margin(
        net_revenue=net_revenue, cost=cost, shipping_cost=shipping or 0,
        marketplace_fee=fee, ad_cost=ad or 0)

    out: dict[str, Any] = {
        "calculable": True,
        "product_revenue": product,
        "delivery_revenue": delivery,
        "seller_coupon": coupon,
        "net_revenue": net_revenue,
        "marketplace_fee": fee,
        "cost": cost,
        "shipping_cost": shipping or 0,
        "ad_cost": ad or 0,
        "margin": margin,
        "assumptions": assumptions,
        # 이 넷은 계산에 안 들어갔다. 마진이 그만큼 낙관적일 수 있다.
        "unpriced": list(UNPRICED_COMPONENTS),
    }
    if settlement:
        # 벤더가 준 정산금액과도 맞춰 본다. 이건 순수하게 정산서 안의 관계다.
        out["vendor_check"] = {
            "reconstructed": net_revenue - fee,
            "vendor_net_settlement": settlement["vendor_net_settlement"],
            "difference": (net_revenue - fee) - settlement["vendor_net_settlement"],
        }
    return out


def _compare(calc: dict[str, Any], so: Any) -> dict[str, Any]:
    """독립 계산 vs 저장된 계산. **원 단위로 정확히.**"""
    if not calc["calculable"] or so["margin_id"] is None:
        return {
            "comparable": False,
            "reason": ("독립 검산을 할 수 없다" if not calc["calculable"]
                       else "이 주문의 실 마진이 아직 계산되지 않았다"),
            "items": [], "all_match": False, "mismatched": 0,
        }

    pairs = (
        ("net_revenue", calc["net_revenue"], int(so["actual_revenue"]), "순매출"),
        ("marketplace_fee", calc["marketplace_fee"],
         int(so["actual_marketplace_fee"]), "판매수수료"),
        ("cost", calc["cost"], int(so["actual_cost"]), "원가"),
        ("shipping_cost", calc["shipping_cost"],
         int(so["actual_shipping_cost"]), "매입 배송비"),
        ("ad_cost", calc["ad_cost"], int(so["actual_ad_cost"]), "광고비"),
        ("margin", calc["margin"], int(so["actual_margin_amount"]), "마진"),
    )
    items = [
        {"field": field, "label": label, "independent": indep,
         "seller_os": stored, "difference": indep - stored,
         "match": indep == stored}
        for field, indep, stored, label in pairs
    ]
    mismatched = sum(1 for i in items if not i["match"])
    return {
        "comparable": True,
        "items": items,
        "mismatched": mismatched,
        "all_match": mismatched == 0,
    }


def _golden_confidence(facts: dict[str, dict[str, Any]]) -> str:
    """이 후보를 승인하면 무엇을 기준으로 삼는 것인가 (§10).

    핵심 셋(매출·수수료·원가)만 본다. 원천이 아예 없는 넷은 모든 주문에서
    똑같이 UNKNOWN 이라 후보를 구분하지 못한다.
    """
    core = ("product_revenue", "marketplace_fee", "cost")
    levels = {facts[name]["confidence"] for name in core}
    if UNKNOWN in levels:
        return UNKNOWN
    if ESTIMATED in levels:
        return ESTIMATED
    return ACTUAL


def _checklist(
    facts: dict[str, dict[str, Any]], settlement: dict[str, Any] | None,
    comparison: dict[str, Any], claims: list[Any],
) -> list[dict[str, Any]]:
    """사용자가 눈으로 확인할 것 3~5개만 (§46).

    전부 나열하면 아무도 안 본다. **이 주문에서 실제로 의미 있는 것**만 남긴다.
    """
    out: list[dict[str, Any]] = []

    def add(label: str, detail: str, auto: bool | None) -> None:
        out.append({"label": label, "detail": detail, "auto_matched": auto})

    matched = {i["field"]: i["match"] for i in comparison.get("items", [])}

    if settlement:
        add("상품매출이 정산서와 같은가",
            f"{settlement['where']} · {facts['product_revenue']['value']:,}원",
            matched.get("net_revenue"))
        if settlement["delivery_revenue"]:
            add("배송비 매출이 맞는가",
                f"{settlement['delivery_revenue']:,}원 — 매출에 포함한다(D1)", None)
        if settlement["seller_coupon"]:
            add("판매자 쿠폰이 맞는가",
                f"{settlement['seller_coupon']:,}원 — 매출에서 뺀다(D2)", None)
        add("실제 판매수수료가 맞는가",
            f"{settlement['marketplace_fee']:,}원 (정산서 실측)",
            matched.get("marketplace_fee"))
    else:
        add("판매금액이 장부와 같은가",
            f"{facts['product_revenue']['value']:,}원 (장부)",
            matched.get("net_revenue"))
        fee = facts["marketplace_fee"]
        add("추정 수수료를 받아들일 수 있는가",
            (f"{fee['value']:,}원 · {fee['source_ref']}"
             if fee["value"] is not None else "셀 수 없다"),
            None)

    cost = facts["cost"]
    add("원가 근거가 맞는가",
        (f"{cost['value']:,}원 · {cost['source_ref']}"
         if cost["value"] is not None else "근거 없음"),
        matched.get("cost"))

    if claims:
        add("취소·반품 영향을 확인했는가",
            "환불 정산 근거가 없어 이 마진은 확정이 아니다", None)

    return out[:5]
