"""M045 — 자동화 정책 · 실행 · 액션.

먼저 기존 구조를 읽었다
--------------------
새 테이블을 만들기 전에 재사용 가능성을 확인했다.

```
worker_run          "언제 돌았고 몇 건 처리했나"        → 재사용한다
operation_issue     "사람이 봐야 할 것"                → 재사용한다
approval_request    "승인이 필요한 변경"                → 재사용한다
notification        "알린다"                          → 재사용한다
```

없는 것은 하나다: **"무엇을 자동으로 할지, 어디까지 허용할지" 를 조직이 정한
기록.** worker 는 코드에 고정된 목록(`WORKER_NAMES`)이고 사용자가 켜고 끌 수
없다. 자동 실행 한도도 담을 곳이 없다.

그래서 이 마이그레이션이 만드는 것은 **정책**이다. 실행 기록은 worker 를 그대로
쓰고, 결과로 생기는 것도 기존 승인·이슈·알림을 쓴다.

```
automation_policy   무엇을 · 어떤 모드로 · 어디까지    ← 새로 만든다
automation_run      한 번의 판단 결과                  ← 새로 만든다 (worker_run 과 다르다)
automation_action   그 판단이 만든 개별 행동            ← 새로 만든다
```

`automation_run` 을 `worker_run` 과 합치지 않은 이유
------------------------------------------------
`worker_run` 은 **프로세스 한 번**이다. 자동화 worker 한 번이 정책 다섯 개를
평가하면, 그 다섯 개의 결과가 하나의 run 에 뭉개진다. "마진 감시는 3건을
제안했고 원가 감시는 실패했다" 를 구분할 수 없게 된다.

`automation_action` 을 따로 두는 이유
----------------------------------
"제안 3건" 이라는 숫자만으로는 무엇을 제안했는지, 그 제안이 승인됐는지 알 수
없다. 행동 하나하나가 결과(승인 요청 id, 이슈 id)와 이어져야 한다.

모드
----
```
REPORT_ONLY    관측만 한다. 아무것도 쓰지 않는다
PROPOSE        승인 요청을 만든다. 사람이 승인해야 반영된다
AUTO_EXECUTE   허용 범위 안에서 직접 실행한다
```

기본은 `REPORT_ONLY` 다. 자동 실행을 기본값으로 두지 않는다 — 켜는 것은 사람의
결정이어야 한다.

한도(`guardrail_json`)
--------------------
generic JSON 잡탕을 만들지 않았다. **실제로 쓰는 키만** 서비스가 읽고, 모르는
키가 있으면 정책 검증이 거부한다(`automation.validate_guardrails`). 검증하지
않는 설정 칸은 있으나 마나다 — 오타 하나로 한도가 조용히 사라진다.
"""

from alembic import op

revision = "M045"
down_revision = "M044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE automation_policy (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          automation_type TEXT NOT NULL,
          name TEXT NOT NULL,

          enabled BOOLEAN NOT NULL DEFAULT FALSE,
          -- 기본은 관측만. 자동 실행을 기본값으로 두지 않는다.
          mode TEXT NOT NULL DEFAULT 'REPORT_ONLY',

          -- 얼마나 자주. 실제 외부 API rate limit 을 모르는 상태라 기본값을
          -- 지어내지 않고 설정 가능하게 둔다.
          interval_minutes INTEGER,

          -- AUTO_EXECUTE 허용 범위. 서비스가 아는 키만 들어간다.
          guardrail_json JSONB NOT NULL DEFAULT '{}'::JSONB,

          -- 대상 좁히기(특정 판매처 계정 등). NULL = 전체.
          scope_json JSONB NOT NULL DEFAULT '{}'::JSONB,

          last_run_at TIMESTAMPTZ,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_ap_mode CHECK (mode IN
            ('REPORT_ONLY', 'PROPOSE', 'AUTO_EXECUTE')),
          CONSTRAINT ck_ap_interval CHECK
            (interval_minutes IS NULL OR interval_minutes >= 5)
        )
    """)
    # 한 조직에 같은 자동화가 둘 있으면 어느 쪽 한도가 적용되는지 알 수 없다.
    op.execute("""
        CREATE UNIQUE INDEX uq_automation_policy_type
            ON automation_policy (org_id, automation_type) WHERE deleted_at IS NULL
    """)

    op.execute("""
        CREATE TABLE automation_run (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          policy_id UUID NOT NULL REFERENCES automation_policy(id),

          -- 판단 시점의 모드. 정책이 나중에 바뀌어도 그때 무엇이었는지 남는다.
          mode TEXT NOT NULL,
          trigger TEXT NOT NULL DEFAULT 'SCHEDULED',

          status TEXT NOT NULL DEFAULT 'RUNNING',

          -- 무엇을 보고 몇 건을 골랐나. NULL = 아직 안 끝났다(0 과 다르다).
          evaluated_count INTEGER,
          action_count    INTEGER,
          skipped_count   INTEGER,

          started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ,
          error_code    TEXT,
          error_message TEXT,

          -- 이 실행을 감싼 worker 실행. 둘을 잇지 않으면 "그날 왜 안 돌았나" 를
          -- 물을 때 어디를 봐야 하는지 알 수 없다.
          worker_run_id UUID REFERENCES worker_run(id),

          CONSTRAINT ck_ar_status CHECK (status IN
            ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'SKIPPED'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_automation_run_policy
            ON automation_run (policy_id, started_at DESC)
    """)

    op.execute("""
        CREATE TABLE automation_action (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          run_id UUID NOT NULL REFERENCES automation_run(id),

          action_type TEXT NOT NULL,

          -- 무엇에 대한 행동인가. FK 를 걸지 않는다 — 대상 종류가 여럿이다.
          target_table TEXT,
          target_id    UUID,

          -- REPORTED  관측만 했다
          -- PROPOSED  승인 요청을 만들었다
          -- EXECUTED  직접 실행했다
          -- BLOCKED   한도에 걸려 하지 않았다  ← 이게 남아야 한도가 검증된다
          -- FAILED    하려다 실패했다
          outcome TEXT NOT NULL,

          -- 왜 그렇게 판단했나. 사람이 읽는 한 줄.
          reason TEXT,
          -- 한도에 걸렸다면 어느 한도인지.
          blocked_by TEXT,

          -- 결과로 만들어진 것.
          approval_request_id UUID REFERENCES approval_request(id),
          operation_issue_id  UUID REFERENCES operation_issue(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          CONSTRAINT ck_aa_outcome CHECK (outcome IN
            ('REPORTED', 'PROPOSED', 'EXECUTED', 'BLOCKED', 'FAILED'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_automation_action_run ON automation_action (run_id)
    """)
    op.execute("""
        COMMENT ON COLUMN automation_action.outcome IS
        'BLOCKED 가 남아야 한도가 실제로 동작했는지 확인할 수 있다. '
        '한도에 걸린 행동을 기록하지 않으면 "아무 일도 없었다" 와 구분되지 않는다.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE automation_action")
    op.execute("DROP TABLE automation_run")
    op.execute("DROP TABLE automation_policy")
