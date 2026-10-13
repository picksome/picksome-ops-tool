"""M022 — 판매처별 가격: product_channel_listing + product_channel_price.

`docs/46_sale_price_schema_design.md` 의 모델 C 를 구현한다.

왜 product 에 단일 컬럼이 아닌가
--------------------------------
같은 상품이 쿠팡과 스마트스토어에서 다른 가격이다. 단일 컬럼은 둘 중 하나를 지운다.
게다가 한 판매처에 판매자 계정이 여럿일 수 있어서, 붙는 대상은 **마켓 문자열이 아니라
`integration_account`** 다.

listing 과 price 를 나눈 이유
-----------------------------
게시 상태(살아 있는 리스팅)와 가격 이력은 수명이 다르다. 한 테이블에 두면
가격을 한 번 바꿀 때마다 리스팅 행이 늘어나고, "지금 이 상품이 어디에 걸려 있나" 를
세는 것조차 어려워진다.

미입력은 0 이 아니라 NULL
------------------------
0 원은 "공짜로 판다" 는 뜻이고 NULL 은 "아직 안 정했다" 는 뜻이다. 둘을 같게 두면
마진 계산이 판매가 0 원으로 돌아가 손실이 원가 전액으로 잡힌다.
그래서 가격 컬럼은 **NULL 허용 + 값이 있으면 0 보다 커야 한다.**

기간 중복은 서비스가 아니라 DB 가 막는다
---------------------------------------
서비스 검증만으로는 동시 요청 두 개가 각자 "겹치는 게 없다" 를 확인하고 둘 다 넣는다.
`daterange` + exclusion constraint 로 DB 가 보장한다.

`product_variant_id IS NULL`(상품 기본 가격)도 충돌 검사에 들어가야 하는데,
SQL 에서 `NULL = NULL` 은 참이 아니라 exclusion 이 그냥 통과시킨다. 그래서
`variant_scope` **생성 컬럼**에 NULL 을 고정 UUID 로 접어 넣어 비교한다.
"""

from alembic import op

revision = "M022"
down_revision = "M021"
branch_labels = None
depends_on = None

