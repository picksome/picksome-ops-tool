"""M053 — 판매 클레임과 판매 조정.

왜 지금 필요한가 — 실제 커넥터가 증명한 gap
--------------------------------------
NEXT ENGINE 클레임 조회로 **실제 취소/반품 10건**을 받아 봤다(docs/80).
이 사실을 담을 자리가 지금 스키마에 없다.

```
sales_order            "그때 3만원 주문이 발생했다"        있다
취소/반품 사실          "이후 그 주문이 취소됐다"           없다
그로 인한 매출 조정      "8월 매출에서 -3만원"              없다
```

`expense_adjustment` 가 비용 쪽에서 이미 같은 문제를 푼다 — 잠긴 달을 고치지
않고 열린 달에 조정을 만든다. 판매 쪽에는 그 짝이 없다.

원 주문을 덮어쓰지 않는다
--------------------
```
order.gross = 30,000  →  0 으로 수정      ✗ "그때 주문이 없었다" 가 된다
order.gross = 30,000 + 조정 -30,000       ○ 두 사실이 다 남는다
```

첫 번째는 과거를 지운다. 3만원 주문이 실제로 발생했고 그 뒤 취소됐다는 것은
**서로 다른 두 사실**이고, 둘 다 있어야 "왜 8월 매출이 줄었나" 에 답할 수 있다.

vendor 이름을 스키마에 넣지 않는다
---------------------------
```
nextengine_claim 테이블        ✗ 셀러툴마다 테이블이 하나씩 생긴다
claim.nextengine_status 컬럼   ✗ 마켓이 늘면 컬럼이 늘어난다
raw_json JSONB 통째 저장        ✗ 개인정보가 통째로 눌러앉는다
```

세 번째가 특히 위험하다. 클레임 응답에는 구매자명·수취인·주소·전화번호가
**전부 채워져서** 온다(실측 10/10). 편하다고 통째로 넣으면 그 순간부터
회수할 수 없다.

모르는 금액을 0 으로 채우지 않는다
--------------------------
금액 컬럼은 전부 **NULL 허용**이다. 실측에서 `취소완료` 인데 `cancel_price`
가 0 으로 왔다(cancel_cnt 는 1~2). 0원 환불이 아니라 **벤더가 안 채운 것**이다.
NOT NULL DEFAULT 0 으로 두면 그 구분이 스키마 단계에서 사라진다.
"""

from alembic import op

