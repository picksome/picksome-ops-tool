"""M016 이익 4단계(EXPECTED/PROVISIONAL/SETTLED/FINAL) 구분.

배경:
  지금은 순이익이 하나뿐이다. 그런데 셀러 입장에서 "지금 보이는 이익"은 시점마다
  의미가 완전히 다르다.

    상품 등록 시점   — 예상 수수료·예상 반품률로 추정한 값
    주문 발생 직후   — 실제 판매가·쿠폰·수량은 확정, 수수료는 아직 추정
    정산 매칭 후     — 마켓이 실제로 뗀 수수료·광고비·실입금이 반영된 값
    월 마감 후       — 반품 가능 기간이 끝나고 비용 배분까지 끝난 확정값

  이걸 한 숫자로 뭉개면 "예상 이익 800만" 을 보고 돈을 썼는데 정산 후 620만이
  들어오는 일이 생긴다. 대시보드 KPI 에서 예상과 확정을 섞는 것도 같은 문제다.

설계 판단:
  **신규 테이블을 만들지 않는다.** 이미 있는 것을 확장한다.
    - product_margin_expected  → EXPECTED 단계 (그대로 사용)
    - product_margin_actual    → PROVISIONAL/SETTLED/FINAL 금액 컬럼만 추가
    - sales_order              → 현재 상태와 시각 컬럼 추가

  상태값을 ENUM 이 아니라 **TEXT + CHECK** 로 만든다. PostgreSQL 은
  `ALTER TYPE ... ADD VALUE` 를 트랜잭션 안에서 못 쓰고 값 제거도 지원하지 않아
  롤백이 불가능하다. CHECK 는 DROP CONSTRAINT 로 되돌릴 수 있다.
  (M007 이 marketplace 를 ENUM→TEXT 로 바꾼 것과 같은 이유)

기존 데이터 처리:
  기존 sales_order 는 전부 이미 발생한 주문이므로 PROVISIONAL 로 시작한다.
  EXPECTED 로 되돌리면 "아직 안 팔린 주문" 이라는 모순이 생긴다.
  정산 매칭이 이미 있는 행(settlement_record_id NOT NULL)은 SETTLED 로 올린다.

  FINAL 로는 아무 행도 올리지 않는다. FINAL 은 반품 기한 경과 + 월 잠금이
  전제인데 둘 다 이 마이그레이션 시점에는 판정할 수 없다. 나중에
  services/profit_status.finalize_month 가 월 잠금 시점에 올린다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M016"
down_revision: str | None = "M015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- ── sales_order: 현재 이익 단계 ──────────────────────────────
        ALTER TABLE sales_order
          ADD COLUMN IF NOT EXISTS profit_status TEXT,
          ADD COLUMN IF NOT EXISTS profit_calculated_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS profit_finalized_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS return_window_ends_at DATE;

        -- 기존 행 backfill: 정산 매칭이 있으면 SETTLED, 없으면 PROVISIONAL
        UPDATE sales_order so
           SET profit_status = CASE
                 WHEN EXISTS (
                   SELECT 1 FROM product_margin_actual pma
                    WHERE pma.sales_order_id = so.id
                      AND pma.settlement_record_id IS NOT NULL
                      AND pma.deleted_at IS NULL
                 ) THEN 'SETTLED'
                 ELSE 'PROVISIONAL'
               END
         WHERE profit_status IS NULL;

        ALTER TABLE sales_order
          ALTER COLUMN profit_status SET DEFAULT 'PROVISIONAL',
          ALTER COLUMN profit_status SET NOT NULL;

        ALTER TABLE sales_order
          ADD CONSTRAINT ck_sales_order_profit_status
          CHECK (profit_status IN ('EXPECTED','PROVISIONAL','SETTLED','FINAL'));

        COMMENT ON COLUMN sales_order.profit_status IS
          '이익 확정 단계. EXPECTED=판매 전 시뮬레이션 / PROVISIONAL=주문 발생, 수수료는 추정 / '
          'SETTLED=마켓 정산 매칭 완료 / FINAL=반품기한 경과 + 월 잠금 완료(변경 불가). '
          '역방향 전이는 월 잠금 해제를 선행해야 한다.';
        COMMENT ON COLUMN sales_order.return_window_ends_at IS
          '반품 가능 기한. 이 날짜가 지나야 FINAL 로 갈 수 있다. '
          'NULL 이면 org 설정 기본값(profit.return_window_days)으로 판정한다.';

        CREATE INDEX IF NOT EXISTS ix_sales_order_profit_status
          ON sales_order (org_id, profit_status, sale_date);

        -- ── product_margin_actual: 단계별 금액 + 계산 근거 박제 ───────
        ALTER TABLE product_margin_actual
          ADD COLUMN IF NOT EXISTS provisional_profit_amount BIGINT,
          ADD COLUMN IF NOT EXISTS settled_profit_amount BIGINT,
          ADD COLUMN IF NOT EXISTS final_profit_amount BIGINT,
          ADD COLUMN IF NOT EXISTS calculation_snapshot_json JSONB,
          ADD COLUMN IF NOT EXISTS applied_policy_versions_json JSONB;

        -- 기존 actual_margin_amount 는 "정산 전 추정" 이었다 → PROVISIONAL 로 옮긴다.
        -- actual_margin_amount 자체는 지우지 않는다. 기존 정산·리포트 쿼리가 읽고 있다.
        UPDATE product_margin_actual
           SET provisional_profit_amount = actual_margin_amount
         WHERE provisional_profit_amount IS NULL;

        UPDATE product_margin_actual
           SET settled_profit_amount = actual_margin_amount
         WHERE settlement_record_id IS NOT NULL
           AND settled_profit_amount IS NULL;

        COMMENT ON COLUMN product_margin_actual.provisional_profit_amount IS
          '주문 확정 직후 순이익. 판매가·수량·쿠폰은 실제값, 수수료는 정책 추정값.';
        COMMENT ON COLUMN product_margin_actual.settled_profit_amount IS
          '마켓 정산 매칭 후 순이익. 실제 수수료·광고비 차감·실입금 반영.';
        COMMENT ON COLUMN product_margin_actual.final_profit_amount IS
          '월 마감 후 확정 순이익. 반품 반영 + 사업비용 배분 완료. 변경 불가.';
        -- 주의) SQLAlchemy 는 콜론 뒤에 오는 단어를 bind parameter 로 해석한다(한글 포함).
        --       그래서 이 파일의 SQL 문자열에는 JSON 예시를 콜론 없이 화살표로 적는다.
        COMMENT ON COLUMN product_margin_actual.applied_policy_versions_json IS
          '계산 시점에 적용된 정책 버전. 예) tax -> 3, marketplace_fee -> 2. '
          '정책이 나중에 바뀌어도 이 값으로 과거를 재현한다 — 자동 재계산 금지.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_sales_order_profit_status;

        ALTER TABLE sales_order
          DROP CONSTRAINT IF EXISTS ck_sales_order_profit_status;

        ALTER TABLE sales_order
          DROP COLUMN IF EXISTS profit_status,
          DROP COLUMN IF EXISTS profit_calculated_at,
          DROP COLUMN IF EXISTS profit_finalized_at,
          DROP COLUMN IF EXISTS return_window_ends_at;

        ALTER TABLE product_margin_actual
          DROP COLUMN IF EXISTS provisional_profit_amount,
          DROP COLUMN IF EXISTS settled_profit_amount,
          DROP COLUMN IF EXISTS final_profit_amount,
          DROP COLUMN IF EXISTS calculation_snapshot_json,
          DROP COLUMN IF EXISTS applied_policy_versions_json;
        """
    )
