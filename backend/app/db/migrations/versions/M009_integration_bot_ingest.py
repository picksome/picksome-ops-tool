"""M009 투트랙 연동(API/봇) — integration_account 확장 + 봇 ingest 인증.

배경 (docs/27_dynamic_channel_and_bot_integration_plan.md 3-3, D-289):
  integration_account/integration_job/batch_run/batch_run_log/marketplace_order_import
  등은 M001 부터 골격만 있었다(Post-MVP). 이제 두 트랙을 실제로 붙인다:
    - 트랙 A(API pull): integration_job 이 cron 으로 마켓 API 호출
    - 트랙 B(봇 push):  봇/MCP 툴이 API Key 로 POST /integrations/{id}/ingest/orders 호출
  두 트랙 모두 같은 정규화 경로(services/integration_ingest.py -> services/sales.create_sale)로 수렴.

조치:
  integration_account:
    + sale_channel_id UUID NULL FK  — 이 연동이 어느 판매채널(M007) 것인지
    + auth_kind TEXT NOT NULL DEFAULT 'API_KEY' CHECK IN (API_KEY, OAUTH, BOT_TOKEN) — D-289: MVP 는 API_KEY 만 실제 지원
    + config_json JSONB NULL        — 마켓별 커스텀 설정(엔드포인트 등, D-290 범위 제한)
    + api_key_hash TEXT NULL        — 봇 ingest 인증용. argon2 해시(core/security.py 재사용, 평문 미저장)
    credentials_vault_entry_id 를 NULLABLE 로 변경 — BOT/API_KEY 계정은 3자 로그인 정보가
    없어 vault 참조가 필요 없음(우리가 발급한 키의 해시만 저장, CLAUDE.md Security 원칙 그대로 적용).

  integration_job:
    + trigger_kind TEXT NOT NULL DEFAULT 'SCHEDULE' CHECK IN (SCHEDULE, WEBHOOK)

  marketplace_order_import / marketplace_settlement_import:
    + source_type TEXT NOT NULL DEFAULT 'MANUAL' CHECK IN (API_AUTO, BOT_AUTO, MANUAL) — D-291,
      나중에 배분 오차 추적 시 값이 어디서 왔는지 구분 필요.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M009"
down_revision: str | None = "M008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE integration_account
          ALTER COLUMN credentials_vault_entry_id DROP NOT NULL,
          ADD COLUMN sale_channel_id UUID NULL REFERENCES sale_channel(id),
          ADD COLUMN auth_kind TEXT NOT NULL DEFAULT 'API_KEY',
          ADD COLUMN config_json JSONB NULL,
          ADD COLUMN api_key_hash TEXT NULL,
          ADD CONSTRAINT ck_integration_account_auth_kind
            CHECK (auth_kind IN ('API_KEY', 'OAUTH', 'BOT_TOKEN'));

        ALTER TABLE integration_job
          ADD COLUMN trigger_kind TEXT NOT NULL DEFAULT 'SCHEDULE',
          ADD CONSTRAINT ck_integration_job_trigger_kind
            CHECK (trigger_kind IN ('SCHEDULE', 'WEBHOOK'));

        ALTER TABLE marketplace_order_import
          ADD COLUMN source_type TEXT NOT NULL DEFAULT 'MANUAL',
          ADD CONSTRAINT ck_moi_source_type
            CHECK (source_type IN ('API_AUTO', 'BOT_AUTO', 'MANUAL'));

        ALTER TABLE marketplace_settlement_import
          ADD COLUMN source_type TEXT NOT NULL DEFAULT 'MANUAL',
          ADD CONSTRAINT ck_msi_source_type
            CHECK (source_type IN ('API_AUTO', 'BOT_AUTO', 'MANUAL'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE marketplace_settlement_import
          DROP CONSTRAINT ck_msi_source_type,
          DROP COLUMN source_type;

        ALTER TABLE marketplace_order_import
          DROP CONSTRAINT ck_moi_source_type,
          DROP COLUMN source_type;

        ALTER TABLE integration_job
          DROP CONSTRAINT ck_integration_job_trigger_kind,
          DROP COLUMN trigger_kind;

        ALTER TABLE integration_account
          DROP CONSTRAINT ck_integration_account_auth_kind,
          DROP COLUMN api_key_hash,
          DROP COLUMN config_json,
          DROP COLUMN auth_kind,
          DROP COLUMN sale_channel_id;
        """
    )
