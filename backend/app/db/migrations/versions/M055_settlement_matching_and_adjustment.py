"""M055 — 정산 레코드의 매칭 상태·원본 식별자, 그리고 정산 조정.

docs/47 선행조건 1·2·4 를 만든다. 3(실제 정산서 파일)·5(승인 어댑터)는
이 파일 밖이다 — 3 은 사용자 조치를 기다리고, 5 는 코드다.

왜 지금인가 — 파서가 없어도 막힌 곳이 여기다
----------------------------------------
정산서 원본을 아직 한 건도 못 받았다(`marketplace_settlement_record` 0건).
그래서 **읽는 쪽**(컬럼명·주문번호 형식)은 추측할 수 없고 만들지 않았다.

그런데 **장부 쪽**은 추측이 아니다. 어느 주문에 붙일 수 있는가, 붙일 수 없는
이유가 무엇인가, 확정한 정산을 어떻게 정정하는가 — 전부 우리 데이터로
판정된다. 실제 `sales_order` 378건으로 확인했다.

```
라인이 1개인 주문번호   362건   (marketplace, order_no) 로 exact 1:1
라인이 2개 이상          7건    어느 라인의 정산인지 알 수 없다
```

마지막 줄이 이 마이그레이션의 이유다.

`matched_sales_order_id IS NULL` 한 칸에 네 가지가 들어 있다
--------------------------------------------------------
```
아직 판정을 안 돌렸다
돌렸는데 후보가 0건이다
후보가 2건 이상이라 고를 수 없다
주문과 무관한 정산행이다 (광고비 정산·보증금)
```

전부 같은 NULL 이다. 화면은 이 넷에 **서로 다른 작업**을 안내해야 한다 —
후보가 여럿인 것은 사람이 고르면 끝나고, 0건인 것은 데이터가 없는 일이다.
같게 보이면 사용자는 둘 다 방치한다. M054 에서 같은 종류의 결함을 이미
겪었다(`UNRESOLVED` 한 칸에 정반대의 두 사실).

기존 UNIQUE 키는 덮어쓰기 사고를 낸다
---------------------------------
```
uq_msr_unique (org, marketplace, COALESCE(order_no,''), period_start)
```

- `order_no` 가 NULL 인 행(광고비 정산 등)은 **전부 같은 키**다. 같은 기간의
  주문 무관 정산행이 둘이면 두 번째가 들어가지 않는다
- 판매처가 정산서를 **재발행**하면 같은 키로 다시 온다

행을 식별하는 것은 기간이 아니라 **판매처가 준 그 행 자체**다. 그래서
`import_idempotency_key` 로 바꾼다 — 벤더 레코드 id 가 있으면 그것,
없으면 행 내용 지문. 지금 0건이므로 `NOT NULL` 로 시작할 수 있다.
**기본값을 주지 않는다** — 기본값이 있으면 식별자를 안 넣은 import 가
조용히 통과하고, 그게 정확히 막으려던 사고다.

만들지 않은 것과 그 이유
--------------------
```
expected_settlement_amount   기대 정산액을 무엇으로 계산할지는 실제
                             정산서가 말한다. 매칭된 주문의 판매금액은
                             이미 join 으로 나오므로 컬럼을 만들면
                             영원히 NULL 이거나 두 번째 진실이 된다
adjustment_record_id         재발행 파일이 새 버전 행을 만드는 경로는
                             실제 재발행 파일 없이는 "어느 쪽을 집계에
                             넣는가" 를 정할 수 없다. 안 쓰는 FK 를 두면
                             집계 규칙이 두 개가 된다
```

정산 조정은 순이익에 접지 않는다
---------------------------
`expense_adjustment` · `sales_adjustment` 와 **같은 상태**다(docs/80 §12-1).
셋 중 하나만 접으면 규칙이 갈라지고 파트너 배분 금액이 바뀐다. 그건 정산
모델 결정이라 마이그레이션이 대신 내릴 수 없다.
"""

