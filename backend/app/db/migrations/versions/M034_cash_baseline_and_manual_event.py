"""M034 — 기준 잔액 + 수동 현금 기록.

`docs/54_cashflow_semantics.md` 의 결론을 구현한다.

무엇을 만들지 않는가
------------------
**generic cashflow ledger 를 만들지 않는다.**

`payout_payment` 는 이미 "이 날 이 금액을 보냈다" 는 사실을 갖고 있다.
그걸 cashflow row 로 또 만들면 지급을 취소했을 때 두 곳을 고쳐야 하고,
하나만 고쳐지는 순간 이중 계상이 된다. 현금흐름은 **읽기 모델**로 계산한다
(`services/cashflow.py`).

여기서 만드는 두 테이블은 **기존 source 로 표현할 수 없는 것**뿐이다.

1. `cash_balance_baseline`
   시작 잔액. 은행 API 가 없으므로 사람이 "이 날 기준 얼마" 를 입력한다.
   이게 없으면 예상 잔액을 계산할 수 없다 — 그때는 잔액을 아예 보여주지 않는다.
   0 에서 시작한 것처럼 그리면 "통장에 0원" 으로 읽힌다.

2. `manual_cash_event`
   은행 API 없이 실제 입출금을 기록하는 유일한 경로.
   특히 **입금 쪽에서 ACTUAL 을 만들 수 있는 유일한 근거**다
   (`settled_at` 은 실제 입금이라는 근거가 없다 — docs/54 §3-2).

이중 계상 방지
------------
`manual_cash_event` 가 이미 `payout_payment` 로 기록된 지급을 수동 출금으로
또 넣을 수 있으면 출금이 두 배가 된다. 애플리케이션 검사만으로는 동시 요청에서
뚫리므로 **부분 UNIQUE 로 DB 가 막는다.**

`source_type`/`source_id` 가 NULL 인 이벤트(순수 수동 입력)는 검사 대상이 아니다 —
같은 날 같은 금액의 현금 지출이 두 번 있을 수 있고 그건 중복이 아니다.

금액 부호
--------
`amount` 는 **항상 양수**다. 방향은 `direction` 이 정한다.
부호로 방향을 표현하면 합계 쿼리에서 실수 하나가 유입을 유출로 만든다.
"""

from alembic import op

revision = "M034"
down_revision = "M033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────
    # 1. 기준 잔액
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE cash_balance_baseline (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),

          -- 이 날짜 "시작 시점" 의 잔액이다. 그 날의 입출금은 포함하지 않는다.
          as_of_date DATE NOT NULL,
          -- 음수 가능 — 마이너스 통장을 쓸 수 있다.
          amount     BIGINT NOT NULL,
          -- 어디를 보고 적었는지. "국민 1234 통장 잔액" 같은 메모.
          -- 계좌번호 전체를 적지 않도록 화면이 안내한다.
          memo       TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id)
        )
    """)

    # 같은 날짜에 기준 잔액이 둘이면 어느 쪽이 맞는지 알 수 없다.
    # 고치고 싶으면 그 행을 수정한다(새로 넣는 게 아니라).
    op.execute("""
        CREATE UNIQUE INDEX uq_cash_baseline_org_date
            ON cash_balance_baseline (org_id, as_of_date)
         WHERE deleted_at IS NULL
    """)

    op.execute("""
        COMMENT ON TABLE cash_balance_baseline IS
        '사용자가 입력한 기준 잔액. 은행 연동이 아니므로 "실제 계좌 잔액"이 아니다.'
    """)

    # ──────────────────────────────────────────────────────────────
    # 2. 수동 현금 기록
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE manual_cash_event (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),

          direction TEXT NOT NULL,
          status    TEXT NOT NULL,

          -- 항상 양수. 방향은 direction 이 정한다.
          amount   BIGINT NOT NULL,
          currency TEXT NOT NULL DEFAULT 'KRW',

          -- 실제로 움직인 날 (ACTUAL 일 때 필수)
          occurred_at DATE,
          -- 움직일 것으로 보는 날 (EXPECTED/CONFIRMED 일 때 필수)
          expected_at DATE,

          category TEXT,
          memo     TEXT,

          -- 기존 source 를 사람이 확인한 경우 그 출처를 남긴다.
          -- 예: payout_payment 를 통장에서 확인 → source_type='payout_payment'
          source_type TEXT,
          source_id   UUID,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_mce_direction CHECK (direction IN ('INFLOW', 'OUTFLOW')),
          CONSTRAINT ck_mce_status    CHECK (status IN ('EXPECTED', 'CONFIRMED', 'ACTUAL')),
          -- 0원짜리 현금 이동은 기록할 이유가 없고, 음수는 방향과 충돌한다.
          CONSTRAINT ck_mce_amount    CHECK (amount > 0),

          -- **ACTUAL 은 날짜 없이 존재할 수 없다.** "실제로 움직였다" 는데
          -- 언제인지 모르면 현금흐름 어느 칸에도 넣을 수 없다.
          CONSTRAINT ck_mce_actual_needs_date
            CHECK (status <> 'ACTUAL' OR occurred_at IS NOT NULL),
          -- 아직 안 움직인 것은 예정일이 있어야 기간 집계에 들어간다.
          CONSTRAINT ck_mce_pending_needs_date
            CHECK (status = 'ACTUAL' OR expected_at IS NOT NULL),
          -- source 는 둘 다 있거나 둘 다 없다.
          CONSTRAINT ck_mce_source_pair
            CHECK ((source_type IS NULL) = (source_id IS NULL))
        )
    """)

    # 이중 계상 방지 — 같은 원본을 두 번 현금흐름에 넣을 수 없다.
    op.execute("""
        CREATE UNIQUE INDEX uq_manual_cash_source
            ON manual_cash_event (org_id, source_type, source_id)
         WHERE source_type IS NOT NULL AND deleted_at IS NULL
    """)

    # 기간 조회가 이 테이블의 유일한 접근 패턴이다.
    # (다른 인덱스는 실제 query plan 을 보고 판단한다 — Phase 18)
    op.execute("""
        CREATE INDEX ix_manual_cash_org_dates
            ON manual_cash_event (org_id, COALESCE(occurred_at, expected_at))
         WHERE deleted_at IS NULL
    """)

    op.execute("""
        COMMENT ON COLUMN manual_cash_event.status IS
        'EXPECTED=예상, CONFIRMED=상대가 금액을 확정, ACTUAL=돈이 실제로 움직였다. ACTUAL 은 occurred_at 이 있어야 한다.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS manual_cash_event")
    op.execute("DROP TABLE IF EXISTS cash_balance_baseline")
