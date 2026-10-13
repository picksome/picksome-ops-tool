"""M060 — 보류 상태와 승인 시점 근거 박제.

왜 필요한가
----------
### 1. 보류는 실패가 아니다

M059 는 `CANDIDATE / APPROVED / RETIRED` 세 상태만 뒀다. 사용자가 "지금은
판단할 수 없다" 고 할 자리가 없어서, 확인하지 못한 것과 아직 안 본 것이
같은 `CANDIDATE` 로 뭉친다.

그러면 화면이 "16건 확인 필요" 라고 계속 말하고, 사용자는 이미 보고 미룬
것을 매번 다시 연다. `DEFERRED` 는 **"봤고, 지금은 못 정한다"** 이다 —
계산이 틀렸다는 뜻이 아니므로 실패로 세지 않는다.

### 2. 승인 근거는 그때의 사실이어야 한다

`expected_json` 에는 기준 **값**만 있다. 그 값이 **어떤 원천에서 나왔는지**
는 남지 않는다.

원천은 나중에 바뀐다. 정산서를 다시 올리거나 원가를 고치면, 승인 당시
사용자가 무엇을 보고 판단했는지 되짚을 수 없다. 그러면 회귀가 FAIL 났을 때
"기준이 틀렸나, 계산이 바뀌었나" 를 가릴 수 없다.

`approved_evidence_json` 은 승인 순간의 원천·출처·독립 검산을 그대로 박제
한다. **읽기 전용이다** — 어떤 화면도 여기서 금액을 가져다 표시하지 않는다.

왜 컬럼을 나누나
--------------
`expected_json` 에 섞어 넣으면 회귀 비교가 값과 근거를 구분해야 하고, 그
분기가 곧 버그가 된다. 값은 값대로, 근거는 근거대로 둔다.
"""

from alembic import op

revision = "M060"
down_revision = "M059"
branch_labels = None
depends_on = None

_OLD = "'CANDIDATE', 'APPROVED', 'RETIRED'"
_NEW = _OLD + ", 'DEFERRED'"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE verification_golden_case
          DROP CONSTRAINT IF EXISTS ck_vgc_status
    """)
    op.execute(f"""
        ALTER TABLE verification_golden_case
          ADD CONSTRAINT ck_vgc_status CHECK (status IN ({_NEW}))
    """)

    op.execute("""
        ALTER TABLE verification_golden_case
          ADD COLUMN IF NOT EXISTS defer_reason TEXT,
          ADD COLUMN IF NOT EXISTS deferred_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS deferred_by UUID REFERENCES app_user(id),
          ADD COLUMN IF NOT EXISTS approved_evidence_json JSONB
    """)

    # 보류에는 사유가 필요하다. 이유 없는 보류가 쌓이면 나중에 왜 미뤘는지
    # 아무도 모르고, 결국 전부 다시 확인하게 된다.
    op.execute("""
        ALTER TABLE verification_golden_case
          ADD CONSTRAINT ck_vgc_deferred_has_reason
          CHECK (status <> 'DEFERRED' OR (
            defer_reason IS NOT NULL AND deferred_at IS NOT NULL
            AND deferred_by IS NOT NULL))
    """)

    # 승인에는 근거 박제가 함께 있어야 한다.
    #
    # M059 의 승인 사례(있다면)를 깨뜨리지 않으려고 `revision >= 2` 부터
    # 강제하지 않는다 — 대신 근거 없이 승인된 것은 화면이 그 사실을 보여준다.
    op.execute("""
        COMMENT ON COLUMN verification_golden_case.approved_evidence_json IS
        '승인 당시의 원천·출처·독립 검산 박제. 읽기 전용 — 화면이 여기서 금액을 가져다 쓰지 않는다(M060).'
    """)
    op.execute("""
        COMMENT ON COLUMN verification_golden_case.defer_reason IS
        '보류 사유. 보류는 계산 오류가 아니라 "지금은 판단할 수 없다" 이다(M060).'
    """)


def downgrade() -> None:
    # 보류 사례가 있으면 되돌리지 않는다. 상태를 조용히 CANDIDATE 로 바꾸면
    # 사용자가 이미 본 것을 안 본 것으로 위조하는 셈이다.
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM verification_golden_case
             WHERE status = 'DEFERRED' AND deleted_at IS NULL;
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '[M060_DEFERRED_CASES_EXIST] 보류된 기준 사례가 % 건 있습니다. '
                    '되돌리면 사용자가 이미 검토한 기록이 사라집니다.', remaining;
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE verification_golden_case
          DROP CONSTRAINT IF EXISTS ck_vgc_deferred_has_reason
    """)
    op.execute("""
        ALTER TABLE verification_golden_case
          DROP COLUMN IF EXISTS defer_reason,
          DROP COLUMN IF EXISTS deferred_at,
          DROP COLUMN IF EXISTS deferred_by,
          DROP COLUMN IF EXISTS approved_evidence_json
    """)
    op.execute("""
        ALTER TABLE verification_golden_case
          DROP CONSTRAINT IF EXISTS ck_vgc_status
    """)
    op.execute(f"""
        ALTER TABLE verification_golden_case
          ADD CONSTRAINT ck_vgc_status CHECK (status IN ({_OLD}))
    """)
