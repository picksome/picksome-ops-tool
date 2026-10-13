"""M037 — Sourcing Core: 공급처 · 상품 소싱처 · 원가 version · 환율 snapshot.

실제 Excel 을 읽고 정했다
----------------------
`마진계산기&장부.xlsx` 의 `판매 장부` 시트 "구매처" 열은 **이름이 아니라 URL** 이다.
358종이 들어 있고 도메인은 소수다.

```
https://domeggook.com/8618305?from=lstBiz
https://www.cheonyu.com/order/order_end_new.html
https://www.ownerclan.com/V2/service/orderList.php
https://www.tobizon.co.kr/mall/goods/goods_view.php?goodscd=DS01518184
```

즉 사용자는 이미 **사이트(도매꾹·천유·오너클랜·토비즈온)** 와 **그 안의 개별 상품
URL** 을 구분해서 쓰고 있었다. 그래서 둘을 나눈다.

```
supplier_site   사이트 (도메인 단위)
product_source  그 사이트의 특정 상품 (URL 단위)
```

`해외 직구 마진계산기` 시트는 landed cost 항목을 알려준다:
판매금액 / 환율 / 원화 결제액 / 배대지비용 / 관부가세 / 기타비용.
그 시트는 **관부가세를 하나로** 묶어 놨지만 여기서는 관세와 수입 부가세를
나눠 둔다 — 수입 부가세는 공제 대상일 수 있고 관세는 아니기 때문이다
(둘을 합치면 나중에 분리할 수 없다). 둘 다 nullable 이다.

무엇을 만들지 않았나
-----------------
**vendor 전용 컬럼을 product 에 넣지 않는다.** `product.next_engine_id` 같은
컬럼을 추가하기 시작하면 셀러툴을 하나 붙일 때마다 상품 테이블이 늘어난다.
외부 시스템 매핑은 별도 테이블(M038 예정)이 담당한다.

기존 `product.cost_amount` / `product.supply_url` 도 그대로 둔다.
자동 backfill 하지 않는다 — URL 만 보고 공급처를 만들면 근거 없는 법인이 생긴다.
"""

from alembic import op

