"""M049 — 발주를 만든 것이 사람이 아닐 수 있다.

M048 은 `created_by` (사람) 와 `automation_run_id` (자동화 실행) 둘만 알았다.
MCP 로 AI 에이전트가 발주 초안을 제안하게 되면서 세 번째 주체가 생긴다.

```
created_by                  사람이 화면에서
automation_run_id           자동화 실행이
created_by_service_account  AI·외부 에이전트가   ← 이번에 추가
```

왜 `created_by` 에 에이전트 계정을 넣지 않나
--------------------------------------
`created_by` 는 `app_user` 를 가리킨다. 거기에 에이전트를 넣으려면 에이전트마다
사람 계정을 만들어야 하고, 그러면 감사 로그에서 **사람이 한 일과 기계가 한 일이
구분되지 않는다.** "누가 이걸 주문했나" 에 답할 수 없게 된다.

승인 요청이 M046 에서 같은 문제를 같은 방식으로 풀었다(`requested_by_type` +
`requested_by_service_account_id`). 여기서도 계정 종류를 섞지 않는다.

셋 중 하나는 반드시 있어야 한다
--------------------------
CHECK 로 못 박는다. 주체를 모르는 발주가 생기면 나중에 되짚을 방법이 없다.
"""

from alembic import op

revision = "M049"
down_revision = "M048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE purchase_order
          ADD COLUMN created_by_service_account UUID REFERENCES service_account(id)
    """)
    op.execute("""
        COMMENT ON COLUMN purchase_order.created_by_service_account IS
        'AI·외부 에이전트가 만든 초안. created_by(사람)와 섞지 않는다 — '
        '섞으면 감사 로그에서 사람과 기계를 구분할 수 없다.'
    """)
    # 주체를 모르는 발주를 만들 수 없게 한다.
    op.execute("""
        ALTER TABLE purchase_order
          ADD CONSTRAINT ck_po_has_actor CHECK (
            created_by IS NOT NULL
            OR automation_run_id IS NOT NULL
            OR created_by_service_account IS NOT NULL
          )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE purchase_order DROP CONSTRAINT ck_po_has_actor")
    op.execute("""
        ALTER TABLE purchase_order DROP COLUMN created_by_service_account
    """)
