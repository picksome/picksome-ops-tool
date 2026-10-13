"""주문 단위 정산 컴포넌트 (docs/83, 결정 D1·D2·D3).

왜 주문 단위인가
--------------
쿠팡 정산서는 주문 하나를 **행 셋**으로 준다(실측 22개 주문 전부).

```
주문 A
├─ 상품 행          gross_revenue = 상품매출
├─ <기본배송료> 행   gross_revenue = 배송비매출
└─ <추가배송료> 행   gross_revenue = 배송비매출
```

`product_margin_actual` 은 **주문 라인 하나당 한 행**이다. 여기에 정산 행
하나만 골라 붙이면 나머지 둘이 사라진다 — 배송료 행을 고르면 매출이 0 이
되고, 상품 행을 고르면 배송비 매출이 통째로 빠진다.

그래서 행을 고르지 않고 **주문 단위로 합친다.**

저장하지 않는다 — 파생 read model 이다
----------------------------------
합계를 컬럼으로 박으면 `marketplace_settlement_record` 와 두 번째 truth 가
된다. 정산 행이 하나 더 들어오거나 매칭이 바뀌면 둘이 갈라지고, 그때 어느
쪽이 사실인지 알 방법이 없다. 관측 fact 는 행에만 있고 여기서는 매번 읽는다.

무엇을 매출로 보는가 (승인된 결정)
------------------------------
```
D1  배송비매출을 공헌이익에 포함한다
D2  판매자 부담 쿠폰을 매출 차감으로 본다
D3  매칭된 주문은 실제 수수료를 정책 추정보다 우선한다
```

```
순매출 = 상품매출 + 배송비매출 - 판매자쿠폰
```

실측 67행에서 `순매출 - 실수수료 = 정산금액` 이 **차이 0원**으로 성립한다.
그 항등식이 이 모델의 근거이자 검산이다.

배송비 **매출**과 배송비 **비용**은 다른 축이다
------------------------------------------
```
정산서 <기본배송료> 행     고객에게 받아 정산되는 돈   → 매출
sales_order.shipping_fee  우리가 내는 매입·출고 배송비 → 비용
fulfillment_fee_policy    로켓그로스 입출고·보관비     → 비용
```

셋을 섞으면 부호와 의미가 동시에 뒤집힌다. 여기서는 **매출만** 다룬다.

미매칭 주문은 다루지 않는다
------------------------
`MATCHED_EXACT` · `MATCHED_MANUAL` 만 본다. `AMBIGUOUS` 는 어느 라인의
정산금인지 모르는 상태라 특정 주문의 손익 근거로 쓸 수 없고, `UNMATCHED` ·
`EXCLUDED` 는 붙을 주문이 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

#: 손익 근거로 쓸 수 있는 매칭 상태. 사람이 승인을 거쳐 붙인 것도 포함한다 —
#: 규칙이 붙였는지 사람이 붙였는지는 **연결의 근거**이지 금액의 성질이 아니다.
POSTABLE_STATUSES = ("MATCHED_EXACT", "MATCHED_MANUAL")

#: 상품 매출 행. 나머지 배송료 행과 경제적 의미가 다르다(M056 row_type).
_PRODUCT = "PRODUCT_SALE"


@dataclass(frozen=True, slots=True)
class SettlementComponents:
    """주문 하나에 붙은 정산 행들의 경제적 분해."""

    sales_order_id: UUID
    product_revenue: int
    delivery_revenue: int
    #: 판매자가 부담한 할인쿠폰. 정산에서 차감된다(D2).
    seller_coupon: int
    #: 벤더가 실제로 뗀 판매수수료. **VAT 포함 실측값**이다(docs/83 §B).
    marketplace_fee: int
    #: 벤더가 준 정산금액 합. 우리가 계산한 값이 아니다 — 검산에 쓴다.
    net_settlement: int
    row_count: int
    record_ids: list[UUID] = field(default_factory=list)
    #: 대표 행(상품 행). `product_margin_actual.settlement_record_id` 는 단일
    #: FK 라 여러 행을 가리킬 수 없어서, 상품 식별을 담은 행을 기준점으로 둔다.
    #: 전체 목록은 `record_ids` 에 있고 계산 snapshot 에 함께 박제된다.
    anchor_record_id: UUID | None = None

    @property
    def gross_revenue(self) -> int:
        """쿠폰 차감 전 매출. 상품 + 배송비."""
        return self.product_revenue + self.delivery_revenue

    @property
    def net_revenue(self) -> int:
        """공헌이익 계산에 쓰는 매출 (D1 + D2)."""
        return self.gross_revenue - self.seller_coupon

    def reconciles(self) -> bool:
        """벤더 정산금액과 맞는가.

        맞지 않으면 우리가 컴포넌트를 잘못 나눈 것이다 — 실측 67행에서는
        차이 0원이었다. 어긋나면 그 사실을 조용히 넘기지 않는다.
        """
        return self.net_revenue - self.marketplace_fee == self.net_settlement

    def as_snapshot(self) -> dict[str, Any]:
        """계산 근거 박제용. 나중에 "이 마진이 왜 이 숫자인가" 에 답한다."""
        return {
            "product_revenue": self.product_revenue,
            "delivery_revenue": self.delivery_revenue,
            "seller_coupon": self.seller_coupon,
            "gross_revenue": self.gross_revenue,
            "net_revenue": self.net_revenue,
            "marketplace_fee_actual": self.marketplace_fee,
            "vendor_net_settlement": self.net_settlement,
            "reconciles": self.reconciles(),
            "settlement_rows": self.row_count,
            "settlement_record_ids": [str(i) for i in self.record_ids],
        }


_AGG = f"""
    SELECT m.matched_sales_order_id AS sales_order_id,
           COALESCE(SUM(m.gross_revenue)
                    FILTER (WHERE m.row_type = '{_PRODUCT}'), 0)  AS product_revenue,
           COALESCE(SUM(m.gross_revenue)
                    FILTER (WHERE m.row_type <> '{_PRODUCT}'), 0) AS delivery_revenue,
           COALESCE(SUM(m.other_deductions), 0)                   AS seller_coupon,
           COALESCE(SUM(m.marketplace_fee), 0)                    AS marketplace_fee,
           COALESCE(SUM(m.net_settlement), 0)                     AS net_settlement,
           COUNT(*)                                               AS row_count,
           ARRAY_AGG(m.id ORDER BY m.row_type, m.line_reference)  AS record_ids,
           (ARRAY_AGG(m.id ORDER BY (m.row_type = '{_PRODUCT}') DESC,
                                    m.line_reference))[1]         AS anchor_record_id
      FROM marketplace_settlement_record m
     WHERE m.org_id = :org
       AND m.deleted_at IS NULL
       AND m.matched_sales_order_id IS NOT NULL
       AND m.matching_status = ANY(:statuses)
       {{extra}}
     GROUP BY m.matched_sales_order_id