from alembic import op

revision = "M055"
down_revision = "M054"
branch_labels = None
depends_on = None

# 매칭 상태. `EXCLUDED` 는 "대사 대상이 아니다" 지 "못 찾았다" 가 아니다 —
# 광고비 정산·보증금처럼 애초에 주문과 무관한 행이다.
_MATCHING_STATUS = (
    "'UNMATCHED', 'AMBIGUOUS', 'MATCHED_EXACT', 'MATCHED_MANUAL', 'EXCLUDED'"
)
_MATCHED = "'MATCHED_EXACT', 'MATCHED_MANUAL'"

# 조정 유형. `expense_adjustment` 와 같은 어휘를 쓴다 — 비용과 정산에서
# 같은 일을 다른 이름으로 부르면 리포트가 둘을 나란히 놓지 못한다.
_ADJ_TYPE = "'LATE_ENTRY', 'CORRECTION', 'REVERSAL', 'RECLASSIFICATION'"


def upgrade() -> None:
    # ── 0. 식별자를 지어낼 수 없는 행이 있으면 멈춘다 ────────────────
    #
    # `import_idempotency_key` 는 기본값 없는 NOT NULL 이다. 기존 행이 있으면
    # 이 ALTER 는 실패하는데, 그때 흔히 하는 대응(`DEFAULT ''` 를 주거나
    # `id::text` 로 backfill)이 **정확히 막으려던 사고를 만든다** — 지어낸
    # 식별자는 다음 import 가 같은 행을 새 행으로 넣게 한다.
    #
    # 그래서 먼저 세고, 있으면 사람이 읽을 수 있는 문장으로 멈춘다.
    # 개발·운영 모두 0건이라 실제로는 통과한다(확인 후 작성).
    op.execute("""
        DO $$
        DECLARE existing INT;
        BEGIN
            SELECT COUNT(*) INTO existing FROM marketplace_settlement_record;
            IF existing > 0 THEN
                RAISE EXCEPTION
                    '[M055_EXISTING_RECORDS] 정산 레코드가 % 건 있습니다. 원본 행 식별자(import_idempotency_key)를 '
                    '지어낼 수 없으므로 자동으로 채우지 않습니다. 원본 정산서에서 다시 '
                    'import 하거나, 각 행의 식별자를 정한 뒤 이 마이그레이션을 다시 '
                    '작성하세요.', existing;
            END IF;
        END $$
    """)

    # ── 1. 매칭 상태 ────────────────────────────────────────────────
    op.execute(f"""
        ALTER TABLE marketplace_settlement_record
          ADD COLUMN matching_status TEXT NOT NULL DEFAULT 'UNMATCHED',
          ADD COLUMN match_reason TEXT,
          ADD COLUMN matched_at TIMESTAMPTZ,
          ADD COLUMN matched_by UUID,
          -- 어느 승인이 이 연결을 허가했나. 비용·상품과 달리 정산 레코드에는
          -- 담을 자리가 있다 — 감사 로그를 뒤지지 않고 행에서 바로 답한다.
          ADD COLUMN match_approval_request_id UUID,

          ADD CONSTRAINT ck_msr_matching_status
            CHECK (matching_status IN ({_MATCHING_STATUS})),

          -- **상태와 연결이 어긋나지 못하게 한다.** 둘이 따로 놀면
          -- "MATCHED 인데 주문이 없는" 행이 리포트에서 매칭 완료로 세어진다.
          ADD CONSTRAINT ck_msr_matched_needs_order
            CHECK (matching_status NOT IN ({_MATCHED})
                   OR matched_sales_order_id IS NOT NULL),
          ADD CONSTRAINT ck_msr_unmatched_has_no_order
            CHECK (matching_status IN ({_MATCHED})
                   OR matched_sales_order_id IS NULL),

          -- 사람이 내린 판단에는 근거가 남아야 한다. 사유 없는 강제 연결은
          -- 나중에 아무도 설명하지 못하고, 그래서 아무도 되돌리지 못한다.
          ADD CONSTRAINT ck_msr_manual_needs_reason
            CHECK (matching_status NOT IN ('MATCHED_MANUAL', 'EXCLUDED')
                   OR (match_reason IS NOT NULL AND btrim(match_reason) <> ''))
    """)

    # ── 2. 정산 건 확정 ─────────────────────────────────────────────
    #
    # 월 마감과 **다른 축**이다(docs/47 §4). 쿠팡 주정산은 7/29~8/4 처럼 월
    # 경계를 넘고, 100건 중 80건만 확정된 상태로도 월 마감은 진행돼야 한다.
    op.execute(f"""
        ALTER TABLE marketplace_settlement_record
          ADD COLUMN confirmed_at TIMESTAMPTZ,
          ADD COLUMN confirmed_by UUID,

          -- 확정하려면 대사가 끝나 있어야 한다. "아직 안 봤다" 를 확정할 수
          -- 있으면 확정이라는 말이 아무 뜻도 갖지 않는다.
          ADD CONSTRAINT ck_msr_confirm_requires_resolution
            CHECK (confirmed_at IS NULL
                   OR matching_status IN ({_MATCHED}, 'EXCLUDED'))
    """)

    # ── 3. 원본 행 식별자 ───────────────────────────────────────────
    op.execute("""
        ALTER TABLE marketplace_settlement_record
          -- 판매처가 준 정산 레코드 id. 주는 곳도 있고 안 주는 곳도 있다.
          ADD COLUMN external_record_id TEXT,
          -- id 를 안 줄 때의 대체 식별자 — 행 내용 해시.
          ADD COLUMN source_row_fingerprint TEXT,
          -- 위 둘 중 실제로 쓴 것. import 가 반드시 채운다.
          ADD COLUMN import_idempotency_key TEXT NOT NULL,

          ADD CONSTRAINT ck_msr_idempotency_key_not_blank
            CHECK (btrim(import_idempotency_key) <> '')
    """)

    # 기간을 식별자로 쓰던 키를 버린다(위 docstring). 지금 0건이라
    # 데이터 이관이 없다.
    op.execute("DROP INDEX IF EXISTS uq_msr_unique")
    op.execute("""
        CREATE UNIQUE INDEX uq_msr_import_identity
            ON marketplace_settlement_record (org_id, marketplace, import_idempotency_key)
         WHERE deleted_at IS NULL
    """)

    # 매칭 판정이 매 행마다 도는 조회다. 판정 규칙이 쓰는 축 그대로 건다.
    op.execute("""
        CREATE INDEX ix_msr_match_lookup
            ON marketplace_settlement_record (org_id, marketplace, marketplace_order_no)
         WHERE deleted_at IS NULL AND marketplace_order_no IS NOT NULL
    """)
    # 화면·마감 검증이 "아직 안 붙은 것" 만 훑는다. 붙은 행이 대다수가 되면
    # 전체 스캔은 비싸진다.
    op.execute("""
        CREATE INDEX ix_msr_pending_match
            ON marketplace_settlement_record (org_id, matching_status)
         WHERE deleted_at IS NULL AND matching_status IN ('UNMATCHED', 'AMBIGUOUS')
    """)

    # ── 4. 정산 조정 ────────────────────────────────────────────────
    #
    # 확정된 정산 레코드를 UPDATE 하지 않는다. 확정 시점의 숫자로 순이익과
    # 배분이 이미 계산됐고, 원본을 고치면 그 근거가 사라진다(M026 과 같은 결론).
    #
    # 비용 조정과 다른 점: 정산 금액은 **항목이 여럿**이다. `amount_delta`
    # 하나로는 "수수료가 5만원 늘고 매출이 그대로" 를 표현할 수 없다.
    # JSONB 로 뭉치지 않고 원본과 **같은 컬럼 이름**을 쓴다 — 집계가 원본과
    # 조정을 UNION ALL 로 그대로 더할 수 있어야 한다.
    op.execute(f"""
        CREATE TABLE settlement_adjustment (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id                 UUID NOT NULL REFERENCES organization(id),

            original_record_id     UUID NOT NULL
                                   REFERENCES marketplace_settlement_record(id),

            adjustment_type        TEXT NOT NULL,
            -- 사유는 필수다. 사유 없는 조정은 나중에 아무도 설명 못 한다.
            adjustment_reason      TEXT NOT NULL,

            -- 원본이 속한 달과 이 조정이 실제로 반영되는 달. 다를 수 있다 —
            -- 7월 정산의 누락을 8월에 잡는 것이 정상적인 경우다.
            original_period        TEXT,
            adjustment_period      TEXT NOT NULL,

            -- 항목별 차액. **원본 금액이 아니라 차액이다** — 원본을 그대로
            -- 넣으면 집계가 두 배가 된다.
            gross_revenue_delta    BIGINT NOT NULL DEFAULT 0,
            marketplace_fee_delta  BIGINT NOT NULL DEFAULT 0,
            ad_cost_delta          BIGINT NOT NULL DEFAULT 0,
            return_cost_delta      BIGINT NOT NULL DEFAULT 0,
            fulfillment_fee_delta  BIGINT NOT NULL DEFAULT 0,
            other_deductions_delta BIGINT NOT NULL DEFAULT 0,
            net_settlement_delta   BIGINT NOT NULL,

            approval_request_id    UUID,

            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by             UUID,
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by             UUID,
            deleted_at             TIMESTAMPTZ,
            deleted_by             UUID,

            CONSTRAINT ck_settlement_adj_type
                CHECK (adjustment_type IN ({_ADJ_TYPE})),
            CONSTRAINT ck_settlement_adj_period
                CHECK (adjustment_period ~ '^[0-9]{{4}}-[0-9]{{2}}$'),
            CONSTRAINT ck_settlement_adj_original_period
                CHECK (original_period IS NULL
                       OR original_period ~ '^[0-9]{{4}}-[0-9]{{2}}$'),
            CONSTRAINT ck_settlement_adj_reason_not_blank
                CHECK (btrim(adjustment_reason) <> ''),
            -- 전부 0 인 조정은 조정이 아니다. 사유만 남기고 싶으면 메모를
            -- 쓸 자리가 따로 있어야지, 0원 조정 행이 리포트에 서면 안 된다.
            CONSTRAINT ck_settlement_adj_not_empty
                CHECK (gross_revenue_delta <> 0 OR marketplace_fee_delta <> 0
                       OR ad_cost_delta <> 0 OR return_cost_delta <> 0
                       OR fulfillment_fee_delta <> 0 OR other_deductions_delta <> 0
                       OR net_settlement_delta <> 0)
        )
    """)
    op.execute("""
        CREATE INDEX ix_settlement_adj_period
            ON settlement_adjustment (org_id, adjustment_period)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_settlement_adj_original
            ON settlement_adjustment (original_record_id)
         WHERE deleted_at IS NULL
    """)

    # ── 주석 ────────────────────────────────────────────────────────
    op.execute("""
        COMMENT ON COLUMN marketplace_settlement_record.matching_status IS
        'UNMATCHED=후보 없음/미판정, AMBIGUOUS=exact 후보 2건 이상(라인 분리 주문), MATCHED_EXACT=규칙으로 유일하게 찾음, MATCHED_MANUAL=승인을 거쳐 사람이 연결, EXCLUDED=주문과 무관한 정산행. 추측 매칭은 하지 않는다(docs/47 §2-2).'
    """)
    op.execute("""
        COMMENT ON COLUMN marketplace_settlement_record.import_idempotency_key IS
        '판매처 레코드 id 가 있으면 그것, 없으면 행 내용 지문. 기간이 아니라 행 자체가 식별자다 — 재발행 정산서가 같은 키로 덮어쓰는 사고를 막는다.'
    """)
    op.execute("""
        COMMENT ON TABLE settlement_adjustment IS
        '확정된 정산 레코드의 정정. 원본을 UPDATE 하지 않고 항목별 차액을 쌓는다(expense_adjustment 와 같은 방식). 순이익·배분에 접을지는 아직 정해지지 않은 재무 결정이다.'
    """)


