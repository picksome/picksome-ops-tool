"""M008 환율 자동화 — exchange_rate_snapshot 테이블 신설.

배경 (docs/27_dynamic_channel_and_bot_integration_plan.md D-288):
  product.cost_fx_rate(M005)는 지금까지 수동 입력만 가능했다("환율은 별도 환율
  정책/입력"으로 Post-MVP 남겨뒀던 부분). 이 테이블은 한국수출입은행 Open API
  고시환율을 매일 스냅샷으로 적재해, 원가 입력 시 자동으로 최신값을 채워주는
  근거 데이터가 된다(services/exchange_rate.py).

  org 단위가 아니라 전역 테이블 — 환율은 조직마다 다르지 않은 객관적 시장 데이터라
  org_id 로 쪼갤 이유가 없고, 쪼개면 org 마다 같은 API 를 중복 호출하게 된다.

조치:
  exchange_rate_snapshot(currency, rate_to_krw, rate_date, source, fetched_at)
  UNIQUE(currency, rate_date) — 같은 날 재조회하면 UPSERT.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M008"
down_revision: str | None = "M007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE exchange_rate_snapshot (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          currency TEXT NOT NULL,
          rate_to_krw NUMERIC(12,4) NOT NULL,
          rate_date DATE NOT NULL,
          source TEXT NOT NULL DEFAULT 'MANUAL',
          fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX uq_exchange_rate_currency_date
          ON exchange_rate_snapshot(currency, rate_date);
        CREATE INDEX ix_exchange_rate_currency_date_desc
          ON exchange_rate_snapshot(currency, rate_date DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE exchange_rate_snapshot")
