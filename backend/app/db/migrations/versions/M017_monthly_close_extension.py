"""M017 월 마감 상태 확장 — OPEN / REVIEW / CONFIRMED / LOCKED.

배경:
  monthly_settlement 에 이미 status(DRAFT/CONFIRMED/LOCKED/REOPENED)와
  locked_at/by, confirmed_at/by, reopen_count 가 있다. 필요한 건 두 가지다.

    1. **REVIEW 단계** — 담당자가 숫자를 맞춘 뒤 승인자가 검토하는 구간.
       지금은 DRAFT 에서 바로 CONFIRMED 로 뛴다.
    2. **마감 전 검증 결과 저장** — 미매칭 정산, 증빙 없는 비용, 원가 0원 판매 같은
       항목을 마감 시점에 확인하고 그 결과를 남겨야 한다. 나중에 "왜 이 달을
       확정했지?" 를 되짚을 수 있어야 한다.

기존 status 를 갈아치우지 않는 이유:
  services/settlement.py, settlement_crud.py, reports.py 가 status 를 읽고 있고
  REOPEN 한도 로직(D-285)도 여기에 걸려 있다. 한 번에 바꾸면 회귀가 난다.
  close_state 를 새로 두고 status 에서 backfill 한 뒤, 신규 코드가 close_state 를
  쓰도록 한다. status 는 당분간 동기화된 상태로 남는다.

REOPENED 를 상태로 두지 않는 이유:
  "재개된 달" 은 결국 다시 열린 달(OPEN)이다. REOPENED 를 별도 상태로 두면
  "재개된 뒤 다시 확정한 달" 을 표현할 수 없다. 재개 사실은 감사 이벤트
  (audit_log.action='STATUS_CHANGE' + reason)와 reopen_count 로 남긴다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M017"
down_revision: str | None = "M016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE monthly_settlement
          ADD COLUMN IF NOT EXISTS close_state TEXT,
          ADD COLUMN IF NOT EXISTS review_requested_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS review_requested_by UUID REFERENCES app_user(id),
          ADD COLUMN IF NOT EXISTS validation_result_json JSONB,
          ADD COLUMN IF NOT EXISTS validated_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS acknowledged_warnings_json JSONB;

        -- 기존 status → close_state backfill.
        -- REOPENED 는 "다시 열린 달" 이므로 OPEN 으로 본다(재개 사실은 reopen_count 가 보존).
        UPDATE monthly_settlement
           SET close_state = CASE status
                 WHEN 'DRAFT'     THEN 'OPEN'
                 WHEN 'REOPENED'  THEN 'OPEN'
                 WHEN 'CONFIRMED' THEN 'CONFIRMED'
                 WHEN 'LOCKED'    THEN 'LOCKED'
                 ELSE 'OPEN'
               END
         WHERE close_state IS NULL;

        ALTER TABLE monthly_settlement
          ALTER COLUMN close_state SET DEFAULT 'OPEN',
          ALTER COLUMN close_state SET NOT NULL;

        ALTER TABLE monthly_settlement
          ADD CONSTRAINT ck_monthly_settlement_close_state
          CHECK (close_state IN ('OPEN','REVIEW','CONFIRMED','LOCKED'));

        COMMENT ON COLUMN monthly_settlement.close_state IS
          '월 마감 상태. OPEN=작성중 / REVIEW=검토중 / CONFIRMED=확정 / LOCKED=잠금(수정 불가). '
          '재개(REOPEN)는 별도 상태가 아니라 OPEN 으로 돌아가고 reopen_count + audit_log 로 남는다. '
          '기존 status 컬럼은 하위 호환을 위해 동기화된 채 유지된다.';
        COMMENT ON COLUMN monthly_settlement.validation_result_json IS
          '마감 전 검증 결과. [{code, severity, count, message, blocking}]. '
          'blocking 이 하나라도 있으면 CONFIRMED 로 넘어갈 수 없다.';
        COMMENT ON COLUMN monthly_settlement.acknowledged_warnings_json IS
          '사용자가 확인하고 진행하기로 한 warning 코드 목록. blocking 은 여기 넣어도 통과하지 않는다.';

        CREATE INDEX IF NOT EXISTS ix_monthly_settlement_close_state
          ON monthly_settlement (org_id, close_state, year_month);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_monthly_settlement_close_state;
        ALTER TABLE monthly_settlement
          DROP CONSTRAINT IF EXISTS ck_monthly_settlement_close_state;
        ALTER TABLE monthly_settlement
          DROP COLUMN IF EXISTS close_state,
          DROP COLUMN IF EXISTS review_requested_at,
          DROP COLUMN IF EXISTS review_requested_by,
          DROP COLUMN IF EXISTS validation_result_json,
          DROP COLUMN IF EXISTS validated_at,
          DROP COLUMN IF EXISTS acknowledged_warnings_json;
        """
    )
