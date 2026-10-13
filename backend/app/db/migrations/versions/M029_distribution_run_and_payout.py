"""M029 — 확정 수익배분 + 지급 장부.

두 가지를 분리한다
----------------
```
① 얼마를 나눠야 하는가   distribution_run / distribution_result   (계산)
② 실제로 돈을 보냈는가   payout_batch / payout_item / payout_payment (현금)
```

이 둘을 한 테이블로 합치면 "계산은 됐는데 아직 안 준 돈" 과 "준 돈" 이 구분되지
않는다. 그리고 급여처럼 **계산은 배분 엔진 밖에서 이뤄지는데 지급은 같이 관리해야
하는** 항목이 들어갈 자리가 없어진다.

기존 `partner_settlement` 는 왜 재사용하지 않았나
--------------------------------------------
`partner_settlement` 은 `settlement_crud.create_settlement` 이 **계산할 때마다**
지우고 다시 쓴다. 즉 시뮬레이션 결과다. 월을 재계산하면 값이 바뀐다.

확정 배분은 그럴 수 없다 — 확정 시점의 snapshot 을 근거로 만들어지고,
나중에 정책이 바뀌어도 그대로여야 한다. 성질이 다르므로 테이블을 나눈다.
`partner_settlement` 은 그대로 두고(미리보기 용도) 건드리지 않는다.

회계 효과가 서로 다르다
--------------------
지급 장부는 현금 흐름을 한 곳에서 보기 위한 것이지 **손익을 만들지 않는다.**

| 항목 | 손익에 이미 반영됐나 | 지급 시 |
|---|---|---|
| 직원 급여 | 반영됨 (LABOR 비용) | 현금 지급 사실만 기록 |
| 동업자 배분 | **아니다** — 순이익 후 이익 처분 | 비용으로 만들지 않는다 |
| 개인카드 상환 | 반영됨 (원래 비용) | **새 비용을 만들지 않는다** |

`accounting_effect` 컬럼이 이 구분을 행마다 명시한다. 나중에 누군가
"지급했으니 비용 처리하자" 고 생각했을 때 이 컬럼이 답을 준다.

PAID 는 지급 기록이 있어야만 가능하다
---------------------------------
`paid_amount` 는 **트리거가 `payout_payment` 합계에서 계산**한다. 서비스가
직접 쓰지 않는다. 거기에 CHECK 를 걸어 `status='PAID'` 이면
`paid_amount = due_amount` 를 요구한다.

  → 지급 기록 없이 PAID 로 만드는 경로가 **DB 수준에서 존재하지 않는다.**

과지급도 CHECK 로 막는다. 과지급을 지원해야 할 업무 근거가 확인되지 않았고,
근거 없이 열어 두면 오타 한 번이 조용히 통과한다.

cap / minimum 은 만들지 않았다
---------------------------
`settlement_policy` 와 `partner_share_policy` 에 상한·하한 컬럼이 **없다.**
저장할 값이 없는 컬럼을 만들면 화면이 "상한 없음" 인지 "기능 미구현" 인지
구분하지 못한다. 정책에 개념이 생기면 그때 함께 추가한다.
"""

from alembic import op