# variant_id 가 NULL(상품 기본 가격)일 때 쓰는 자리표시자.
# 실제 variant UUID 와 겹칠 수 없는 nil UUID 를 쓴다.
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    # gist 인덱스에서 uuid/text 를 `=` 로 비교하려면 필요하다.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.execute("""
        CREATE TABLE product_channel_listing (
          id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id               UUID NOT NULL REFERENCES organization(id),

          -- 마켓 문자열이 아니라 **판매자 계정**에 붙는다.
          -- 같은 쿠팡이라도 스토어가 둘이면 상품번호도 가격도 따로다.
          integration_account_id UUID NOT NULL REFERENCES integration_account(id),
          product_id           UUID NOT NULL REFERENCES product(id),

          -- 판매처가 발급한 상품번호. 등록 전에는 NULL 일 수 있다.
          external_product_id  TEXT,
          external_product_url TEXT,

          status               TEXT NOT NULL DEFAULT 'DRAFT',

          -- 승인된 가격을 판매처에 자동 전송할지. **기본은 수동**이다.
          -- 자동 전송을 기본으로 두면 승인 실수가 곧바로 외부에 나간다.
          auto_publish_price   BOOLEAN NOT NULL DEFAULT FALSE,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID,
          deleted_at TIMESTAMPTZ,
          deleted_by UUID,

          CONSTRAINT ck_pcl_status CHECK (
            status IN ('DRAFT','ACTIVE','PAUSED','ENDED')
          )
        )
    """)

    # 같은 계정에서 같은 외부 상품번호가 두 번 나올 수 없다.
    # 부분 인덱스 — external_product_id 가 NULL 인 등록 전 리스팅은 여럿 허용한다.
    op.execute("""
        CREATE UNIQUE INDEX ux_pcl_account_external
            ON product_channel_listing (org_id, integration_account_id, external_product_id)
         WHERE external_product_id IS NOT NULL AND deleted_at IS NULL
    """)

    # 내부 상품 하나가 같은 계정에 두 번 걸리지 않는다.
    # (같은 스토어에 같은 상품을 두 번 올리는 건 중복 등록이고, 가격이 갈라진다.)
    op.execute("""
        CREATE UNIQUE INDEX ux_pcl_account_product
            ON product_channel_listing (org_id, integration_account_id, product_id)
         WHERE deleted_at IS NULL
    """)
    op.execute("CREATE INDEX ix_pcl_product ON product_channel_listing (org_id, product_id)")

    op.execute(f"""
        CREATE TABLE product_channel_price (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id       UUID NOT NULL REFERENCES organization(id),
          listing_id   UUID NOT NULL REFERENCES product_channel_listing(id),

          -- NULL = 상품 기본 가격. 값이 있으면 그 variant 의 **완전 판매가**다
          -- (기본가 + 추가금이 아니라 그 자체가 판매가).
          product_variant_id UUID REFERENCES product_variant(id),

          -- exclusion constraint 전용. NULL 을 nil UUID 로 접어 비교 가능하게 만든다.
          -- SQL 에서 NULL = NULL 은 참이 아니라, 이게 없으면 기본 가격끼리는
          -- 기간이 겹쳐도 통과한다.
          variant_scope UUID NOT NULL
            GENERATED ALWAYS AS (COALESCE(product_variant_id, '{_NIL_UUID}'::uuid)) STORED,

          currency     TEXT NOT NULL DEFAULT 'KRW',

          -- 금액은 정수. 통화의 최소 단위(KRW = 원)로 저장한다. float 금지.
          -- NULL = 미입력. 0 이 아니다.
          regular_price BIGINT,
          sale_price    BIGINT,
          sale_start_at TIMESTAMPTZ,
          sale_end_at   TIMESTAMPTZ,

          effective_from DATE NOT NULL,
          effective_to   DATE,

          status       TEXT NOT NULL DEFAULT 'ACTIVE',

          -- 이 가격 버전을 만든 승인 요청. 직접 입력이면 NULL.
          approval_request_id UUID REFERENCES approval_request(id),

          -- 외부 판매처 반영 상태. **내부 가격 확정과 분리한다** —
          -- 전송 실패가 승인 결과를 되돌리면 안 된다.
          publish_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
          publish_requested_at TIMESTAMPTZ,
          published_at   TIMESTAMPTZ,
          publish_error  TEXT,
          publish_attempt_count INTEGER NOT NULL DEFAULT 0,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID,
          deleted_at TIMESTAMPTZ,
          deleted_by UUID,

          -- 미입력은 NULL. 값이 있으면 0 보다 커야 한다.
          -- 0 원 판매가는 마진 계산에서 손실을 원가 전액으로 만든다.
          CONSTRAINT ck_pcp_regular_positive CHECK (
            regular_price IS NULL OR regular_price > 0
          ),
          CONSTRAINT ck_pcp_sale_positive CHECK (
            sale_price IS NULL OR sale_price > 0
          ),

          -- 할인가가 정상가보다 비쌀 수 없다. 입력 실수를 그대로 두면
          -- "할인" 이라는 이름으로 더 비싼 값이 마진 계산에 들어간다.
          CONSTRAINT ck_pcp_sale_not_above_regular CHECK (
            sale_price IS NULL OR regular_price IS NULL OR sale_price <= regular_price
          ),

          -- 할인 기간 쌍 검증. 할인가가 없으면 기간도 없어야 하고,
          -- 있으면 시작일이 반드시 있어야 한다(언제부터인지 모르는 할인은 관리 불가).
          CONSTRAINT ck_pcp_sale_window CHECK (
            (sale_price IS NULL AND sale_start_at IS NULL AND sale_end_at IS NULL)
            OR (sale_price IS NOT NULL AND sale_start_at IS NOT NULL)
          ),
          CONSTRAINT ck_pcp_sale_range CHECK (
            sale_end_at IS NULL OR sale_start_at IS NULL OR sale_end_at > sale_start_at
          ),

          CONSTRAINT ck_pcp_effective_range CHECK (
            effective_to IS NULL OR effective_to > effective_from
          ),

          CONSTRAINT ck_pcp_status CHECK (
            status IN ('ACTIVE','SUPERSEDED','CANCELLED')
          ),
          CONSTRAINT ck_pcp_publish_status CHECK (
            publish_status IN ('NOT_REQUESTED','QUEUED','PUBLISHING',
                               'PUBLISHED','FAILED','VERIFICATION_FAILED')
          ),
          CONSTRAINT ck_pcp_currency CHECK (currency ~ '^[A-Z]{{3}}$')
        )
    """)

    # ── 기간 중복 방지 ──
    #
    # 같은 listing + 같은 variant 범위의 유효기간이 겹치면 "지금 가격" 이 둘이 된다.
    # 서비스 검증만으로는 동시 요청 두 개가 각자 통과해 둘 다 들어간다.
    #
    # SUPERSEDED/CANCELLED 는 과거 기록이라 검사 대상이 아니다.
    op.execute("""
        ALTER TABLE product_channel_price
          ADD CONSTRAINT ex_pcp_no_overlap
          EXCLUDE USING gist (
            listing_id WITH =,
            variant_scope WITH =,
            daterange(effective_from, effective_to, '[)') WITH &&
          )
          WHERE (status = 'ACTIVE' AND deleted_at IS NULL)
    """)

    op.execute("""
        CREATE INDEX ix_pcp_listing_effective
            ON product_channel_price (org_id, listing_id, effective_from DESC)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_pcp_publish_queue
            ON product_channel_price (publish_status, publish_requested_at)
         WHERE publish_status IN ('QUEUED','FAILED') AND deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_pcp_approval
            ON product_channel_price (approval_request_id)
         WHERE approval_request_id IS NOT NULL
    """)

    # updated_at 자동 갱신 — M021 에서 clock_timestamp() 로 바뀐 그 함수를 공유한다.
    for tbl in ("product_channel_listing", "product_channel_price"):
        op.execute(f"""
            CREATE TRIGGER trg_{tbl}_updated_at
              BEFORE UPDATE ON {tbl}
              FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """)


def downgrade() -> None:
    # 가격 이력은 되살릴 수 없다. downgrade 는 **데이터 손실**이다 —
    # 운영에서 되돌릴 일이 생기면 먼저 export 해야 한다.
    op.execute("DROP TABLE IF EXISTS product_channel_price")
    op.execute("DROP TABLE IF EXISTS product_channel_listing")
    # btree_gist 는 남긴다. 다른 곳에서 쓸 수 있고, 지워서 얻는 것이 없다.
