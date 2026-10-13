"""M028 — 월 마감 검증 이력 + 확정 숫자 snapshot.

무엇이 문제였나
--------------
M017 이 `monthly_settlement.close_state` 로 OPEN→REVIEW→CONFIRMED→LOCKED 를 만들었고
검증 결과를 `validation_result_json` **한 칼럼에 덮어쓰기**로 저장했다. 그래서:

  · 검증을 다시 돌리면 이전 결과가 사라진다. "확정 당시 무엇을 알고 있었나" 를
    나중에 물어볼 수 없다
  · 확정 숫자가 어디에도 박제되지 않는다. `net_profit` 컬럼은 있지만 그 숫자를
    구성한 세부 항목(원가·수수료·광고비·비용)은 **매번 raw 테이블을 다시 집계**해야
    나온다. 확정 후 판매 한 건이 조용히 바뀌면 "확정 순이익" 이 달라진다

이 마이그레이션이 두 문제를 각각 해결한다.

1. `monthly_close_check_run` / `_check_item` — 검증 1회 = run 1행. 덮어쓰지 않는다
2. `monthly_close_snapshot`               — 확정 시점의 숫자를 박제. UPDATE 금지

왜 check_item 에 status/assignee 를 두지 않는가
--------------------------------------------
검증 run 은 **측정**이다. "14:32 에 미매칭 정산 3건이 보였다" 는 사실은 나중에
사람이 처리했든 안 했든 바뀌지 않는다. 그 항목을 누가 언제 처리하는지는
`operation_issue`(M030)가 다룬다 — 같은 상태를 두 테이블에 두면 반드시 어긋난다.

연결은 `dedupe_key` 로 한다. 이슈 스캐너가 최신 run 의 BLOCKING/WARNING 항목을
읽어 이슈를 만들거나 갱신한다.

snapshot 불변성을 DB 로 강제한다
-----------------------------
service 에만 두면 다음 사람이 UPDATE 를 한 줄 쓴다. 그래서 트리거로 막는다:

  · `superseded_at` / `superseded_by_id` **외의 컬럼 변경은 거부**
  · 즉 세대 교체(reopen 후 재확정)는 되지만 과거 숫자 정정은 불가능하다

`content_hash` 는 감사·회귀 탐지용이다. 확정 후 raw 데이터가 조용히 바뀌었는지
다시 집계해서 비교하면 알 수 있다. **암호학적 보안 목적이 아니다** —
같은 키로 서명하지 않으므로 위조 방지 기능은 없다.
"""

from alembic import op