revision = "M029"
down_revision = "M028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────
    # 1. 확정 수익배분
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE distribution_run (
          id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id     UUID NOT NULL REFERENCES organization(id),
          year_month TEXT NOT NULL,
          monthly_settlement_id UUID NOT NULL REFERENCES monthly_settlement(id),

          -- 확정 snapshot 없이는 확정 배분이 없다. NOT NULL 이 그걸 강제한다.
          close_snapshot_id UUID NOT NULL REFERENCES monthly_close_snapshot(id),
          snapshot_generation INTEGER NOT NULL,

          -- 배분 기준 금액. snapshot 의 distributable_profit 을 복사해 둔다 —
          -- 조인 없이 이 행만 보고 검증할 수 있어야 한다.
          distributable_base BIGINT NOT NULL,
          carryover_applied  BIGINT NOT NULL DEFAULT 0,
          total_distributed  BIGINT NOT NULL DEFAULT 0,

          status TEXT NOT NULL DEFAULT 'CALCULATED',

          -- 적용된 규칙 버전. 나중에 정책이 바뀌어도 이 값은 그대로다.
          applied_rule_versions_json JSONB,
          loss_policy TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          approved_at TIMESTAMPTZ,
          approved_by UUID,
          superseded_at TIMESTAMPTZ,
          superseded_by_id UUID REFERENCES distribution_run(id),

          CONSTRAINT ck_dr_ym CHECK (year_month ~ '^[0-9]{4}-[0-9]{2}$'),
          CONSTRAINT ck_dr_status CHECK (
            status IN ('CALCULATED','APPROVED','SUPERSEDED','CANCELLED')
          ),
          CONSTRAINT ck_dr_amounts CHECK (
            distributable_base >= 0 AND total_distributed >= 0
            AND carryover_applied >= 0
          ),
          -- 배분 합계가 기준 금액을 넘을 수 없다. 넘으면 없는 돈을 나눈 것이다.
          CONSTRAINT ck_dr_not_over CHECK (total_distributed <= distributable_base),
          CONSTRAINT ck_dr_superseded_pair CHECK (
            (superseded_at IS NULL) = (superseded_by_id IS NULL)
          )
        )
    """)
    # 살아 있는 확정 배분은 달마다 하나다.
    op.execute("""
        CREATE UNIQUE INDEX ux_dr_current ON distribution_run (org_id, year_month)
         WHERE superseded_at IS NULL AND status <> 'CANCELLED'
    """)
    op.execute("CREATE INDEX ix_dr_snapshot ON distribution_run (close_snapshot_id)")

    op.execute("""
        CREATE TABLE distribution_result (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id   UUID NOT NULL REFERENCES distribution_run(id) ON DELETE CASCADE,
          org_id   UUID NOT NULL REFERENCES organization(id),

          partner_id UUID NOT NULL REFERENCES partner(id),
          -- 파트너 이름이 나중에 바뀌거나 파트너가 비활성화돼도
          -- "누구에게 얼마" 를 말할 수 있어야 한다.
          partner_name_snapshot TEXT NOT NULL,

          -- 현재 스키마에서 배분 대상은 내부 파트너뿐이다.
          -- 투자자·외부 참여자 개념이 생기면 값을 늘린다.
          participant_type TEXT NOT NULL DEFAULT 'PARTNER',

          distribution_kind TEXT NOT NULL,
          -- 이 행의 계산 근거가 된 금액. PRIORITY 는 전체 기준,
          -- RATIO 는 우선분배를 뺀 잔액이다 — 비율만 봐서는 금액을 검증할 수 없다.
          calculation_base BIGINT NOT NULL,
          ratio_used NUMERIC(6,5),
          priority_order INTEGER,
          amount BIGINT NOT NULL,
          note TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          CONSTRAINT ck_drs_kind CHECK (distribution_kind IN ('PRIORITY','RATIO')),
          CONSTRAINT ck_drs_participant CHECK (participant_type IN ('PARTNER')),
          CONSTRAINT ck_drs_amount CHECK (amount >= 0),
          -- 비율 배분인데 비율이 없으면 금액을 재현할 수 없다.
          CONSTRAINT ck_drs_ratio_present CHECK (
            distribution_kind <> 'RATIO' OR ratio_used IS NOT NULL
          )
        )
    """)
    op.execute("""
        CREATE INDEX ix_drs_run ON distribution_result (run_id)
    """)
    # 한 run 에서 같은 파트너가 같은 종류로 두 번 나오면 합계가 어긋난다.
    op.execute("""
        CREATE UNIQUE INDEX ux_drs_run_partner_kind
            ON distribution_result (run_id, partner_id, distribution_kind)
    """)

    # ──────────────────────────────────────────────────────────────
    # 2. 지급 장부
    # ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE payout_batch (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id       UUID NOT NULL REFERENCES organization(id),
          business_key TEXT NOT NULL,
          title        TEXT NOT NULL,

          -- 어느 달의 지급인가. 상환처럼 특정 월에 묶이지 않는 배치도 있어 NULL 허용.
          period_year_month TEXT,

          -- 배분에서 만들어진 배치면 그 run 을 가리킨다.
          source_distribution_run_id UUID REFERENCES distribution_run(id),

          status TEXT NOT NULL DEFAULT 'DRAFT',
          memo   TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID,
          approved_at TIMESTAMPTZ,
          approved_by UUID,
          cancelled_at TIMESTAMPTZ,
          cancelled_by UUID,
          deleted_at TIMESTAMPTZ,
          deleted_by UUID,

          CONSTRAINT ck_pb_status CHECK (
            status IN ('DRAFT','APPROVED','PARTIALLY_PAID','PAID','CANCELLED')
          ),
          CONSTRAINT ck_pb_ym CHECK (
            period_year_month IS NULL OR period_year_month ~ '^[0-9]{4}-[0-9]{2}$'
          )
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_pb_business_key ON payout_batch (org_id, business_key)
    """)
    op.execute("""
        CREATE INDEX ix_pb_period ON payout_batch (org_id, period_year_month, status)
    """)
    # 한 배분 run 에서 지급 배치가 두 개 만들어지면 같은 돈을 두 번 준다.
    op.execute("""
        CREATE UNIQUE INDEX ux_pb_distribution_run
            ON payout_batch (source_distribution_run_id)
         WHERE source_distribution_run_id IS NOT NULL
           AND status <> 'CANCELLED' AND deleted_at IS NULL
    """)

    op.execute("""
        CREATE TABLE payout_item (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),
          batch_id UUID NOT NULL REFERENCES payout_batch(id) ON DELETE CASCADE,

          item_type TEXT NOT NULL,

          -- 회계 효과. 이 값이 "지급했으니 비용 처리하자" 를 막는다.
          accounting_effect TEXT NOT NULL,

          payee_partner_id UUID REFERENCES partner(id),
          payee_name_snapshot TEXT NOT NULL,

          -- 출처. 계산 결과에서 왔으면 그 행을, 개인카드 상환이면 원래 비용을 가리킨다.
          source_distribution_result_id UUID REFERENCES distribution_result(id),
          source_expense_id UUID REFERENCES expenses(id),

          due_amount  BIGINT NOT NULL,
          -- ⚠️ 트리거가 payout_payment 합계로 관리한다. 서비스가 직접 쓰지 않는다.
          paid_amount BIGINT NOT NULL DEFAULT 0,

          status TEXT NOT NULL DEFAULT 'CALCULATED',
          due_date DATE,
          memo TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID,
          deleted_at TIMESTAMPTZ,
          deleted_by UUID,

          CONSTRAINT ck_pi_type CHECK (item_type IN (
            'PARTNER_DISTRIBUTION','EMPLOYEE_SALARY','EXPENSE_REIMBURSEMENT',
            'INVESTOR_DISTRIBUTION','OTHER'
          )),
          CONSTRAINT ck_pi_effect CHECK (accounting_effect IN (
            -- 이미 비용으로 손익에 반영됨 (급여·개인카드 원비용)
            'ALREADY_EXPENSED',
            -- 순이익 후 이익 처분. 비용이 아니다
            'PROFIT_APPROPRIATION',
            -- 기타 (손익 영향 없음이 확인된 경우)
            'NONE'
          )),
          -- 동업자 배분을 비용으로 만들지 않는다. 유형과 회계효과를 짝지어 고정한다.
          CONSTRAINT ck_pi_type_effect CHECK (
            (item_type = 'PARTNER_DISTRIBUTION'   AND accounting_effect = 'PROFIT_APPROPRIATION') OR
            (item_type = 'INVESTOR_DISTRIBUTION'  AND accounting_effect = 'PROFIT_APPROPRIATION') OR
            (item_type = 'EMPLOYEE_SALARY'        AND accounting_effect = 'ALREADY_EXPENSED') OR
            (item_type = 'EXPENSE_REIMBURSEMENT'  AND accounting_effect = 'ALREADY_EXPENSED') OR
            (item_type = 'OTHER')
          ),
          CONSTRAINT ck_pi_status CHECK (status IN (
            'CALCULATED','APPROVED','PARTIALLY_PAID','PAID','CANCELLED','CARRIED_FORWARD'
          )),
          CONSTRAINT ck_pi_due_positive CHECK (due_amount > 0),
          CONSTRAINT ck_pi_paid_range CHECK (paid_amount >= 0),
          -- 과지급 차단. 업무 근거가 확인되지 않았고, 열어 두면 오타가 조용히 통과한다.
          CONSTRAINT ck_pi_no_overpay CHECK (paid_amount <= due_amount),
          -- PAID 는 실제 지급 기록이 due_amount 를 채웠을 때만 가능하다.
          CONSTRAINT ck_pi_paid_requires_payment CHECK (
            status <> 'PAID' OR paid_amount = due_amount
          ),
          CONSTRAINT ck_pi_partially_paid CHECK (
            status <> 'PARTIALLY_PAID' OR (paid_amount > 0 AND paid_amount < due_amount)
          ),
          -- 상환 항목은 원본 비용을 가리켜야 한다. 안 가리키면 중복 방지를 못 건다.
          CONSTRAINT ck_pi_reimbursement_source CHECK (
            item_type <> 'EXPENSE_REIMBURSEMENT' OR source_expense_id IS NOT NULL
          )
        )
    """)
    op.execute("CREATE INDEX ix_pi_batch ON payout_item (batch_id)")
    op.execute("""
        CREATE INDEX ix_pi_open ON payout_item (org_id, status)
         WHERE status IN ('CALCULATED','APPROVED','PARTIALLY_PAID') AND deleted_at IS NULL
    """)
    # 같은 비용에 상환 항목이 두 개 생기면 두 번 갚는다.
    op.execute("""
        CREATE UNIQUE INDEX ux_pi_reimbursement_once
            ON payout_item (source_expense_id)
         WHERE item_type = 'EXPENSE_REIMBURSEMENT'
           AND status <> 'CANCELLED' AND deleted_at IS NULL
    """)
    # 같은 배분 결과에 지급 항목이 두 개 생기면 두 번 준다.
    op.execute("""
        CREATE UNIQUE INDEX ux_pi_distribution_once
            ON payout_item (source_distribution_result_id)
         WHERE source_distribution_result_id IS NOT NULL
           AND status <> 'CANCELLED' AND deleted_at IS NULL
    """)
    op.execute("""
        CREATE TRIGGER trg_payout_item_updated_at
          BEFORE UPDATE ON payout_item
          FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    op.execute("""
        CREATE TRIGGER trg_payout_batch_updated_at
          BEFORE UPDATE ON payout_batch
          FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # ──────────────────────────────────────────────────────────────
    # 3. 지급 기록
    # ──────────────────────────────────────────────────────────────
    #
    # 계좌번호를 저장하지 않는다. 지급 사실을 기록하는 데 필요하지 않고,
    # 저장하면 유출 시 피해가 커진다. 필요해지면 vault 참조만 둔다.
    op.execute("""
        CREATE TABLE payout_payment (
          id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id   UUID NOT NULL REFERENCES organization(id),
          payout_item_id UUID NOT NULL REFERENCES payout_item(id) ON DELETE CASCADE,

          payment_date DATE NOT NULL,
          amount       BIGINT NOT NULL,
          -- 이체 확인용 참조(거래번호 등). 계좌번호가 아니다.
          payment_reference TEXT,
          memo TEXT,

          recorded_by UUID,
          recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          evidence_attachment_id UUID REFERENCES expense_attachment(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID,
          deleted_at TIMESTAMPTZ,
          deleted_by UUID,

          CONSTRAINT ck_pp_amount_positive CHECK (amount > 0)
        )
    """)
    op.execute("CREATE INDEX ix_pp_item ON payout_payment (payout_item_id)")
    op.execute("CREATE INDEX ix_pp_date ON payout_payment (org_id, payment_date DESC)")

    # ── paid_amount 는 트리거가 계산한다 ──
    #
    # 서비스가 직접 쓰면 지급 기록 없이 PAID 를 만드는 경로가 생긴다.
    # 여기서만 쓰고, CHECK 가 status 와의 일관성을 강제한다.
    op.execute("""
        CREATE OR REPLACE FUNCTION payout_item_recalc_paid() RETURNS TRIGGER AS $$
        DECLARE
          target UUID;
          total  BIGINT;
        BEGIN
          target := COALESCE(NEW.payout_item_id, OLD.payout_item_id);
          SELECT COALESCE(SUM(amount), 0) INTO total
            FROM payout_payment
           WHERE payout_item_id = target AND deleted_at IS NULL;

          UPDATE payout_item
             SET paid_amount = total,
                 -- 금액이 채워지면 상태를 함께 올린다. 사람이 따로 누르지 않아도
                 -- "돈은 다 줬는데 상태가 APPROVED" 같은 불일치가 생기지 않는다.
                 status = CASE
                   WHEN status IN ('CANCELLED','CARRIED_FORWARD') THEN status
                   WHEN total = 0 THEN
                     CASE WHEN status IN ('PARTIALLY_PAID','PAID') THEN 'APPROVED' ELSE status END
                   WHEN total >= due_amount THEN 'PAID'
                   ELSE 'PARTIALLY_PAID'
                 END
           WHERE id = target;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_pp_recalc
          AFTER INSERT OR UPDATE OR DELETE ON payout_payment
          FOR EACH ROW EXECUTE FUNCTION payout_item_recalc_paid()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_pp_recalc ON payout_payment")
    op.execute("DROP TABLE IF EXISTS payout_payment")
    op.execute("DROP FUNCTION IF EXISTS payout_item_recalc_paid()")
    op.execute("DROP TABLE IF EXISTS payout_item")
    op.execute("DROP TABLE IF EXISTS payout_batch")
    op.execute("DROP TABLE IF EXISTS distribution_result")
    op.execute("DROP TABLE IF EXISTS distribution_run")
