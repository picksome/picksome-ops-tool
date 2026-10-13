"""M024 — 판매처 가격 반영 outbox.

왜 별도 테이블인가
-----------------
`integration_job` 은 **cron 스케줄 정의** 테이블이다 (`schedule_cron`, `is_enabled`).
"이 계정을 매일 3시에 동기화한다" 를 담는 곳이지 "이 가격 한 건을 보내야 한다" 를
담는 곳이 아니다. 재사용하면 두 개념이 한 테이블에 섞여 조회 조건이 뒤엉킨다.

왜 transactional outbox 인가
---------------------------
승인 적용 트랜잭션 안에서 외부 API 를 부르면 안 된다.

  · 외부 호출이 느리면 승인 트랜잭션이 그만큼 잠금을 쥐고 있다
  · 외부 호출이 실패하면 **승인이 통째로 롤백된다** — 승인은 유효한데
    판매처 전송만 실패한 것을 "승인 실패" 로 만들어 버린다
  · 커밋 후에 부르면 커밋과 호출 사이에 프로세스가 죽었을 때 전송이 유실된다

그래서 **같은 트랜잭션에서 outbox 행만 INSERT** 하고, 별도 runner 가 나중에 보낸다.
승인이 커밋되면 작업도 반드시 남고, 롤백되면 작업도 함께 사라진다.

lease 를 쓰는 이유
-----------------
`FOR UPDATE SKIP LOCKED` 만 쓰면 워커가 죽었을 때 그 행이 PROCESSING 으로 영원히 남는다.
`lease_expires_at` 이 지나면 다른 워커가 회수한다 — 사람이 개입할 필요가 없다.
"""

from alembic import op

revision = "M024"
down_revision = "M023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE channel_price_publish_outbox (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id       UUID NOT NULL REFERENCES organization(id),
          integration_account_id UUID NOT NULL REFERENCES integration_account(id),
          listing_id   UUID NOT NULL REFERENCES product_channel_listing(id),
          channel_price_id UUID NOT NULL REFERENCES product_channel_price(id),

          status       TEXT NOT NULL DEFAULT 'QUEUED',

          -- 같은 가격 버전을 두 번 보내지 않기 위한 키.
          -- 판매처가 멱등키를 지원하면 그대로 넘길 수도 있다.
          dedupe_key   TEXT NOT NULL,

          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TIMESTAMPTZ,

          -- lease: 워커가 잡은 시각과 만료 시각.
          -- 만료되면 프로세스가 죽은 것으로 보고 다른 워커가 회수한다.
          locked_at    TIMESTAMPTZ,
          lease_expires_at TIMESTAMPTZ,
          locked_by    TEXT,

          last_error_code TEXT,
          last_error_message TEXT,

          -- 판매처가 준 요청 식별자. 나중에 대사할 때 필요하다.
          external_request_id TEXT,
          -- 응답 메타. **자격증명이나 토큰을 넣지 않는다.**
          response_metadata_json JSONB,

          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by   UUID,
          started_at   TIMESTAMPTZ,
          finished_at  TIMESTAMPTZ,
          updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by   UUID,

          CONSTRAINT ck_cppo_status CHECK (
            status IN ('QUEUED','PROCESSING','PUBLISHED','RETRY_PENDING',
                       'FAILED','CONNECTOR_UNAVAILABLE','VERIFICATION_FAILED','CANCELLED')
          )
        )
    """)

    # 같은 가격 버전 × 같은 계정의 **활성** 작업은 하나만.
    # 종료된 작업(PUBLISHED/FAILED/CANCELLED)은 여러 건 남을 수 있다 — 재시도 이력이다.
    op.execute("""
        CREATE UNIQUE INDEX ux_cppo_active_dedupe
            ON channel_price_publish_outbox (org_id, integration_account_id, dedupe_key)
         WHERE status IN ('QUEUED','PROCESSING','RETRY_PENDING')
    """)

    # runner 가 집어갈 작업 조회용.
    op.execute("""
        CREATE INDEX ix_cppo_claimable
            ON channel_price_publish_outbox (status, next_attempt_at)
         WHERE status IN ('QUEUED','RETRY_PENDING')
    """)
    # lease 가 만료된 PROCESSING 회수용.
    op.execute("""
        CREATE INDEX ix_cppo_stale_lease
            ON channel_price_publish_outbox (lease_expires_at)
         WHERE status = 'PROCESSING'
    """)
    op.execute("""
        CREATE INDEX ix_cppo_price ON channel_price_publish_outbox (channel_price_id)
    """)

    op.execute("""
        CREATE TRIGGER trg_channel_price_publish_outbox_updated_at
          BEFORE UPDATE ON channel_price_publish_outbox
          FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS channel_price_publish_outbox")
