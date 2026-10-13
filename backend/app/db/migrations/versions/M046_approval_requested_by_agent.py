"""M046 — 승인 요청자에 서비스 계정을 허용한다.

무엇이 막혔나
-----------
AI 에이전트가 원가 변경을 **제안**하려면 승인 요청을 만들어야 한다. 그런데
`approval_request.requested_by` 는 `app_user` FK 이고 NOT NULL 이다. 서비스
계정은 사람이 아니라 넣을 수 없었다.

테스트가 이걸 잡았다 — `actor_user_id=None` 으로 요청을 만들려다 DataError 가
났다.

왜 사람 계정을 빌려 쓰지 않나
------------------------
서비스 계정을 만든 관리자를 요청자로 넣으면 승인함에 **"김대표가 요청함"** 으로
뜬다. 실제로는 에이전트가 만든 것이고, 승인자는 누가 왜 올렸는지를 보고
판단한다. 거짓 이름이 뜨면 그 판단이 틀린 전제 위에 선다.

기존 패턴을 따른다
---------------
`applied_by_type` 이 이미 같은 문제를 푼 적이 있다(M019) — "사람이 눌렀는지
실행기가 했는지". 요청 쪽에도 같은 구조를 쓴다.

```
requested_by_type            USER / SERVICE_ACCOUNT
requested_by                 사람이면 여기
requested_by_service_account 에이전트면 여기
```

`requested_by` 를 NULL 허용으로 바꾼다. 대신 **둘 중 하나는 반드시 있어야
한다** — CHECK 로 강제한다. 아무도 요청하지 않은 승인 요청은 있을 수 없다.

기존 행
------
전부 사람이 만든 것이다. `requested_by_type` 을 `'USER'` 로 채운다 — 이 컬럼은
CHECK 에 쓰이므로 NULL 로 두면 기존 행이 제약을 위반한다.
"""

from alembic import op

revision = "M046"
down_revision = "M045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE approval_request
            ADD COLUMN requested_by_type TEXT,
            ADD COLUMN requested_by_service_account_id UUID
                REFERENCES service_account(id)
    """)
    # 기존 행은 전부 사람이 만든 것이다. 이 컬럼은 아래 CHECK 에 쓰이므로
    # NULL 로 두면 기존 행이 제약을 위반한다 — 그래서 backfill 한다.
    op.execute("""
        UPDATE approval_request SET requested_by_type = 'USER'
         WHERE requested_by_type IS NULL
    """)
    op.execute("""
        ALTER TABLE approval_request
            ALTER COLUMN requested_by_type SET DEFAULT 'USER',
            ALTER COLUMN requested_by_type SET NOT NULL,
            ALTER COLUMN requested_by DROP NOT NULL
    """)
    # 아무도 요청하지 않은 승인 요청은 있을 수 없다.
    op.execute("""
        ALTER TABLE approval_request
            ADD CONSTRAINT ck_approval_requested_by_type
            CHECK (requested_by_type IN ('USER', 'SERVICE_ACCOUNT')),
            ADD CONSTRAINT ck_approval_requester_present
            CHECK (
              (requested_by_type = 'USER' AND requested_by IS NOT NULL)
              OR (requested_by_type = 'SERVICE_ACCOUNT'
                  AND requested_by_service_account_id IS NOT NULL)
            )
    """)
    op.execute("""
        COMMENT ON COLUMN approval_request.requested_by_service_account_id IS
        '에이전트가 올린 제안. 승인자가 "누가 왜 올렸나" 를 정확히 봐야 한다.'
    """)


def downgrade() -> None:
    # 서비스 계정이 올린 요청은 사람 요청자로 되돌릴 수 없다. 되돌리기 전에
    # 지워야 `requested_by` 를 NOT NULL 로 되돌릴 수 있다.
    op.execute("""
        DELETE FROM approval_request WHERE requested_by IS NULL
    """)
    op.execute("""
        ALTER TABLE approval_request
            DROP CONSTRAINT ck_approval_requester_present,
            DROP CONSTRAINT ck_approval_requested_by_type,
            ALTER COLUMN requested_by SET NOT NULL,
            DROP COLUMN requested_by_service_account_id,
            DROP COLUMN requested_by_type
    """)
