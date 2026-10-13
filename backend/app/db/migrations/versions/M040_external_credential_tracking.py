"""M040 — 외부 계정 자격증명 **참조** 추적.

이 프로젝트는 비밀을 저장하지 않는다
--------------------------------
조사 결과 프로젝트 전체에 암호화 저장소가 없다. 의도적이다.

```
inbound  (봇이 우리에게)     argon2 해시      검증만 가능, 복원 불가
outbound (우리가 외부에게)   vault 참조만     `password_vault_entry`
                             vault_provider / vault_item_name / account_masked
```

CLAUDE.md: "프로그램에는 실제 비밀번호를 저장하지 않고 **보관 위치만** 저장한다
(예: 'Bitwarden - 쿠팡 Wing')".

그래서 여기서도 **암호화 저장소를 새로 만들지 않는다.** 이 마이그레이션이
추가하는 것은 "자격증명이 설정됐다는 사실과 그 시점" 뿐이다.

⚠️ 남은 결정 (docs/59 §3)
------------------------
셀러툴을 자동 동기화하려면 호출 시점에 **복원 가능한** 비밀이 필요하다.
해시로는 안 되고, Bitwarden 항목 이름으로도 무인 실행은 안 된다.

선택지는 (a) 암호화 저장소 도입 + 키 관리, (b) 실행할 때 사람이 비밀을 넣는
반자동 방식이다. **보안 태세를 바꾸는 결정이라 임의로 고르지 않았다.**
지금은 실제 커넥터가 없어 아무것도 막히지 않는다.
"""

from alembic import op

revision = "M040"
down_revision = "M039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE external_account
            ADD COLUMN credential_updated_at TIMESTAMPTZ,
            ADD COLUMN credential_updated_by UUID REFERENCES app_user(id),
            -- 어떤 방식으로 인증하는가. 값이 아니라 종류다.
            ADD COLUMN credential_kind TEXT,
            -- 마지막 연결 시험 결과. 실패 사유를 상태로 구분한다 —
            -- "연결 실패" 하나로 뭉치면 무엇을 고쳐야 할지 알 수 없다.
            ADD COLUMN last_test_status TEXT,
            ADD COLUMN last_test_at TIMESTAMPTZ,
            ADD COLUMN last_test_message TEXT
    """)
    op.execute("""
        ALTER TABLE external_account
            ADD CONSTRAINT ck_external_credential_kind
            CHECK (credential_kind IS NULL OR credential_kind IN
                   ('API_KEY', 'ACCESS_SECRET', 'OAUTH_CLIENT', 'BASIC', 'CUSTOM'))
    """)
    op.execute("""
        ALTER TABLE external_account
            ADD CONSTRAINT ck_external_test_status
            CHECK (last_test_status IS NULL OR last_test_status IN
                   ('OK', 'CONNECTOR_UNAVAILABLE', 'CREDENTIAL_NOT_CONFIGURED',
                    'AUTHENTICATION_FAILED', 'CONNECTION_FAILED', 'RATE_LIMITED'))
    """)
    op.execute("""
        COMMENT ON COLUMN external_account.credential_kind IS
        '인증 방식(값이 아니라 종류). 비밀 자체는 저장하지 않는다 — vault 참조만 둔다.'
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE external_account
            DROP CONSTRAINT IF EXISTS ck_external_test_status,
            DROP CONSTRAINT IF EXISTS ck_external_credential_kind,
            DROP COLUMN IF EXISTS last_test_message,
            DROP COLUMN IF EXISTS last_test_at,
            DROP COLUMN IF EXISTS last_test_status,
            DROP COLUMN IF EXISTS credential_kind,
            DROP COLUMN IF EXISTS credential_updated_by,
            DROP COLUMN IF EXISTS credential_updated_at
    """)
