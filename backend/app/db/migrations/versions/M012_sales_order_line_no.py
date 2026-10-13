"""M012 sales_order 에 line_no 추가 — 한 마켓 주문에 여러 라인 허용.

배경:
  M001 의 `uq_sales_order_mkt_orderno UNIQUE (org_id, marketplace,
  marketplace_order_no) WHERE deleted_at IS NULL` 은 "마켓 주문 1건 = 판매장부
  1행"을 가정했다. 그런데 이 가정이 틀렸다.

  `marketplace_order_no` 는 각 쇼핑몰이 발급한 주문번호를 그대로 옮긴 값이고,
  재구매를 하면 주문번호도 새로 발급된다(사용자 확인, 2026-07-31). 따라서
  **같은 주문번호가 여러 행에 나타나면 그건 한 주문의 여러 라인**이다 —
  상품이 여러 개거나, 분할 배송되어 출고일이 나뉜 경우.

  이 제약 때문에 `scripts/import_data.sql` 의 `ON CONFLICT DO NOTHING` 이
  2~3번째 라인을 **에러 한 줄 없이 조용히 버렸다**. 픽썸 실데이터에서
  9행 / 매출 146,440원이 유실됐다:
    2025-07  3행  40,000원   (14100127297442 x2, 17100128167282 x3)
    2025-12  1행  16,700원   (12100157392979 x2 — 같은 날 같은 상품, 금액 다름)
    2026-07  3행  89,740원   (31101555112499 x2, 30101563959636 x3)
    2025-03  1행       0원
    2025-08  1행       0원   (판매처·주문번호 공란 행끼리 충돌)

조치:
  line_no 를 추가하고 유니크 키에 포함시킨다. 기존 행은 전부 1번 라인이다
  (원래 각 주문번호당 첫 행만 살아남았으므로 line_no=1 이 정확하다).

  DEFAULT 1 을 남겨두는 이유: 단일 라인 주문이 절대다수라 API/봇 경로가
  line_no 를 안 보내도 자연스럽게 동작해야 한다. 여러 라인을 넣을 때만
  services/sales.py 가 다음 번호를 자동 채번한다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M012"
down_revision: str | None = "M011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sales_order
          ADD COLUMN line_no INT NOT NULL DEFAULT 1,
          ADD CONSTRAINT ck_sales_order_line_no CHECK (line_no >= 1);

        COMMENT ON COLUMN sales_order.line_no IS
          '한 마켓 주문(marketplace_order_no) 안에서의 라인 번호. 1부터 시작. '
          '단일 상품 주문이면 항상 1.';

        DROP INDEX IF EXISTS uq_sales_order_mkt_orderno;
        CREATE UNIQUE INDEX uq_sales_order_mkt_orderno
          ON sales_order(org_id, marketplace, marketplace_order_no, line_no)
          WHERE deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    # line_no > 1 인 행이 있으면 되돌릴 때 유니크 제약이 깨지므로 먼저 지운다.
    op.execute(
        """
        DELETE FROM sales_order WHERE line_no > 1;

        DROP INDEX IF EXISTS uq_sales_order_mkt_orderno;
        CREATE UNIQUE INDEX uq_sales_order_mkt_orderno
          ON sales_order(org_id, marketplace, marketplace_order_no)
          WHERE deleted_at IS NULL;

        ALTER TABLE sales_order
          DROP CONSTRAINT ck_sales_order_line_no,
          DROP COLUMN line_no;
        """
    )
