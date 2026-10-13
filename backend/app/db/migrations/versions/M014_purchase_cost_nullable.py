"""M014 sales_order.purchase_cost 를 nullable 로 — "미입력" 과 "실제 0원" 구분.

배경:
  M013 이 purchase_cost 를 `NOT NULL DEFAULT 0` 으로 만들었다. 그래서
  services/margin.py 의 `_resolve_actual_cost` 가 `if purchase_cost:` 로
  0 을 falsy 취급해 상품 마스터의 cost_amount × qty 로 넘어갔다.

  상품 마스터가 비어 있던 동안에는 어차피 0 이라 티가 안 났는데,
  상품 1,061건을 import 하고 sales_order 에 연결하자마자 문제가 드러났다:
  엑셀 대조 일치가 15/19 → 12/19 로 떨어졌다.

  원인을 보니 purchase_cost=0 인 15건 중 **14건은 매출도 0** 이었다.
  취소/반품 등으로 거래가 성립하지 않은 행이다. 여기에 상품 마스터 원가를
  끼워 넣으면 팔지도 않은 건에 원가만 계상된다.

조치:
  purchase_cost 를 nullable 로 바꾸고 DEFAULT 를 없앤다. 의미를 나눈다.
    purchase_cost IS NULL  → 매입액 미상. 상품 마스터 단가 × qty 로 추정.
    purchase_cost = 0      → 실제로 0원. 그대로 0 으로 존중.

  기존 EXCEL_IMPORT 행의 0 은 그대로 둔다 — 엑셀 원본에서도 명시적 0 이라
  0 이 맞는 값이다(빈칸은 374건 중 3건뿐이고 그중 2건은 매출도 0).
  generate_import_sql.py 는 앞으로 빈칸을 NULL 로 넣도록 같이 고쳤다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M014"
down_revision: str | None = "M013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sales_order
          ALTER COLUMN purchase_cost DROP NOT NULL,
          ALTER COLUMN purchase_cost DROP DEFAULT;

        COMMENT ON COLUMN sales_order.purchase_cost IS
          '구매비용 — 라인 합계다. 단가가 아니므로 quantity 를 곱하지 말 것 '
          '(sale_price 와 동일 규칙, M011 참조). '
          'NULL = 매입액 미상(상품 마스터 단가로 추정), 0 = 실제로 0원(취소/증정). '
          '매입 배송비는 shipping_fee 에 별도.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE sales_order SET purchase_cost = 0 WHERE purchase_cost IS NULL;
        ALTER TABLE sales_order
          ALTER COLUMN purchase_cost SET DEFAULT 0,
          ALTER COLUMN purchase_cost SET NOT NULL;
        """
    )