revision = "M028"
down_revision = "M027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────
    # 1. 검증 run 이력
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE monthly_close_check_run (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id      UUID NOT NULL REFERENCES organization(id),
          year_month  TEXT NOT NULL,

          -- 검증을 돌린 이유. 확정 시도가 실패한 run 과 사용자가 눌러본 run 을
          -- 구분해야 "확정 직전에 무엇이 남아 있었나" 를 답할 수 있다.
          trigger     TEXT NOT NULL,

          -- 검증 당시 마감 상태. 나중에 상태가 바뀌어도 이 값은 그대로다.
          close_state_at_run TEXT NOT NULL,

          blocking_count INTEGER NOT NULL DEFAULT 0,
          warning_count  INTEGER NOT NULL DEFAULT 0,
          info_count     INTEGER NOT NULL DEFAULT 0,

          -- 검증이 완주하지 못한 경우(쿼리 오류 등)를 구분한다.
          -- 실패한 검증을 "이상 없음" 으로 읽으면 안 된다.
          status      TEXT NOT NULL DEFAULT 'RUNNING',
          error_text  TEXT,

          started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ,
          duration_ms INTEGER,
          actor_user_id UUID,

          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by  UUID,

          CONSTRAINT ck_mccr_ym CHECK (year_month ~ '^[0-9]{4}-[0-9]{2}$'),
          CONSTRAINT ck_mccr_trigger CHECK (
            trigger IN ('MANUAL','CONFIRM','LOCK','SCHEDULED','REOPEN')
          ),
          CONSTRAINT ck_mccr_status CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
          CONSTRAINT ck_mccr_state CHECK (
            close_state_at_run IN ('OPEN','REVIEW','CONFIRMED','LOCKED')
          ),
          -- 완주했으면 끝난 시각이 있어야 한다. 없는 run 은 프로세스가 죽은 것이다.
          CONSTRAINT ck_mccr_finished CHECK (
            status = 'RUNNING' OR finished_at IS NOT NULL
          )
        )
    """)
    op.execute("""
        CREATE INDEX ix_mccr_period ON monthly_close_check_run (org_id, year_month, started_at DESC)
    """)

    op.execute("""
        CREATE TABLE monthly_close_check_item (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id   UUID NOT NULL REFERENCES monthly_close_check_run(id) ON DELETE CASCADE,
          org_id   UUID NOT NULL REFERENCES organization(id),
          year_month TEXT NOT NULL,

          code     TEXT NOT NULL,
          severity TEXT NOT NULL,

          -- 어느 도메인의 몇 건인가. entity_id 는 대표 1건만 담는다 —
          -- 300건짜리 항목에 300행을 만들면 run 이력이 폭발한다.
          -- 개별 목록은 code 별 조회 API 가 raw 테이블에서 다시 뽑는다.
          entity_type TEXT,
          entity_id   UUID,
          affected_count INTEGER NOT NULL DEFAULT 0,

          message  TEXT NOT NULL,
          hint     TEXT,

          -- 이슈 센터(M030)와 연결하는 키. 같은 달 같은 코드는 같은 이슈다.
          dedupe_key TEXT NOT NULL,

          detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          CONSTRAINT ck_mcci_severity CHECK (severity IN ('BLOCKING','WARNING','INFO')),
          CONSTRAINT ck_mcci_ym CHECK (year_month ~ '^[0-9]{4}-[0-9]{2}$')
        )
    """)
    op.execute("""
        CREATE INDEX ix_mcci_run ON monthly_close_check_item (run_id, severity)
    """)
    op.execute("""
        CREATE INDEX ix_mcci_dedupe ON monthly_close_check_item (org_id, dedupe_key)
    """)
    # 한 run 안에서 같은 코드가 두 번 나오면 집계가 두 배가 된다.
    op.execute("""
        CREATE UNIQUE INDEX ux_mcci_run_code ON monthly_close_check_item (run_id, code)
    """)

    # ──────────────────────────────────────────────────────────────
    # 2. 확정 숫자 snapshot
    # ──────────────────────────────────────────────────────────────
    #
    # 컬럼은 **실제로 집계할 수 있는 값만** 만들었다. 정산 연동이 없어서
    # `confirmed_settlement_amount` 는 0 이 되지만, 컬럼을 없애는 대신
    # `settlement_connected` 플래그로 "0 원인지 모르는지" 를 구분한다.
    # 0 과 미연동을 같게 표시하면 대시보드가 거짓말을 한다.
    op.execute("""
        CREATE TABLE monthly_close_snapshot (
          id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id     UUID NOT NULL REFERENCES organization(id),
          year_month TEXT NOT NULL,
          monthly_settlement_id UUID NOT NULL REFERENCES monthly_settlement(id),

          -- reopen 세대. 1 부터 시작하고 재확정마다 +1.
          -- 과거 세대는 지우지 않고 superseded 로 남긴다.
          generation INTEGER NOT NULL DEFAULT 1,
          -- 이 snapshot 을 만든 전이. CONFIRMED 와 LOCKED 는 다른 사실이다.
          taken_at_state TEXT NOT NULL,
          taken_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          taken_by   UUID,
          reason     TEXT,

          -- ── 매출 ──
          order_count        INTEGER NOT NULL DEFAULT 0,
          gross_sales        BIGINT  NOT NULL DEFAULT 0,

          -- ── 상품 직접비 (product_margin_actual 의 실제 컬럼과 1:1) ──
          cogs               BIGINT  NOT NULL DEFAULT 0,
          shipping_cost      BIGINT  NOT NULL DEFAULT 0,
          marketplace_fees   BIGINT  NOT NULL DEFAULT 0,
          payment_fees       BIGINT  NOT NULL DEFAULT 0,
          advertising_cost   BIGINT  NOT NULL DEFAULT 0,
          fulfillment_fees   BIGINT  NOT NULL DEFAULT 0,
          returns_cost       BIGINT  NOT NULL DEFAULT 0,
          total_direct_cost  BIGINT  NOT NULL DEFAULT 0,

          -- ── 사업 공통비 ──
          business_expenses      BIGINT NOT NULL DEFAULT 0,
          -- 급여는 **비용**이다. 동업자 배분과 계산 단계가 다르므로 따로 센다.
          labor_expense          BIGINT NOT NULL DEFAULT 0,
          personal_card_expense  BIGINT NOT NULL DEFAULT 0,
          expense_vat            BIGINT NOT NULL DEFAULT 0,
          -- 이 달에 반영된 과거 월 조정(expense_adjustment)의 차액 합
          adjustment_total       BIGINT NOT NULL DEFAULT 0,

          -- ── 정산 ──
          settlement_connected   BOOLEAN NOT NULL DEFAULT FALSE,
          settlement_record_count INTEGER NOT NULL DEFAULT 0,
          settlement_unmatched_count INTEGER NOT NULL DEFAULT 0,
          confirmed_settlement_amount BIGINT NOT NULL DEFAULT 0,

          -- ── 손익 ──
          net_profit           BIGINT NOT NULL DEFAULT 0,
          carryover_offset     BIGINT NOT NULL DEFAULT 0,
          distributable_profit BIGINT NOT NULL DEFAULT 0,

          -- 주문 단위 이익 4단계 합계. net_profit(업무 집계)과 별개 관점이다.
          provisional_profit_total BIGINT NOT NULL DEFAULT 0,
          settled_profit_total     BIGINT NOT NULL DEFAULT 0,
          final_profit_total       BIGINT NOT NULL DEFAULT 0,

          -- 이 순이익이 무엇에 근거하는가. 정산 미연동이면 CONFIRMED 라고 말하지 않는다.
          profit_basis TEXT NOT NULL,

          -- ── 검증 결과 ──
          check_run_id     UUID REFERENCES monthly_close_check_run(id),
          blocking_count   INTEGER NOT NULL DEFAULT 0,
          warning_count    INTEGER NOT NULL DEFAULT 0,
          acknowledged_warnings_json JSONB,

          -- ── 재현 근거 ──
          -- 이 시각 이후에 들어온 데이터는 이 snapshot 에 없다.
          source_cutoff_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          applied_policy_versions_json JSONB,
          calculation_version TEXT NOT NULL,
          content_hash TEXT NOT NULL,

          -- ── 세대 교체 ──
          superseded_at TIMESTAMPTZ,
          superseded_by_id UUID REFERENCES monthly_close_snapshot(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,

          CONSTRAINT ck_mcs_ym CHECK (year_month ~ '^[0-9]{4}-[0-9]{2}$'),
          CONSTRAINT ck_mcs_state CHECK (taken_at_state IN ('CONFIRMED','LOCKED')),
          CONSTRAINT ck_mcs_generation CHECK (generation >= 1),
          CONSTRAINT ck_mcs_basis CHECK (
            profit_basis IN ('SALES_AND_EXPENSE_ONLY','SETTLEMENT_RECONCILED')
          ),
          -- 정산이 연결되지 않았는데 대사 기준이라고 주장할 수 없다.
          CONSTRAINT ck_mcs_basis_requires_settlement CHECK (
            profit_basis <> 'SETTLEMENT_RECONCILED' OR settlement_connected = TRUE
          ),
          -- blocking 이 남은 채로 확정된 snapshot 은 존재할 수 없다.
          CONSTRAINT ck_mcs_no_blocking CHECK (blocking_count = 0),
          -- 대체된 snapshot 은 대체한 대상을 가리켜야 한다(양쪽 다 채워지거나 다 빈다).
          CONSTRAINT ck_mcs_superseded_pair CHECK (
            (superseded_at IS NULL) = (superseded_by_id IS NULL)
          ),
          CONSTRAINT ck_mcs_hash CHECK (length(content_hash) = 64)
        )
    """)

    # 한 달의 한 세대에 snapshot 은 하나다.
    op.execute("""
        CREATE UNIQUE INDEX ux_mcs_period_generation
            ON monthly_close_snapshot (org_id, year_month, generation)
    """)
    # 살아 있는(대체되지 않은) snapshot 은 달마다 하나여야 한다.
    # 대시보드가 "확정 숫자" 를 물으면 이 인덱스가 유일성을 보장한다.
    op.execute("""
        CREATE UNIQUE INDEX ux_mcs_current
            ON monthly_close_snapshot (org_id, year_month)
         WHERE superseded_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_mcs_settlement ON monthly_close_snapshot (monthly_settlement_id)
    """)

    # ── 불변성 트리거 ──
    #
    # `superseded_at` / `superseded_by_id` 만 바꿀 수 있다. 나머지는 거부한다.
    # to_jsonb 로 행 전체를 비교하고 허용 키를 빼서 본다 — 컬럼을 나중에
    # 추가해도 자동으로 보호 대상이 된다(허용 목록에 넣지 않으면 잠긴다).
    op.execute("""
        CREATE OR REPLACE FUNCTION mcs_forbid_mutation() RETURNS TRIGGER AS $$
        DECLARE
          before_json JSONB;
          after_json  JSONB;
        BEGIN
          before_json := to_jsonb(OLD) - 'superseded_at' - 'superseded_by_id';
          after_json  := to_jsonb(NEW) - 'superseded_at' - 'superseded_by_id';
          IF before_json <> after_json THEN
            RAISE EXCEPTION
              '월 마감 snapshot 은 수정할 수 없습니다 (year_month=%, generation=%). '
              '정정이 필요하면 월을 재오픈하고 새 세대를 만드세요.',
              OLD.year_month, OLD.generation
              USING ERRCODE = 'restrict_violation';
          END IF;
          -- 이미 대체된 snapshot 을 다시 되돌리는 것도 막는다.
          IF OLD.superseded_at IS NOT NULL AND NEW.superseded_at IS NULL THEN
            RAISE EXCEPTION '이미 대체된 snapshot 을 되돌릴 수 없습니다.'
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_mcs_immutable
          BEFORE UPDATE ON monthly_close_snapshot
          FOR EACH ROW EXECUTE FUNCTION mcs_forbid_mutation()
    """)
    # 삭제도 막는다. soft delete 컬럼을 두지 않은 이유가 이것이다 —
    # 확정 숫자는 지워지지 않는 사실이어야 한다.
    #
    # 유지보수 탈출구
    # -------------
    # 조직 데이터 완전 삭제나 테스트 teardown 처럼 **행 자체를 없애야 하는** 경우가
    # 있다. 그때 트리거를 DROP 했다 다시 만드는 것보다 세션 단위 플래그가 낫다 —
    # DROP 을 잊으면 보호가 영구히 사라진다.
    #
    #     SET LOCAL picksome.allow_snapshot_delete = 'on';
    #
    # `SET LOCAL` 이므로 트랜잭션이 끝나면 자동으로 풀린다.
    # **애플리케이션 코드는 이 플래그를 설정하지 않는다.** 설정하는 곳이 생기면
    # 그건 확정 숫자를 지우는 경로가 열렸다는 뜻이므로 리뷰에서 잡아야 한다.
    op.execute("""
        CREATE OR REPLACE FUNCTION mcs_forbid_delete() RETURNS TRIGGER AS $$
        BEGIN
          IF current_setting('picksome.allow_snapshot_delete', TRUE) = 'on' THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION
            '월 마감 snapshot 은 삭제할 수 없습니다 (year_month=%). '
            '유지보수로 삭제해야 하면 SET LOCAL picksome.allow_snapshot_delete = ''on'' 이 필요합니다.',
            OLD.year_month
            USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_mcs_no_delete
          BEFORE DELETE ON monthly_close_snapshot
          FOR EACH ROW EXECUTE FUNCTION mcs_forbid_delete()
    """)

    # ──────────────────────────────────────────────────────────────
    # 3. monthly_settlement 에 현재 세대 포인터
    # ──────────────────────────────────────────────────────────────
    #
    # 기존 `snapshot_id` 는 M001 에서 `policy_snapshot` 을 가리키도록 만들어졌다
    # (정책 박제용). 의미가 다르므로 재사용하지 않고 새 컬럼을 만든다 —
    # 같은 컬럼에 두 종류의 참조를 넣으면 FK 를 걸 수 없다.
    op.execute("""
        ALTER TABLE monthly_settlement
          ADD COLUMN close_snapshot_id UUID REFERENCES monthly_close_snapshot(id),
          ADD COLUMN close_generation INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN last_check_run_id UUID REFERENCES monthly_close_check_run(id)
    """)
    op.execute("""
        COMMENT ON COLUMN monthly_settlement.close_snapshot_id IS
          '현재 유효한 확정 snapshot. 재오픈하면 NULL 이 되고 재확정 시 새 세대를 가리킨다.'
    """)
    op.execute("""
        COMMENT ON COLUMN monthly_settlement.close_generation IS
          '확정 세대. 0 = 한 번도 확정하지 않음. 재확정마다 +1.'
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE monthly_settlement
          DROP COLUMN IF EXISTS last_check_run_id,
          DROP COLUMN IF EXISTS close_generation,
          DROP COLUMN IF EXISTS close_snapshot_id
    """)
    # 트리거가 DELETE 를 막으므로 DROP TABLE 전에 트리거를 먼저 뗀다.
    # (DROP TABLE 자체는 트리거를 타지 않지만, 순서를 명시해 의도를 남긴다.)
    op.execute("DROP TRIGGER IF EXISTS trg_mcs_no_delete ON monthly_close_snapshot")
    op.execute("DROP TRIGGER IF EXISTS trg_mcs_immutable ON monthly_close_snapshot")
    op.execute("DROP TABLE IF EXISTS monthly_close_snapshot")
    op.execute("DROP FUNCTION IF EXISTS mcs_forbid_delete()")
    op.execute("DROP FUNCTION IF EXISTS mcs_forbid_mutation()")
    op.execute("DROP TABLE IF EXISTS monthly_close_check_item")
    op.execute("DROP TABLE IF EXISTS monthly_close_check_run")
