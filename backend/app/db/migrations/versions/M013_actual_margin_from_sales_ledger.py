"""M013 판매 장부 기반 실 마진 계산이 가능하도록 3가지를 고친다.

배경:
  product_margin_actual 이 0건이라 월정산의 direct_cost 가 항상 0이었고,
  순이익이 매출과 거의 같게 나왔다. 채우려고 보니 경로 자체가 막혀 있었다.

1) 원가가 memo 문자열에 박혀 있었다
   scripts/generate_import_sql.py 가 엑셀 `구매비용` 을 넣을 컬럼이 없어
   `memo = '구매비용:37070'` 처럼 문자열로 우겨넣었다. 계산에 못 쓴다.
   → sales_order.purchase_cost 컬럼을 만들고 memo 에서 역파싱해 backfill.

   purchase_cost 는 sale_price 와 마찬가지로 **라인 합계**다 (M011 참조).
   엑셀 `단순 수익` = `판매가+배송비` - `구매비용` - `구매 배송비` 에
   `판매 개수`가 곱해지지 않는다. 반면 product.cost_amount 는 단가다.

2) product_margin_actual.product_id 가 NOT NULL 이었다
   그런데 sales_order.product_id 는 NULL 허용이고, 실제로 import 된
   374건 전부 NULL 이다(상품 마스터 import 가 아직 안 됨 — 판매장부에는
   상품명 스냅샷만 있다). NOT NULL 이면 실 마진을 한 건도 못 넣는다.

   실 마진의 정체성은 sales_order_id(UNIQUE)이지 product_id 가 아니다.
   상품 링크는 있으면 좋은 부가정보다. sales_order 와 nullable 여부를
   맞춘다.

3) 구매 배송비를 담을 곳이 없었다
   CLAUDE.md Settlement Model 의 상품 직접비는
   `원가 + 마켓수수료 + 광고비 + 반품비 + 풀필먼트비 + 배송비` 인데
   product_margin_actual 에 배송비 컬럼이 없었다.
   actual_cost 에 합쳐버리면 세무사 리포트에서 원가와 배송비를 분리할 수
   없으므로 actual_shipping_cost 를 별도로 둔다.
   services/settlement.py 의 direct_cost 합계에도 포함시킨다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M013"
down_revision: str | None = "M012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- 1) 구매비용 컬럼 + memo 역파싱 backfill
        ALTER TABLE sales_order
          ADD COLUMN purchase_cost BIGINT NOT NULL DEFAULT 0,
          ADD CONSTRAINT ck_sales_order_purchase_cost CHECK (purchase_cost >= 0);

        COMMENT ON COLUMN sales_order.purchase_cost IS
          '구매비용 — 라인 합계다. 단가가 아니므로 quantity 를 곱하지 말 것 '
          '(sale_price 와 동일 규칙, M011 참조). 매입 배송비는 shipping_fee 에 별도.';

        COMMENT ON COLUMN sales_order.shipping_fee IS
          '구매(매입) 배송비 — 라인 합계. 판매 배송비는 sale_price 에 이미 포함돼 있다.';

        UPDATE sales_order
           SET purchase_cost = CAST(substring(memo from '^구매비용:([0-9]+)$') AS BIGINT),
               memo = NULL
         WHERE memo ~ '^구매비용:[0-9]+$';

        -- 2) 실 마진은 sales_order 에 매달린다. 상품 링크는 선택.
        ALTER TABLE product_margin_actual ALTER COLUMN product_id DROP NOT NULL;

        -- 3) 매입 배송비를 원가와 분리해 보관
        ALTER TABLE product_margin_actual
          ADD COLUMN actual_shipping_cost BIGINT NOT NULL DEFAULT 0;

        COMMENT ON COLUMN product_margin_actual.actual_shipping_cost IS
          '매입 배송비. actual_cost(상품 원가)와 분리해 세무사 리포트에서 따로 뽑을 수 있게 한다.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE product_margin_actual DROP COLUMN actual_shipping_cost;

        -- product_id 를 다시 NOT NULL 로 되돌리려면 NULL 인 행을 지워야 한다.
        DELETE FROM product_margin_actual WHERE product_id IS NULL;
        ALTER TABLE product_margin_actual ALTER COLUMN product_id SET NOT NULL;

        UPDATE sales_order
           SET memo = '구매비용:' || purchase_cost::text
         WHERE memo IS NULL AND purchase_cost > 0;

        ALTER TABLE sales_order
          DROP CONSTRAINT ck_sales_order_purchase_cost,
          DROP COLUMN purchase_cost;
        """
    )
