"""M038 — 외부 시스템 · 외부 계정 (셀러툴 등).

왜 `integration_account` 를 넓히지 않았나
------------------------------------
기존 `integration_account` 를 읽어 보면 **판매처 계정**이다.

```
sale_channel_id          어느 판매채널의 계정인가
auth_kind API_KEY        우리가 발급한 키로 봇이 **우리에게** 들어온다 (inbound)
```

그리고 여섯 테이블이 FK 로 참조한다:

```
sales_order.integration_account_id                 이 주문이 어느 스토어 것인가
marketplace_settlement_record.integration_account_id
product_channel_listing / channel_price_publish_outbox
integration_job / external_product_mapping
```

여기에 NEXT ENGINE 계정을 섞으면 **주문이 셀러툴 계정에 귀속될 수 있게 된다.**
`sale_channel_id` 도 셀러툴에는 의미가 없다. 방향도 반대다 — 셀러툴은 우리가
나가서 호출한다(outbound).

그래서 판매처 계정은 그대로 두고 **외부 시스템 계정을 따로 만든다.**
상위 generic 계정으로 통합하는 안(Option C)은 위 여섯 FK 를 전부 옮겨야 해서
지금 얻는 것 없이 회귀 위험만 크다.

enum 대신 master table
--------------------
`external_system` 을 enum 으로 두면 셀러툴 하나 추가할 때마다 마이그레이션이
필요하다. 조직마다 쓰는 도구가 다른 SaaS 에서 그건 배포 없이는 아무것도 못
붙인다는 뜻이다. 그래서 행으로 관리한다.

비밀은 저장하지 않는다
------------------
`access_key` / `secret` / `password` 컬럼을 만들지 않는다. 기존 vault
(`password_vault_entry`)를 참조만 한다 — 보관 위치를 가리킬 뿐이다.
"""

from alembic import op

revision = "M038"
down_revision = "M037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────
    # 1. 외부 시스템 (전역 카탈로그)
    # ──────────────────────────────────────────────────────────────
    #
    # org_id 가 없다. "NEXT ENGINE 이라는 서비스가 존재한다" 는 사실은 조직마다
    # 다르지 않다. 조직별인 것은 그 서비스의 **계정**이다(아래 external_account).
    op.execute("""
        CREATE TABLE external_system (
          id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),

          -- 안정 식별자. 표시명이 바뀌어도 코드는 그대로다.
          -- 애플리케이션이 이 값으로 connector 를 찾는다.
          code TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,

          system_type TEXT NOT NULL,

          -- 인증 방식. 계정 등록 화면이 무엇을 물어볼지 결정한다.
          supports_oauth   BOOLEAN NOT NULL DEFAULT FALSE,
          supports_api_key BOOLEAN NOT NULL DEFAULT FALSE,
          supports_webhook BOOLEAN NOT NULL DEFAULT FALSE,

          documentation_url TEXT,
          is_active BOOLEAN NOT NULL DEFAULT TRUE,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          CONSTRAINT ck_external_system_type
            CHECK (system_type IN ('SELLER_TOOL', 'SUPPLIER_PLATFORM', 'OTHER'))
        )
    """)
    op.execute("""
        COMMENT ON TABLE external_system IS
        '외부 시스템 카탈로그. 판매처 계정(integration_account)과 다르다 — 여기는 셀러툴 등 outbound 대상이다.'
    """)

    # ──────────────────────────────────────────────────────────────
    # 2. 외부 계정 (조직별)
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE external_account (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          external_system_id UUID NOT NULL REFERENCES external_system(id),

          -- 한 조직이 같은 셀러툴 계정을 여럿 쓸 수 있다(법인 분리 등).
          account_code TEXT NOT NULL,
          display_name TEXT NOT NULL,

          -- 외부가 부르는 계정 식별자(회사코드 등). 비밀이 아니다.
          external_account_identifier TEXT,

          status TEXT NOT NULL DEFAULT 'DISCONNECTED',

          -- **비밀 원문을 여기 두지 않는다.** vault 항목을 가리킬 뿐이다.
          -- 이 컬럼이 NULL 이면 "자격증명 미설정" 이고, 그 사실만 API 로 나간다.
          credentials_vault_entry_id UUID REFERENCES password_vault_entry(id),

          -- 계정 단위로 기능을 끄고 싶을 때. NULL 이면 시스템 기본값을 따른다.
          -- (기본값은 connector 가 코드로 선언한다 — DB 가 진실이 아니다.)
          capability_override_json JSONB,

          last_connected_at       TIMESTAMPTZ,
          last_successful_sync_at TIMESTAMPTZ,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_external_account_status
            CHECK (status IN ('DISCONNECTED', 'CONNECTED', 'ERROR', 'DISABLED'))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_external_account_code
            ON external_account (org_id, external_system_id, account_code)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_external_account_org
            ON external_account (org_id, external_system_id) WHERE deleted_at IS NULL
    """)
    op.execute("""
        COMMENT ON COLUMN external_account.credentials_vault_entry_id IS
        '자격증명 보관 위치. 비밀 원문은 어떤 컬럼에도 저장하지 않는다.'
    """)

    # ──────────────────────────────────────────────────────────────
    # 3. 알려진 시스템 seed
    # ──────────────────────────────────────────────────────────────
    #
    # 코드에 if/elif 를 퍼뜨리지 않기 위한 카탈로그 행이다.
    # **connector 가 구현돼 있다는 뜻이 아니다** — 구현 여부는 registry 가 안다.
    op.execute("""
        INSERT INTO external_system (code, name, system_type, supports_api_key, supports_oauth)
        VALUES
          ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE, TRUE),
          ('WINDLY', '윈들리', 'SELLER_TOOL', TRUE, FALSE)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS external_account")
    op.execute("DROP TABLE IF EXISTS external_system")
