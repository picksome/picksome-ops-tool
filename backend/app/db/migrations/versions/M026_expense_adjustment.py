"""M026 — 잠긴 월 비용 조정 (expense_adjustment).

문제
----
7월이 마감·잠긴 뒤에 7월 비용이 잘못됐거나 누락된 게 발견된다. 실무에서 흔하다.

그때 할 수 없는 것과 할 수 있는 것을 나눠야 한다.

  **할 수 없다**: 7월 비용을 고친다. 7월 순이익과 수익배분이 이미 확정됐고,
                 파트너에게 정산까지 끝났을 수 있다. 과거를 바꾸면 그 배분이
                 소급해서 틀린 값이 된다.
  **할 수 있다**: 지금 열린 달에 **조정 항목**을 만들어 차액을 반영한다.
                 회계에서 하는 방식 그대로다.

그래서 이 테이블이 있다
--------------------
`expenses` 를 self-reference 로 만들지 않고 별도 테이블로 둔 이유:

  · 조정은 비용과 **다른 필드**가 필요하다(원본 참조, 조정 유형, 사유, 차액)
  · `expenses` 에 nullable 컬럼 5개를 더하면 일반 비용 행 대부분이 NULL 이 되고,
    "이게 조정인가" 를 매번 판단해야 한다
  · 조정은 승인을 거쳐야만 생기는데, 일반 비용은 그렇지 않다

조정이 실제 비용 행을 만드는가
---------------------------
**만든다.** `adjustment_expense_id` 가 그 행을 가리킨다. 그래야 월별 집계·세무
리포트가 조정을 자동으로 포함한다 — 집계 쿼리마다 "조정도 더해라" 를 넣으면
언젠가 빠뜨린다.

`expense_adjustment` 는 그 비용이 **왜 생겼는지**를 설명하는 메타데이터다.
"""

from alembic import op

revision = "M026"
down_revision = "M025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE expense_adjustment (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id       UUID NOT NULL REFERENCES organization(id),

          -- 무엇을 조정하는가. 누락분 신규 등록이면 NULL 이 될 수 있다.
          original_expense_id UUID REFERENCES expenses(id),

          -- 조정으로 실제 생성된 비용 행. 집계는 이 행을 통해 반영된다.
          adjustment_expense_id UUID NOT NULL REFERENCES expenses(id),

          adjustment_type TEXT NOT NULL,
          adjustment_reason TEXT NOT NULL,

          -- 원본이 귀속된 달(YYYY-MM). 잠겨 있어서 조정으로 처리한 그 달.
          original_period TEXT,
          -- 조정이 실제로 반영되는 달. 지금 열린 달이다.
          adjustment_period TEXT NOT NULL,

          -- 차액. 원본을 늘리면 양수, 되돌리면 음수.
          -- 금액은 정수(원). float 금지.
          amount_delta BIGINT NOT NULL,

          approval_request_id UUID REFERENCES approval_request(id) ON DELETE SET NULL,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID,
          deleted_at TIMESTAMPTZ,
          deleted_by UUID,

          CONSTRAINT ck_ea_type CHECK (
            adjustment_type IN ('LATE_ENTRY','CORRECTION','REVERSAL','RECLASSIFICATION')
          ),
          CONSTRAINT ck_ea_period_format CHECK (
            adjustment_period ~ '^[0-9]{4}-[0-9]{2}$'
            AND (original_period IS NULL OR original_period ~ '^[0-9]{4}-[0-9]{2}$')
          ),
          -- 정정·되돌림·재분류는 원본이 있어야 한다. 누락분 등록만 원본이 없다.
          CONSTRAINT ck_ea_original_required CHECK (
            adjustment_type = 'LATE_ENTRY' OR original_expense_id IS NOT NULL
          ),
          -- 사유 없는 조정은 나중에 아무도 설명하지 못한다.
          CONSTRAINT ck_ea_reason_not_blank CHECK (length(btrim(adjustment_reason)) > 0)
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX ux_ea_adjustment_expense
            ON expense_adjustment (adjustment_expense_id)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_ea_original
            ON expense_adjustment (org_id, original_expense_id)
         WHERE original_expense_id IS NOT NULL AND deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_ea_period ON expense_adjustment (org_id, adjustment_period)
    """)

    op.execute("""
        CREATE TRIGGER trg_expense_adjustment_updated_at
          BEFORE UPDATE ON expense_adjustment
          FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # ── 재계산 필요 표시 ──
    #
    # 비용이 바뀌면 그 달의 잠정 손익이 낡는다. 그렇다고 승인 트랜잭션 안에서
    # 전체를 다시 집계하지 않는다 — 느리고, 실패하면 승인이 통째로 롤백된다.
    #
    # **표시만 남긴다.** 집계 화면이 이 표시를 보고 다시 계산하거나,
    # 배치가 나중에 처리한다. 표시와 비용 변경이 같은 트랜잭션이라
    # "비용은 바뀌었는데 표시가 없다" 가 생기지 않는다.
    op.execute("""
        CREATE TABLE period_recalc_flag (
          org_id       UUID NOT NULL REFERENCES organization(id),
          year_month   TEXT NOT NULL,
          reason       TEXT NOT NULL,
          marked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          marked_by    UUID,
          resolved_at  TIMESTAMPTZ,
          PRIMARY KEY (org_id, year_month),
          CONSTRAINT ck_prf_ym CHECK (year_month ~ '^[0-9]{4}-[0-9]{2}$')
        )
    """)
    op.execute("""
        CREATE INDEX ix_prf_unresolved ON period_recalc_flag (org_id)
         WHERE resolved_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS period_recalc_flag")
    op.execute("DROP TABLE IF EXISTS expense_adjustment")