revision = "M037"
down_revision = "M036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────
    # 1. 공급처 사이트
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE supplier_site (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),

          -- 사람이 이름을 바꿔도 관계가 깨지지 않는 고정 코드.
          -- Excel 행 번호를 코드로 쓰지 않는다 — 행이 밀리면 전부 어긋난다.
          supplier_code TEXT NOT NULL,
          name          TEXT NOT NULL,

          country_code  TEXT,
          supplier_type TEXT NOT NULL DEFAULT 'WHOLESALE',

          -- 도메인. 실제 Excel 의 "구매처" 가 URL 이라 여기로 정규화된다.
          website_url      TEXT,
          website_domain   TEXT,
          default_currency TEXT NOT NULL DEFAULT 'KRW',

          status TEXT NOT NULL DEFAULT 'ACTIVE',

          -- 사용자가 매긴 점수. 없으면 NULL — 0 은 "최악" 이라는 뜻이 된다.
          rating SMALLINT,
          memo   TEXT,

          supports_api       BOOLEAN NOT NULL DEFAULT FALSE,
          integration_status TEXT NOT NULL DEFAULT 'NONE',

          -- 비용 거래 상대방과 이어질 수 있다. **합치지 않는다** —
          -- 한 법인이 사이트를 여럿 운영할 수 있고, 사이트만 알고 법인을
          -- 모르는 경우가 더 흔하다. 모르면 NULL 이다.
          external_partner_id UUID REFERENCES external_partner(id),

          -- 로그인 계정이 있다는 사실만 남긴다. **비밀번호는 저장하지 않는다.**
          -- 실제 secret 은 vault(`password_vault_entry`)가 보관한다.
          credential_present   BOOLEAN NOT NULL DEFAULT FALSE,
          credentials_vault_entry_id UUID REFERENCES password_vault_entry(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_supplier_status
            CHECK (status IN ('ACTIVE', 'PAUSED', 'BLOCKED', 'ARCHIVED')),
          CONSTRAINT ck_supplier_type
            CHECK (supplier_type IN
                   ('WHOLESALE', 'MANUFACTURER', 'OVERSEAS_MARKETPLACE',
                    'RETAIL', 'AGENT', 'OTHER')),
          CONSTRAINT ck_supplier_integration
            CHECK (integration_status IN ('NONE', 'PLANNED', 'CONNECTED', 'FAILED')),
          CONSTRAINT ck_supplier_rating
            CHECK (rating IS NULL OR rating BETWEEN 1 AND 5)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_supplier_site_code
            ON supplier_site (org_id, supplier_code) WHERE deleted_at IS NULL
    """)
    # 이름 중복은 막지 않는다. 같은 이름의 다른 지점/계정이 실제로 있다
    # (실제 Excel 에 도매꾹 URL 이 여러 형태로 들어 있었다).
    op.execute("""
        CREATE INDEX ix_supplier_site_domain
            ON supplier_site (org_id, website_domain) WHERE deleted_at IS NULL
    """)

    # ──────────────────────────────────────────────────────────────
    # 2. 상품 ↔ 소싱처
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE product_source (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),

          product_id       UUID NOT NULL REFERENCES product(id),
          supplier_site_id UUID NOT NULL REFERENCES supplier_site(id),

          source_url TEXT,
          -- 공급처가 부르는 상품번호. 우리 상품 id 와 다르다.
          external_source_product_id TEXT,

          currency TEXT NOT NULL DEFAULT 'KRW',

          moq            INTEGER,
          lead_time_days INTEGER,

          -- 배송비 산정에 쓰인다. 모르면 NULL — 0 으로 두면 무게 없는 상품이 된다.
          weight_g   INTEGER,
          length_mm  INTEGER,
          width_mm   INTEGER,
          height_mm  INTEGER,

          is_primary BOOLEAN NOT NULL DEFAULT FALSE,
          status     TEXT NOT NULL DEFAULT 'ACTIVE',

          last_checked_at TIMESTAMPTZ,
          memo TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_product_source_status
            CHECK (status IN ('ACTIVE', 'PAUSED', 'DISCONTINUED')),
          CONSTRAINT ck_product_source_moq CHECK (moq IS NULL OR moq > 0),
          CONSTRAINT ck_product_source_lead
            CHECK (lead_time_days IS NULL OR lead_time_days >= 0)
        )
    """)

    # 한 상품의 **주 소싱처는 최대 하나**다.
    #
    # 애플리케이션에서만 막으면 동시 요청 두 개가 각각 primary 를 세우고 둘 다
    # 성공한다. 그 뒤로 예상 원가가 어느 쪽인지 알 수 없다.
    op.execute("""
        CREATE UNIQUE INDEX uq_product_single_primary_source
            ON product_source (product_id)
         WHERE is_primary AND status = 'ACTIVE' AND deleted_at IS NULL
    """)
    # 같은 상품을 같은 공급처에 두 번 연결할 이유가 없다.
    op.execute("""
        CREATE UNIQUE INDEX uq_product_source_pair
            ON product_source (product_id, supplier_site_id)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_product_source_supplier
            ON product_source (org_id, supplier_site_id) WHERE deleted_at IS NULL
    """)

    # ──────────────────────────────────────────────────────────────
    # 3. 소싱 원가 version
    # ──────────────────────────────────────────────────────────────
    #
    # 원가를 덮어쓰지 않는다. "지금 다시 사면 얼마인가" 는 시간에 따라 변하고,
    # 과거에 얼마였는지는 그 시점의 판단을 설명하는 근거다.
    #
    # ⚠️ 이것은 **SOURCE COST** 다 — 재고 취득원가(INVENTORY COST)도,
    #    판매에 귀속된 원가(ORDER COST)도 아니다. 셋을 합치면 되돌릴 수 없다.
    op.execute("""
        CREATE TABLE product_source_cost_version (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),
          product_source_id UUID NOT NULL REFERENCES product_source(id),

          -- 공급처가 부르는 값. 통화는 source 와 다를 수 있어 여기 다시 둔다.
          purchase_price BIGINT NOT NULL,
          currency       TEXT NOT NULL,

          -- 취득 부대비용. **실제 Excel 에 없는 항목을 NOT NULL 로 만들지 않는다.**
          domestic_shipping      BIGINT,
          international_shipping BIGINT,
          -- 관세와 수입 부가세를 나눈다. Excel 은 "관부가세" 로 합쳐 놨지만
          -- 수입 부가세는 공제 대상일 수 있고 관세는 아니다.
          customs_duty BIGINT,
          import_vat   BIGINT,
          packaging_cost BIGINT,
          other_cost     BIGINT,

          -- 반열린 구간 [valid_from, valid_to). 판매가 versioning 과 같은 규칙이다.
          valid_from DATE NOT NULL,
          valid_to   DATE,

          source_type TEXT NOT NULL DEFAULT 'MANUAL',
          observed_at TIMESTAMPTZ,
          memo TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_source_cost_price CHECK (purchase_price >= 0),
          CONSTRAINT ck_source_cost_period
            CHECK (valid_to IS NULL OR valid_to > valid_from),
          CONSTRAINT ck_source_cost_origin
            CHECK (source_type IN
                   ('MANUAL', 'EXCEL_IMPORT', 'CRAWLED', 'API', 'SELLER_TOOL'))
        )
    """)

    # 같은 소싱처에 기간이 겹치는 원가가 둘이면 "그날 얼마였나" 에 답할 수 없다.
    # 반열린 구간이라 `[)` 로 겹침을 판정한다.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("""
        ALTER TABLE product_source_cost_version
          ADD CONSTRAINT ex_source_cost_no_overlap
          EXCLUDE USING gist (
            product_source_id WITH =,
            daterange(valid_from, valid_to, '[)') WITH &&
          ) WHERE (deleted_at IS NULL)
    """)
    op.execute("""
        CREATE INDEX ix_source_cost_lookup
            ON product_source_cost_version (product_source_id, valid_from DESC)
         WHERE deleted_at IS NULL
    """)

    # ──────────────────────────────────────────────────────────────
    # 4. 환율 snapshot
    # ──────────────────────────────────────────────────────────────
    #
    # 같은 거래라도 시점마다 환율이 다르다:
    # 소싱 검토 / 가격 설정 / 실제 구매 / 대금 지급 / 회계 인식.
    # 하나의 "현재 환율" 로 뭉개면 어느 시점 기준인지 말할 수 없다.
    op.execute("""
        CREATE TABLE fx_rate_snapshot (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),

          base_currency  TEXT NOT NULL,
          quote_currency TEXT NOT NULL,
          -- 1 base = rate quote. 예: 1 CNY = 205 KRW
          rate NUMERIC(18, 6) NOT NULL,

          rate_type TEXT NOT NULL DEFAULT 'SOURCING',

          observed_at TIMESTAMPTZ NOT NULL,

          source_type TEXT NOT NULL DEFAULT 'MANUAL',
          source_name TEXT,
          external_reference TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_fx_rate_positive CHECK (rate > 0),
          CONSTRAINT ck_fx_not_same CHECK (base_currency <> quote_currency),
          CONSTRAINT ck_fx_rate_type
            CHECK (rate_type IN
                   ('SOURCING', 'PRICE_SETTING', 'PURCHASE', 'PAYMENT', 'ACCOUNTING')),
          CONSTRAINT ck_fx_source
            CHECK (source_type IN ('MANUAL', 'EXCEL_IMPORT', 'API'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_fx_lookup
            ON fx_rate_snapshot (org_id, base_currency, quote_currency,
                                 rate_type, observed_at DESC)
         WHERE deleted_at IS NULL
    """)

    op.execute("""
        COMMENT ON TABLE product_source_cost_version IS
        'SOURCE COST — 지금 다시 사면 얼마인가. 재고 취득원가·판매 귀속 원가와 다르다.'
    """)
    op.execute("""
        COMMENT ON TABLE fx_rate_snapshot IS
        '시점별 환율. rate_type 이 어느 국면의 환율인지 말한다. 하나의 현재 환율로 뭉개지 않는다.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fx_rate_snapshot")
    op.execute("DROP TABLE IF EXISTS product_source_cost_version")
    op.execute("DROP TABLE IF EXISTS product_source")
    op.execute("DROP TABLE IF EXISTS supplier_site")
    # btree_gist 는 남긴다 — 다른 마이그레이션이 쓸 수 있고, extension 을 지우는
    # 것은 이 마이그레이션의 소유 범위를 넘는다.
