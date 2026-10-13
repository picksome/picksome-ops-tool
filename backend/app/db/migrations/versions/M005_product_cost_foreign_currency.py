"""M005 상품 원가 — 현지가격(cost_amount_foreign) 컬럼 추가.

배경:
  product.cost_currency / cost_fx_rate 컬럼은 M001부터 있었지만 계산에 전혀
  쓰이지 않는 죽은 필드였다 (그냥 저장만 되고 cost_amount 는 항상 사용자가
  KRW로 직접 입력). "구매 시점 환율을 반영해달라"는 요청에 대응하기 위해,
  해외 사입/구매대행 상품은 "현지가격 × 환율 = cost_amount(KRW)" 를
  서버에서 자동 계산하도록 한다 (services/product.py 동반 변경).

  실시간 환율 API 연동, 재고 lot별 원가관리(FIFO)는 이번 범위에 포함하지
  않음 — Post-MVP. 지금은 "최신 원가 1개" 모델 유지, cost_fx_rate 는
  수동 입력값을 그대로 사용.

조치:
  product.cost_amount_foreign BIGINT NULL 추가 — 현지통화 기준 공급가.
  cost_currency='KRW'(또는 NULL)면 무시하고 cost_amount 를 직접 입력값으로 사용.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M005"
down_revision: str | None = "M004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE product ADD COLUMN cost_amount_foreign BIGINT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE product DROP COLUMN cost_amount_foreign")
