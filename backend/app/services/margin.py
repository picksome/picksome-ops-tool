"""마진 서비스 — 정책 조회 + 계산 + 영구화 통합.

calculation.py (순수 산식) + policy.py (DB 조회/박제) 를 결합한 어댑터.
라우터 (api/v1/margin.py) 는 본 모듈만 호출.

흐름:
    calculate_expected_margin: PolicyContext → resolve → compute (DB read-only)
    save_expected_margin:      위 결과 → product_margin_expected INSERT + snapshot 박제
    calculate_actual_margin:   sales_order + marketplace_settlement_record → 실 마진 + delta
    save_actual_margin:        위 결과 → product_margin_actual UPSERT (sales_order_id 기준)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.month_lock import assert_period_writable
from app.domain.margin import (
    ActualMarginInput,
    ActualMarginResult,
    ExpectedMarginInput,
    ExpectedMarginResult,
)
from app.domain.policy import PolicyContext
from app.services.calculation import calc_marketplace_fee, compute_expected_margin
from app.services.policy import persist_policy_snapshot, resolve_active_policies


def calculate_expected_margin(
    input: ExpectedMarginInput, db: Session, at: date | None = None
) -> ExpectedMarginResult:
    """정책 조회 → 순수 계산 함수 호출. DB 저장은 save_expected_margin 에서 별도."""
    ctx = PolicyContext(
        org_id=input.org_id,
        at=at or date.today(),
        marketplace=input.marketplace,
        margin_type=input.margin_type,
        payment_method=input.payment_method,
    )
    policies = resolve_active_policies(ctx, db)
    return compute_expected_margin(input, policies)


def save_expected_margin(
    input: ExpectedMarginInput,
    result: ExpectedMarginResult,
    db: Session,
    valid_from: date | None = None,
) -> UUID:
    """product_margin_expected 행 + policy_snapshot 박제. 반환: 새 행 UUID.

    snapshot_id 는 product_margin_expected.snapshot_id NOT NULL FK 이므로
    policy_snapshot 을 먼저 만들고(persist_policy_snapshot) 그 id로 margin 행을 INSERT 한다.
    호출자가 commit 책임.
    """
    margin_row_id = new_id()
    snapshot_id = persist_policy_snapshot(
        "MARGIN_EXPECTED", margin_row_id, result.applied_policies, input.org_id, db
    )
    db.execute(
        text(
            """
            INSERT INTO product_margin_expected (
              id, org_id, product_id, product_variant_id, margin_type, marketplace,
              input_cost, input_sale_price, input_extra_json,
              computed_marketplace_fee, computed_payment_fee, computed_ad_cost,
              computed_fulfillment_fee, computed_return_risk_cost, computed_vat,
              computed_margin_amount, computed_margin_rate,
              snapshot_id, override_fields_json, valid_from
            ) VALUES (
              :id, :org_id, :product_id, :variant_id, :margin_type, :marketplace,
              :cost, :sale_price, CAST(:extras AS JSONB),
              :mfee, :pfee, :ad, :fulfill, :rrisk, :vat,
              :margin_amount, :margin_rate,
              :snapshot_id, CAST(:overrides AS JSONB), :valid_from
            )
            """
        ),
        {
            "id": str(margin_row_id),
            "org_id": str(input.org_id),
            "product_id": str(input.product_id),
            "variant_id": str(input.product_variant_id) if input.product_variant_id else None,
            "margin_type": input.margin_type,
            "marketplace": input.marketplace,
            "cost": input.cost_amount,
            "sale_price": input.sale_price,
            "extras": _json(input.extras),
            "mfee": result.marketplace_fee,
            "pfee": result.payment_fee,
            "ad": result.ad_cost,
            "fulfill": result.fulfillment_fee,
            "rrisk": result.return_risk_cost,
            "vat": result.vat,
            "margin_amount": result.margin_amount,
            "margin_rate": result.margin_rate,
            "snapshot_id": str(snapshot_id),
            "overrides": _json(input.override_fields) if input.override_fields else None,
            "valid_from": valid_from or date.today(),
        },
    )
    return margin_row_id


def calculate_actual_margin(input: ActualMarginInput, db: Session) -> ActualMarginResult:
    """실 마진 — sales_order + (매칭됐다면) 주문 단위 정산 컴포넌트.

    승인된 재무 결정 (docs/83)
    -----------------------
    ```
    D1  배송비매출을 공헌이익에 포함한다
    D2  판매자 부담 쿠폰을 매출 차감으로 본다
    D3  매칭된 주문은 실제 수수료를 정책 추정보다 우선한다
    ```

    ```
    매칭됨   revenue = 상품매출 + 배송비매출 - 판매자쿠폰   (정산 컴포넌트)
             mfee    = 벤더가 실제로 뗀 판매수수료 (VAT 포함)
    미매칭   revenue = sales_order.sale_price
             mfee    = marketplace_fee_policy 추정 + warning
    ```

    **정산 행 하나를 고르지 않는다.** 쿠팡은 주문 하나를 상품·기본배송료·
    추가배송료 세 행으로 준다. 하나만 붙이면 나머지가 사라진다 — 배송료 행을
    고르면 매출이 0 이 되고, 상품 행을 고르면 배송비 매출이 통째로 빠진다.
    `settlement_components` 가 주문 단위로 합쳐서 준다.

    정산서가 안 주는 것을 0 으로 덮지 않는다
    ------------------------------------
    예전 구현은 정산 레코드가 있으면 `ad_cost` · `return_cost` ·
    `fulfillment_fee` 를 **그 레코드 값으로 갈아끼웠다.** 쿠팡 정산서에는 이
    셋이 아예 없어서(`NOT_PROVIDED`) 적재 시 0 이 들어가 있다 — 그대로
    덮으면 장부에 있던 광고비가 조용히 사라지고 마진이 그만큼 부푼다.

    이제 **정산서가 실제로 준 항목만** 덮는다(매출·판매수수료). 나머지는
    장부 값을 그대로 쓴다.

    나머지 (변경 없음):
        cost     = sales_order.purchase_cost (라인 합계) 우선,
                   없으면 variant/product.cost_amount × qty (단가)
        shipping = sales_order.shipping_fee (**매입 배송비 = 비용**)
        payment_fee / vat 는 MVP 0 (외부 import 완성 후 채움)
        matched_expected_id + delta 계산 (정확도 모니터링용)
    """
    so = db.execute(
        text(
            """
            SELECT id, product_id, product_variant_id, marketplace, sale_price,
                   quantity, ad_cost, sale_date, purchase_cost, shipping_fee
              FROM sales_order
             WHERE id = :id AND org_id = :org AND deleted_at IS NULL
            """
        ),
        {"id": str(input.sales_order_id), "org": str(input.org_id)},
    ).first()
    if so is None:
        raise NotFoundError(f"sales_order {input.sales_order_id} not found")

    from app.services import settlement_components as sc

    components = sc.load_for_order(
        db, org_id=input.org_id, sales_order_id=input.sales_order_id)

    # 호출자가 특정 정산 행을 지목했다면 **그 행이 이 주문 것인지 확인한다.**
    # 예전에는 아무 행이나 받아 그 금액으로 계산했다 — 남의 주문 정산서를
    # 붙여도 막지 못했다. 지금은 무엇을 붙일지 매칭이 정하고, 이 인자는
    # 그 매칭과 어긋나지 않는지 검사하는 용도로만 쓴다.
    if input.settlement_record_id is not None:
        known = components.record_ids if components else []
        if input.settlement_record_id not in known:
            raise ValidationError(
                "이 주문에 매칭되지 않은 정산 레코드입니다. "
                "정산 연결은 대사 화면에서 승인을 거쳐 바꿉니다.",
                detail={"sales_order_id": str(input.sales_order_id),
                        "settlement_record_id": str(input.settlement_record_id)},
            )

    warnings: list[str] = []

    cost, cost_source = _resolve_actual_cost(
        so.purchase_cost, so.product_id, so.product_variant_id, int(so.quantity), db
    )
    if cost_source == "UNKNOWN":
        # 0 원으로 계산은 하되 그것이 "모른다" 임을 남긴다. 이 문구가 없으면
        # 원가 미확인 주문이 원가 0 원인 주문과 같아 보인다.
        warnings.append(
            "원가를 확인할 수 없어 0원으로 계산했다 — 실제 원가가 아니다")
    # **비용**이다. 정산서의 배송비 매출과 다른 축이다(docs/83 §F-30).
    shipping = int(so.shipping_fee or 0)  # 매입 배송비 — 라인 합계

    # 정산서가 주지 않는 항목은 장부 값을 그대로 쓴다. 0 으로 덮지 않는다.
    ad = int(so.ad_cost)
    rret = 0
    fulfill = 0
    fee_source = "SETTLEMENT" if components else "POLICY"

    if components:
        # D1 + D2 — 상품매출 + 배송비매출 - 판매자쿠폰
        revenue = components.net_revenue
        # D3 — 벤더가 실제로 뗀 금액 (VAT 포함)
        mfee = components.marketplace_fee
        if not components.reconciles():
            # 컴포넌트 합이 벤더 정산금액과 안 맞는다. 우리가 행을 잘못
            # 나눴거나 벤더 형식이 바뀐 것이다 — 조용히 넘기지 않는다.
            warnings.append(
                f"정산 컴포넌트 합({components.net_revenue - components.marketplace_fee:,}원)이 "
                f"판매처 정산금액({components.net_settlement:,}원)과 다르다 — "
                "정산 행 구성을 확인해야 한다"
            )
        ledger = int(so.sale_price)
        if components.gross_revenue != ledger:
            # D4 — 알리되 장부를 고치지 않는다. 예외 센터·마감 검증이
            # 같은 사실을 목록으로 보여준다.
            warnings.append(
                f"정산서 매출({components.gross_revenue:,}원)과 장부 판매금액"
                f"({ledger:,}원)이 다르다 — 판매 원본은 수정하지 않았다"
            )
    else:
        # sale_price 는 라인 합계(판매가+배송비)라 quantity 를 곱하지 않는다.
        revenue = int(so.sale_price)
        # 정산서가 아직 없으면 마켓 수수료 정책으로 추정한다. 예전에는 0 으로 뒀는데,
        # 그러면 실 마진이 수수료만큼 통째로 과대 계상되고 월정산 direct_cost 도
        # 그만큼 비어 순이익이 부풀려진다. 추정임을 warning 으로 남긴다.
        mfee, fee_source = _estimate_marketplace_fee(
            revenue, so.marketplace, so.sale_date, input.org_id, db)
        if mfee > 0:
            warnings.append(
                f"마켓 수수료 {mfee:,}원은 정산서가 아직 없어 정책 요율로 추정한 값이다 — "
                "정산서 import 후 재계산 필요"
            )
        elif fee_source == "POLICY_MISSING":
            # **0 원이 아니라 모른다.** 예전에는 이 경우에 아무 말도 없어서
            # 수수료가 통째로 빠진 마진이 정상처럼 보였다.
            warnings.append(
                "마켓 수수료 정책을 찾지 못해 0원으로 계산했다 — 실제 수수료가 아니다")

    payment_fee = 0  # 실 결제 수수료는 별도 import 시 채움 — MVP 추정 0
    vat = 0  # 실 VAT 는 세무 import 기반 — MVP 0
    margin = revenue - cost - shipping - mfee - ad - rret - fulfill - payment_fee - vat
    margin_rate = (Decimal(margin) / Decimal(revenue)).quantize(Decimal("0.0001")) if revenue > 0 else Decimal("0.0000")

    matched_expected, delta_amount, delta_rate = _match_expected(
        so.product_id, so.product_variant_id, margin, margin_rate, db
    )

    return ActualMarginResult(
        product_id=UUID(str(so.product_id)) if so.product_id else None,
        product_variant_id=UUID(str(so.product_variant_id)) if so.product_variant_id else None,
        revenue=revenue,
        cost=cost,
        shipping_cost=shipping,
        marketplace_fee=mfee,
        payment_fee=payment_fee,
        ad_cost=ad,
        fulfillment_fee=fulfill,
        return_cost=rret,
        vat=vat,
        margin_amount=margin,
        margin_rate=margin_rate,
        matched_expected_id=matched_expected,
        delta_margin_amount=delta_amount,
        delta_margin_rate=delta_rate,
        warnings=warnings,
        settlement_snapshot=_calculation_evidence(
            components=components, so=so, revenue=revenue, cost=cost,
            cost_source=cost_source, shipping=shipping, mfee=mfee,
            fee_source=fee_source, ad=ad, rret=rret, fulfill=fulfill,
            payment_fee=payment_fee, vat=vat, margin=margin, warnings=warnings,
        ),
        settlement_anchor_id=components.anchor_record_id if components else None,
    )


#: 마진 산식. **여기 한 곳에서만 문자열로 적는다** — 화면이 자기 버전을 만들면
#: 코드가 바뀌어도 화면은 옛 식을 보여준다(§15: UI 용 계산식 재작성 금지).
MARGIN_FORMULA = (
    "margin = revenue - cost - shipping - marketplace_fee "
    "- ad_cost - return_cost - fulfillment_fee - payment_fee - vat"
)

#: 성분의 신뢰도. 화면 라벨은 프론트가 번역한다 — 여기서는 사실만 정한다.
ACTUAL, ESTIMATED, UNKNOWN = "ACTUAL", "ESTIMATED", "UNKNOWN"


def _component(value: int, source: str, confidence: str) -> dict[str, Any]:
    return {"value": value, "source": source, "confidence": confidence}


def _calculation_evidence(
    *, components, so, revenue: int, cost: int, cost_source: str, shipping: int,
    mfee: int, fee_source: str, ad: int, rret: int, fulfill: int,
    payment_fee: int, vat: int, margin: int, warnings: list[str],
) -> dict[str, Any]:
    """"이 마진이 왜 이 숫자인가" 에 답할 수 있는 최소 근거.

    왜 여기에 넣나
    ------------
    `product_margin_actual.calculation_snapshot_json` 은 이미 있는 칸이다.
    검증 화면 편의를 위해 재무 사실을 다른 테이블에 복제하지 않는다(§60).

    무엇이 부족했나
    -------------
    예전에는 정산 매칭된 주문만 근거를 남기고, 정책 추정 주문은
    `{"basis": "LEDGER_AND_POLICY"}` 한 줄이 전부였다. 그 한 줄로는 원가가
    어디서 왔는지도, 수수료를 어떤 근거로 뗐는지도 알 수 없다 — 전체의
    대부분이 그 경로다.

    **벤더별 필드를 늘리지 않는다.** 성분마다 `value / source / confidence`
    세 가지만 둔다. 판매처가 늘어도 이 구조는 그대로다.

    `confidence` 의 뜻
    ----------------
    ```
    ACTUAL     외부 원천이 실제로 준 값 (정산서·장부에 기록된 지출)
    ESTIMATED  정책·마스터로 추정한 값
    UNKNOWN    모른다. **금액 0 은 0원이라는 뜻이 아니다**
    ```
    """
    cost_confidence = {
        "ORDER_PURCHASE_COST": ACTUAL,
        "VARIANT_MASTER": ESTIMATED,
        "PRODUCT_MASTER": ESTIMATED,
        "UNKNOWN": UNKNOWN,
    }[cost_source]
    fee_confidence = {
        "SETTLEMENT": ACTUAL,
        "POLICY": ESTIMATED,
        "NO_REVENUE": ACTUAL,      # 매출이 0 이면 수수료 0 은 사실이다
        "POLICY_MISSING": UNKNOWN,
    }[fee_source]

    evidence: dict[str, Any] = {
        "basis": "SETTLEMENT_ACTUAL" if components else "LEDGER_AND_POLICY",
        "formula": MARGIN_FORMULA,
        "components": {
            "revenue": _component(
                revenue, "SETTLEMENT" if components else "ORDER_LEDGER",
                ACTUAL if components else ESTIMATED),
            "cost": _component(cost, cost_source, cost_confidence),
            # 매입 배송비는 장부에 사람이 적은 값이다. 없으면 0 이 맞다.
            "shipping_cost": _component(shipping, "ORDER_LEDGER", ACTUAL),
            "marketplace_fee": _component(mfee, fee_source, fee_confidence),
            "ad_cost": _component(ad, "ORDER_LEDGER", ACTUAL),
            # 아래 셋은 아직 원천이 없다. 0 으로 계산하지만 사실은 모른다 —
            # 그 구분을 여기서 지운다면 검증센터를 만드는 의미가 없다.
            "return_cost": _component(rret, "NOT_AVAILABLE", UNKNOWN),
            "fulfillment_fee": _component(fulfill, "NOT_AVAILABLE", UNKNOWN),
            "payment_fee": _component(payment_fee, "NOT_AVAILABLE", UNKNOWN),
            "vat": _component(vat, "NOT_AVAILABLE", UNKNOWN),
        },
        "margin_amount": margin,
        "warnings": list(warnings),
    }
    if components:
        evidence.update({
            **components.as_snapshot(),
            "ledger_sale_price": int(so.sale_price),
            "revenue_diff_vs_ledger": components.gross_revenue - int(so.sale_price),
        })
    return evidence


def save_actual_margin(
    input: ActualMarginInput, result: ActualMarginResult, db: Session
) -> UUID:
    """product_margin_actual UPSERT (sales_order_id UNIQUE) — 계산 결과 영구화.

    이 호출 없이는 calculate_actual_margin() 결과가 product_margin_actual 에 남지
    않아 services/settlement.py 의 direct_cost 집계(SELECT SUM ... FROM
    product_margin_actual)가 그 sales_order 를 못 잡는다 — 기존에 알려져 있던 gap.
    재계산(예: settlement_record 매칭 후 재실행) 시 같은 행을 갱신하도록 UPSERT.
    호출자가 commit 책임.
    """
    # 잠긴 월의 실 마진을 다시 쓰면 확정 보고한 순이익이 조용히 달라진다.
    # 재계산도 쓰기다 — 읽기 전용이 아니다 (docs/43 Phase 2).
    sale_date = db.execute(
        text("SELECT sale_date FROM sales_order WHERE id = :sid"),
        {"sid": str(input.sales_order_id)},
    ).scalar()
    if sale_date is not None:
        assert_period_writable(
            db, org_id=input.org_id, effective_date=sale_date,
            operation="RECALC_MARGIN", what="실 마진 계산 결과",
        )

    existing = db.execute(
        text("SELECT id FROM product_margin_actual WHERE sales_order_id = :sid"),
        {"sid": str(input.sales_order_id)},
    ).first()
    margin_row_id = UUID(str(existing.id)) if existing else new_id()

    db.execute(
        text(
            """
            INSERT INTO product_margin_actual (
              id, org_id, product_id, product_variant_id, sales_order_id, settlement_record_id,
              actual_revenue, actual_cost, actual_shipping_cost,
              actual_marketplace_fee, actual_payment_fee,
              actual_ad_cost, actual_fulfillment_fee, actual_return_cost, actual_vat,
              actual_margin_amount, actual_margin_rate,
              matched_expected_id, delta_margin_amount, delta_margin_rate,
              calculation_snapshot_json
            ) VALUES (
              :id, :org_id, :product_id, :variant_id, :sales_order_id, :settlement_record_id,
              :revenue, :cost, :shipping, :mfee, :pfee, :ad, :fulfill, :rret, :vat,
              :margin_amount, :margin_rate,
              :matched_expected_id, :delta_amount, :delta_rate,
              CAST(:calc_snapshot AS JSONB)
            )
            ON CONFLICT (sales_order_id) DO UPDATE SET
              -- 상품 연결도 갱신해야 한다. 예전엔 이 두 줄이 빠져 있어서,
              -- 판매 장부를 나중에 상품 마스터에 연결(M014)한 뒤 재계산해도
              -- 최초 INSERT 당시의 NULL 이 그대로 남았다 — 상품별 손익 리포트가
              -- 이 컬럼으로 GROUP BY 하므로 연결 효과가 통째로 사라진다.
              product_id = EXCLUDED.product_id,
              product_variant_id = EXCLUDED.product_variant_id,
              settlement_record_id = EXCLUDED.settlement_record_id,
              actual_revenue = EXCLUDED.actual_revenue,
              actual_cost = EXCLUDED.actual_cost,
              actual_shipping_cost = EXCLUDED.actual_shipping_cost,
              actual_marketplace_fee = EXCLUDED.actual_marketplace_fee,
              actual_payment_fee = EXCLUDED.actual_payment_fee,
              actual_ad_cost = EXCLUDED.actual_ad_cost,
              actual_fulfillment_fee = EXCLUDED.actual_fulfillment_fee,
              actual_return_cost = EXCLUDED.actual_return_cost,
              actual_vat = EXCLUDED.actual_vat,
              actual_margin_amount = EXCLUDED.actual_margin_amount,
              actual_margin_rate = EXCLUDED.actual_margin_rate,
              matched_expected_id = EXCLUDED.matched_expected_id,
              delta_margin_amount = EXCLUDED.delta_margin_amount,
              delta_margin_rate = EXCLUDED.delta_margin_rate,
              -- 계산 근거도 함께 갱신한다. 안 하면 재계산 뒤에도 옛 근거가
              -- 남아 "이 숫자가 왜 이런가" 에 틀린 답을 준다.
              calculation_snapshot_json = EXCLUDED.calculation_snapshot_json,
              updated_at = now()
            """
        ),
        {
            "id": str(margin_row_id),
            "org_id": str(input.org_id),
            "product_id": str(result.product_id) if result.product_id else None,
            "variant_id": str(result.product_variant_id) if result.product_variant_id else None,
            "sales_order_id": str(input.sales_order_id),
            # 호출자가 지목한 행이 아니라 **계산에 실제로 쓴 대표 행**을 남긴다.
            # 예전에는 입력값을 그대로 저장해서, 매칭이 바뀐 뒤에도 옛 행을
            # 가리키고 있었다.
            "settlement_record_id": str(result.settlement_anchor_id)
            if result.settlement_anchor_id
            else None,
            "revenue": result.revenue,
            "cost": result.cost,
            "shipping": result.shipping_cost,
            "mfee": result.marketplace_fee,
            "pfee": result.payment_fee,
            "ad": result.ad_cost,
            "fulfill": result.fulfillment_fee,
            "rret": result.return_cost,
            "vat": result.vat,
            "margin_amount": result.margin_amount,
            "margin_rate": result.margin_rate,
            "calc_snapshot": _json(result.settlement_snapshot)
            if result.settlement_snapshot else None,
            "matched_expected_id": str(result.matched_expected_id)
            if result.matched_expected_id
            else None,
            "delta_amount": result.delta_margin_amount,
            "delta_rate": result.delta_margin_rate,
        },
    )
    return margin_row_id


def _estimate_marketplace_fee(
    revenue: int, marketplace: str, at: date, org_id: UUID, db: Session
) -> tuple[int, str]:
    """정산서가 없을 때 marketplace_fee_policy 로 수수료를 추정한다.

    M010 에서 엑셀 실제 요율로 교체했으므로 추정치의 신뢰도가 낮지 않다.

    **금액과 함께 근거를 돌려준다.** 예전에는 금액만 돌려줘서, 0 원이 "요율이
    0" 인지 "정책을 못 찾아 추정 실패" 인지 구분할 수 없었다. 후자는
    수수료를 통째로 0 으로 계상하는 것이고 마진이 그만큼 부푼다 — 검증센터가
    잡아야 하는 바로 그 상태다(`UNKNOWN_FEE`).

    ```
    POLICY          정책으로 추정했다
    NO_REVENUE      매출이 0 이하라 추정할 것이 없다
    POLICY_MISSING  정책을 못 찾았다 → 금액 0 은 **모른다는 뜻**이다
    ```
    """
    if revenue <= 0:
        return 0, "NO_REVENUE"
    ctx = PolicyContext(org_id=org_id, at=at, marketplace=marketplace)
    try:
        policies = resolve_active_policies(ctx, db)
    except Exception:  # 정책 부재 등 — 추정 실패는 계산 자체를 막지 않는다
        return 0, "POLICY_MISSING"
    return calc_marketplace_fee(revenue, policies.marketplace_fee), "POLICY"


def _resolve_actual_cost(
    purchase_cost, product_id, variant_id, qty: int, db: Session
) -> tuple[int, str]:
    """실 원가와 **그 원가가 어디서 왔는지**.

    sales_order.purchase_cost(라인 합계) 가 있으면 그게 최우선이다. 그 주문에서
    실제로 지출한 금액이라 상품 마스터의 cost_amount(카탈로그 평균)보다
    정확하다. purchase_cost 는 이미 라인 합계라 qty 를 곱하지 않는다 — 반면
    cost_amount 는 단가라 곱한다 (M013).

    ⚠️ `is not None` 이어야 한다. `if purchase_cost:` 로 쓰면 **실제로 0원인 건**
    (취소/반품/증정)이 falsy 로 걸려 상품 마스터 원가로 넘어가고, 팔지도 않은
    주문에 원가만 계상된다. 픽썸 실데이터에서 0원 15건 중 14건이 매출도 0이었다
    — M014 가 이 구분을 위해 컬럼을 nullable 로 바꿨다.

    근거를 함께 돌려주는 이유
    ---------------------
    마지막 `return 0` 은 **"원가가 0원" 이 아니라 "모른다"** 이다. 둘을 같은
    숫자로 돌려주면 마진이 원가만큼 부풀고 아무도 눈치채지 못한다. 금액은
    그대로 두되(계산을 막지 않는다) 근거로 구분한다 — 검증센터가 이걸 본다.

    ```
    ORDER_PURCHASE_COST  그 주문에서 실제로 지출한 금액
    VARIANT_MASTER       옵션 마스터 단가 × 수량
    PRODUCT_MASTER       상품 마스터 단가 × 수량
    UNKNOWN              어느 것도 없다 → 0 은 모른다는 뜻
    ```
    """
    if purchase_cost is not None:
        return int(purchase_cost), "ORDER_PURCHASE_COST"
    if variant_id:
        row = db.execute(
            text("SELECT cost_amount FROM product_variant WHERE id = :id"),
            {"id": str(variant_id)},
        ).first()
        if row and row.cost_amount is not None:
            return int(row.cost_amount) * qty, "VARIANT_MASTER"
    if product_id is None:
        return 0, "UNKNOWN"
    row = db.execute(
        text("SELECT cost_amount FROM product WHERE id = :id"),
        {"id": str(product_id)},
    ).first()
    if row and row.cost_amount is not None:
        return int(row.cost_amount) * qty, "PRODUCT_MASTER"
    return 0, "UNKNOWN"


def _match_expected(product_id, variant_id, actual_margin: int, actual_rate: Decimal, db: Session):
    # 상품 마스터에 링크되지 않은 판매(엑셀 import 등)는 대조할 예상 마진이 없다.
    # str(None) == 'None' 이 그대로 UUID 파라미터로 나가면 DataError 가 난다.
    if product_id is None:
        return None, None, None
    row = db.execute(
        text(
            """
            SELECT id, computed_margin_amount, computed_margin_rate
              FROM product_margin_expected
             WHERE product_id = :pid
               AND (product_variant_id = :vid OR (:vid IS NULL AND product_variant_id IS NULL))
               AND deleted_at IS NULL
             ORDER BY valid_from DESC LIMIT 1
            """
        ),
        {"pid": str(product_id), "vid": str(variant_id) if variant_id else None},
    ).first()
    if row is None:
        return None, None, None
    return (
        UUID(str(row.id)),
        actual_margin - int(row.computed_margin_amount),
        (actual_rate - Decimal(str(row.computed_margin_rate))).quantize(Decimal("0.0001")),
    )


def _json(d: dict | None) -> str | None:
    if d is None:
        return None
    import json

    from app.services.policy import _jsonable

    return json.dumps(d, default=_jsonable)