def downgrade() -> None:
    # **되돌리기가 업무 데이터를 판단하지 않는다.** 조정이 남아 있으면
    # 실패시킨다 — 조용히 지우면 어느 달 정산이 왜 달라졌는지가 사라진다
    # (M053 downgrade 와 같은 규칙).
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM settlement_adjustment
             WHERE deleted_at IS NULL;
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '[M055_ADJUSTMENTS_REMAIN] 정산 조정이 % 건 남아 있습니다. 먼저 정리한 뒤 되돌리세요.',
                    remaining;
            END IF;
        END $$
    """)
    op.execute("DROP TABLE IF EXISTS settlement_adjustment")

    # 사람이 강제로 연결한 것을 조용히 풀지 않는다. 되돌리면 `matching_status`
    # 가 사라지므로 MATCHED_MANUAL 과 MATCHED_EXACT 를 구분할 수 없게 되고,
    # 다음 판정이 사람의 결정을 규칙으로 덮어쓴다.
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM marketplace_settlement_record
             WHERE deleted_at IS NULL
               AND matching_status IN ('MATCHED_MANUAL', 'EXCLUDED');
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '[M055_HUMAN_DECISIONS] 사람이 판단한 정산 매칭이 % 건 있습니다. 되돌리면 그 판단을 구분할 수 없게 됩니다.',
                    remaining;
            END IF;
        END $$
    """)

    op.execute("DROP INDEX IF EXISTS ix_msr_pending_match")
    op.execute("DROP INDEX IF EXISTS ix_msr_match_lookup")
    op.execute("DROP INDEX IF EXISTS uq_msr_import_identity")

    # 기간 기반 키를 되살린다. **원래 있던 결함까지 되살아난다** — 그게
    # 되돌리기의 의미다. 되살릴 수 없는 상태(같은 키 중복)면 여기서 터진다.
    op.execute("""
        CREATE UNIQUE INDEX uq_msr_unique
          ON marketplace_settlement_record(
            org_id, marketplace, COALESCE(marketplace_order_no, ''), settlement_period_start
          ) WHERE deleted_at IS NULL
    """)

    op.execute("""
        ALTER TABLE marketplace_settlement_record
          DROP CONSTRAINT IF EXISTS ck_msr_idempotency_key_not_blank,
          DROP CONSTRAINT IF EXISTS ck_msr_confirm_requires_resolution,
          DROP CONSTRAINT IF EXISTS ck_msr_manual_needs_reason,
          DROP CONSTRAINT IF EXISTS ck_msr_unmatched_has_no_order,
          DROP CONSTRAINT IF EXISTS ck_msr_matched_needs_order,
          DROP CONSTRAINT IF EXISTS ck_msr_matching_status,
          DROP COLUMN IF EXISTS import_idempotency_key,
          DROP COLUMN IF EXISTS source_row_fingerprint,
          DROP COLUMN IF EXISTS external_record_id,
          DROP COLUMN IF EXISTS confirmed_by,
          DROP COLUMN IF EXISTS confirmed_at,
          DROP COLUMN IF EXISTS match_approval_request_id,
          DROP COLUMN IF EXISTS matched_by,
          DROP COLUMN IF EXISTS matched_at,
          DROP COLUMN IF EXISTS match_reason,
          DROP COLUMN IF EXISTS matching_status
    """)
