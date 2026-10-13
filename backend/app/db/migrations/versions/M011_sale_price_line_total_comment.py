"""M011 sales_order.sale_price 의 의미를 DB COMMENT 로 못박는다.

배경:
  `sale_price` 라는 이름은 "단가"로 읽히지만, 실제로 저장된 값은 **라인 합계**다.
  import 원본인 엑셀 `판매 장부` 시트의 컬럼이 `판매가+배송비`이고, 그 시트의
  `단순 수익` = `판매가+배송비` - `구매비용` - `구매 배송비` 로 계산된다 —
  `판매 개수`가 어디에도 곱해지지 않는다. 즉 원본부터 라인 합계다.

  검증 (2026-07-30, 픽썸 실데이터 365건):
    SUM(sale_price)          가 엑셀 월별매출과 원 단위까지 일치
    SUM(sale_price*quantity) 는 불일치 (2025-02: 1,817,400 vs 실제 1,256,200)

  이름 때문에 백엔드 9곳이 `sale_price * quantity` 로 매출을 이중 계산하고
  있었다(settlement / margin / margin_analytics / ad_cost_allocation /
  reports / tax_reports). 같은 실수가 재발하지 않도록 컬럼 정의를 DB 에 남긴다.

  주의: `product.cost_amount` / `product_variant.cost_amount` 는 반대로 **단가**다.
  그래서 원가 쪽에는 quantity 를 곱하는 게 맞다 (services/margin.py
  `_resolve_actual_cost`). 매출만 곱하지 않는다.

컬럼명을 sale_amount 로 rename 하지 않은 이유:
  API 계약(domain/sales.py)과 프론트 6개 파일이 이미 sale_price 를 쓰고 있어
  rename 비용이 크다. 대신 DB COMMENT + 서비스 주석 + 화면 라벨
  ("판매가+배송비 (합계)") 3중으로 의미를 노출한다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M011"
down_revision: str | None = "M010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = (
    "라인 합계 (엑셀 `판매가+배송비`). 단가가 아니다 — "
    "매출 집계 시 quantity 를 곱하지 말 것. 원가(cost_amount)는 단가라 곱해야 함."
)


def upgrade() -> None:
    op.execute(f"COMMENT ON COLUMN sales_order.sale_price IS '{_COMMENT}'")
    op.execute(
        "COMMENT ON COLUMN sales_order.quantity IS "
        "'판매 개수. 원가 산정에만 쓰이고 매출(sale_price)에는 곱하지 않는다.'"
    )


def downgrade() -> None:
    op.execute("COMMENT ON COLUMN sales_order.sale_price IS NULL")
    op.execute("COMMENT ON COLUMN sales_order.quantity IS NULL")