revision = "M053"
down_revision = "M052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 클레임 사실 ────────────────────────────────────────────────
    #
    # 외부에서 일어난 취소/반품/교환 **사실**이다. 우리가 처리하는 업무가
    # 아니다 — Seller OS 는 클레임을 승인하거나 회수를 지시하지 않는다.
    op.execute("""
        CREATE TABLE sales_claim (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id                UUID NOT NULL REFERENCES organization(id),

            -- 어느 연동에서 왔나. 같은 외부 계정 안에서 클레임 번호가 유일하다.
            external_account_id   UUID NOT NULL REFERENCES external_account(id),
            external_claim_id     TEXT NOT NULL,
            -- 외부가 말하는 원 주문. 우리 주문을 못 찾아도 이건 남긴다 —
            -- 나중에 주문이 들어오면 이 값으로 되살린다.
            external_order_id     TEXT,
            marketplace_order_no  TEXT,

            -- 우리 쪽 연결. **못 찾으면 NULL 이고 그대로 둔다.**
            -- 비슷한 주문에 붙이지 않는다.
            sales_order_id        UUID REFERENCES sales_order(id),
            integration_account_id UUID REFERENCES integration_account(id),

            claim_type            TEXT NOT NULL,
            claim_status          TEXT NOT NULL,
            -- 벤더가 준 원문 코드/문구. 우리 해석이 틀렸을 때 되짚을 근거다.
            vendor_status_code    TEXT,
            vendor_status_text    TEXT,

            -- 재무에 영향을 줄 수 있는 상태인가. 상태와 재무효과는 다르다 —
            -- 반품요청은 아직 환불이 아니다.
            financial_effect      TEXT NOT NULL,

            -- 수량·금액. **모르면 NULL.** 0 은 "0원" 이라는 확정된 값이다.
            claim_quantity        INT,
            claim_amount          BIGINT,
            shipping_amount       BIGINT,
            -- 금액을 어디서 얻었나. 파생값과 벤더값을 섞으면 나중에 구분 못 한다.
            amount_source         TEXT,

            requested_at          TIMESTAMPTZ,
            status_changed_at     TIMESTAMPTZ,
            claim_registered_at   TIMESTAMPTZ,
            -- 원 주문의 판매일. 조정을 어느 달에 달지 정할 때 쓴다.
            original_sale_date    DATE,

            first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by            UUID,
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by            UUID,
            deleted_at            TIMESTAMPTZ,
            deleted_by            UUID,

            CONSTRAINT ck_sales_claim_type
                CHECK (claim_type IN ('CANCEL', 'RETURN', 'EXCHANGE', 'UNKNOWN')),
            CONSTRAINT ck_sales_claim_status
                CHECK (claim_status IN (
                    'REQUESTED', 'IN_PROGRESS', 'COMPLETED',
                    'WITHDRAWN', 'UNKNOWN')),
            CONSTRAINT ck_sales_claim_financial_effect
                CHECK (financial_effect IN (
                    'NONE', 'PENDING', 'EFFECTIVE', 'REVERSED', 'UNRESOLVED')),
            CONSTRAINT ck_sales_claim_amount_source
                CHECK (amount_source IS NULL OR amount_source IN (
                    'VENDOR_CLAIM', 'DERIVED_FROM_ORIGINAL_ORDER')),
            -- 금액이 있으면 출처가 반드시 있어야 한다. 출처 없는 금액은
            -- 나중에 누구도 검증할 수 없다.
            CONSTRAINT ck_sales_claim_amount_needs_source
                CHECK (claim_amount IS NULL OR amount_source IS NOT NULL),
            CONSTRAINT ck_sales_claim_quantity
                CHECK (claim_quantity IS NULL OR claim_quantity >= 0)
        )
    """)

    # 같은 외부 계정 안에서 클레임 번호는 하나다. **이게 멱등성의 근거다** —
    # 같은 클레임을 100번 읽어도 행은 하나여야 한다.
    op.execute("""
        CREATE UNIQUE INDEX uq_sales_claim_external_identity
            ON sales_claim (external_account_id, external_claim_id)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_sales_claim_order
            ON sales_claim (org_id, sales_order_id)
         WHERE deleted_at IS NULL
    """)
    # 원 주문을 아직 못 찾은 클레임을 되살릴 때 쓰는 경로.
    op.execute("""
        CREATE INDEX ix_sales_claim_unlinked
            ON sales_claim (org_id, external_order_id)
         WHERE deleted_at IS NULL AND sales_order_id IS NULL
    """)

    # ── 판매 조정 ──────────────────────────────────────────────────
    #
    # `expense_adjustment` 의 판매 쪽 짝이다. 같은 어휘를 쓴다 —
    # original_period / adjustment_period / amount_delta.
    op.execute("""
        CREATE TABLE sales_adjustment (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id              UUID NOT NULL REFERENCES organization(id),

            -- 무엇 때문에 생긴 조정인가.
            sales_claim_id      UUID REFERENCES sales_claim(id),
            sales_order_id      UUID REFERENCES sales_order(id),

            adjustment_type     TEXT NOT NULL,
            adjustment_reason   TEXT NOT NULL,

            -- 원 주문이 속한 달과, 이 조정이 실제로 반영되는 달.
            -- 둘이 다를 수 있다 — 7월 주문의 8월 반품.
            original_period     TEXT,
            adjustment_period   TEXT NOT NULL,

            -- 매출 조정액. 취소면 음수다.
            amount_delta        BIGINT NOT NULL,
            quantity_delta      INT,
            amount_source       TEXT NOT NULL,

            -- 되돌림이면 무엇을 되돌리는가. 원본을 지우지 않고 상쇄한다.
            reverses_adjustment_id UUID REFERENCES sales_adjustment(id),

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by          UUID,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by          UUID,
            deleted_at          TIMESTAMPTZ,
            deleted_by          UUID,

            CONSTRAINT ck_sales_adj_type
                CHECK (adjustment_type IN ('CLAIM_REVENUE', 'CLAIM_REVERSAL')),
            CONSTRAINT ck_sales_adj_source
                CHECK (amount_source IN (
                    'VENDOR_CLAIM', 'DERIVED_FROM_ORIGINAL_ORDER')),
            CONSTRAINT ck_sales_adj_period
                CHECK (adjustment_period ~ '^[0-9]{4}-[0-9]{2}$'),
            CONSTRAINT ck_sales_adj_original_period
                CHECK (original_period IS NULL
                       OR original_period ~ '^[0-9]{4}-[0-9]{2}$'),
            -- 되돌림은 무엇을 되돌리는지 반드시 가리킨다.
            CONSTRAINT ck_sales_adj_reversal_target
                CHECK (adjustment_type <> 'CLAIM_REVERSAL'
                       OR reverses_adjustment_id IS NOT NULL)
        )
    """)

    # **재무 조정 멱등성.** 한 클레임의 한 전이에 조정은 하나다.
    # 같은 클레임을 다시 읽어도 조정이 또 생기면 매출이 두 번 깎인다.
    op.execute("""
        CREATE UNIQUE INDEX uq_sales_adj_claim_type
            ON sales_adjustment (sales_claim_id, adjustment_type)
         WHERE deleted_at IS NULL AND sales_claim_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_sales_adj_period
            ON sales_adjustment (org_id, adjustment_period)
         WHERE deleted_at IS NULL
    """)

    op.execute("""
        COMMENT ON TABLE sales_claim IS
        '외부에서 발생한 취소/반품/교환 사실. Seller OS 는 클레임을 처리하지 않고 읽기만 한다. 개인정보는 저장하지 않는다.'
    """)
    op.execute("""
        COMMENT ON TABLE sales_adjustment IS
        '클레임으로 인한 매출 조정. 원 주문을 수정하지 않고 조정 행을 쌓는다(expense_adjustment 와 같은 방식).'
    """)
    op.execute("""
        COMMENT ON COLUMN sales_claim.claim_amount IS
        'NULL = 모름. 0 = 확정된 0원. 벤더가 금액을 안 채우는 경우가 실측으로 확인됐다.'
    """)


def downgrade() -> None:
    # **되돌리기가 업무 데이터를 판단하지 않는다.** 조정이 남아 있으면
    # 실패시키고 사람이 먼저 정리하게 한다 — 조용히 지우면 어느 달 매출이
    # 왜 줄었는지가 사라지고, 그 사실은 다음 정산 때까지 아무도 모른다.
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM sales_adjustment
             WHERE deleted_at IS NULL;
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '판매 조정이 % 건 남아 있습니다. 먼저 정리한 뒤 되돌리세요.',
                    remaining;
            END IF;
        END $$
    """)
    op.execute("DROP TABLE IF EXISTS sales_adjustment")
    op.execute("DROP TABLE IF EXISTS sales_claim")