"""


def _row_to_components(row: Any) -> SettlementComponents:
    return SettlementComponents(
        sales_order_id=row["sales_order_id"],
        product_revenue=int(row["product_revenue"]),
        delivery_revenue=int(row["delivery_revenue"]),
        seller_coupon=int(row["seller_coupon"]),
        marketplace_fee=int(row["marketplace_fee"]),
        net_settlement=int(row["net_settlement"]),
        row_count=int(row["row_count"]),
        record_ids=list(row["record_ids"] or []),
        anchor_record_id=row["anchor_record_id"],
    )


def load_for_orders(
    db: Session, *, org_id: UUID, sales_order_ids: list[UUID],
) -> dict[UUID, SettlementComponents]:
    """여러 주문의 컴포넌트를 **한 번의 조회로** 가져온다.

    한 달치 재계산이 주문마다 조회하면 수백 번이 된다. 호출자가 반복문
    안에서 `load_for_order` 를 부르지 않도록 이 경로를 먼저 둔다.
    """
    if not sales_order_ids:
        return {}
    rows = db.execute(
        text(_AGG.format(extra="AND m.matched_sales_order_id = ANY(:ids)")),
        {"org": str(org_id), "statuses": list(POSTABLE_STATUSES),
         "ids": [str(i) for i in sales_order_ids]},
    ).mappings().all()
    return {r["sales_order_id"]: _row_to_components(r) for r in rows}


def load_for_order(
    db: Session, *, org_id: UUID, sales_order_id: UUID,
) -> SettlementComponents | None:
    """주문 하나. 붙은 정산 행이 없으면 `None` — **0 이 아니다.**

    0 을 돌려주면 "정산금 0원" 과 "정산서가 아직 없음" 이 같아지고, 호출자는
    매출을 0 으로 계산한다.
    """
    return load_for_orders(
        db, org_id=org_id, sales_order_ids=[sales_order_id],
    ).get(sales_order_id)


def load_all(db: Session, *, org_id: UUID) -> dict[UUID, SettlementComponents]:
    """조직 전체. 대사·검증 화면이 쓴다."""
    rows = db.execute(
        text(_AGG.format(extra="")),
        {"org": str(org_id), "statuses": list(POSTABLE_STATUSES)},
    ).mappings().all()
    return {r["sales_order_id"]: _row_to_components(r) for r in rows}
