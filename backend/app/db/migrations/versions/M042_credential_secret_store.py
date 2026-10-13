"""M042 — 복원 가능한 자격증명 저장소 (암호문만 DB 에).

무엇이 바뀌나
-----------
지금까지 `external_account.credentials_vault_entry_id` 는 **사람이 읽는 쪽지**를
가리켰다(`password_vault_entry` = "Bitwarden 의 어떤 항목"). 사람은 그걸 보고
찾아갈 수 있지만 새벽 3시에 도는 배치는 못 한다.

무인 동기화를 하려면 프로그램이 인증정보를 **다시 쓸 수 있어야** 한다. 그래서
암호문을 담는 테이블을 새로 만든다.

```
password_vault_entry   사람이 찾아가는 위치   값 없음      (그대로 둔다)
credential_secret      기계가 쓰는 인증정보   암호문 보관   (여기서 추가)
```

기존 컬럼을 지우지 않는다
----------------------
`credentials_vault_entry_id` 는 남는다. "이 계정의 비밀번호는 Bitwarden 어디에
있다" 는 정보는 여전히 사람에게 쓸모가 있고, 담당자가 바뀔 때 그 쪽지가 유일한
단서인 경우가 많다. 둘은 대체 관계가 아니다.

DB 만으로는 못 푼다
----------------
```
credential_secret.ciphertext   DB 에 있다
마스터 키                       환경변수에 있다 (CREDENTIAL_MASTER_KEY)
```

DB 덤프만 유출돼도 복호화되지 않는다. **백업 번들에 DB 와 키를 같이 넣으면
이 분리가 무의미해진다** — docs/60 백업 절차에 적었다.

nonce 컬럼을 따로 두지 않는 이유
----------------------------
Fernet 토큰은 버전·타임스탬프·IV·암호문·HMAC 을 하나의 바이트열에 담는다.
IV 를 별도 컬럼으로 빼면 둘이 어긋날 수 있는 상태를 우리가 새로 만드는 것이다.

`key_fingerprint` 는 키가 아니다
-----------------------------
SHA-256 앞 16자다. 32바이트 난수 키의 해시 앞부분으로 키를 되찾을 수 없다.
"옛 키로 암호화된 행이 아직 남았는가" 를 **복호화 없이** 세기 위한 것이다.
"""

from alembic import op

revision = "M042"
down_revision = "M041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE credential_secret (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          -- 불투명 참조. 소유자 id 를 담지 않는다 — 참조가 새도 그 자체로는
          -- 아무것도 알려 주지 않아야 한다.
          secret_ref TEXT NOT NULL,

          -- 누구 것인가. FK 를 걸지 않는다 — 소유자 종류가 늘어난다
          -- (external_account, 나중에 판매처 계정·발송 대행 등).
          -- 고아 행은 데이터 품질 점검이 잡는다.
          owner_type TEXT NOT NULL,
          owner_id   UUID NOT NULL,

          provider        TEXT NOT NULL,
          credential_kind TEXT NOT NULL,

          -- 어느 마스터 키로 암호화했는지. **키가 아니라 지문이다.**
          key_fingerprint TEXT NOT NULL,

          -- Fernet 토큰 전체. 평문은 어떤 컬럼에도 없다.
          ciphertext BYTEA NOT NULL,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          rotated_at TIMESTAMPTZ,
          deleted_at TIMESTAMPTZ,

          CONSTRAINT ck_cs_kind CHECK (credential_kind IN
            ('API_KEY', 'ACCESS_SECRET', 'CLIENT_CREDENTIALS',
             'OAUTH_REFRESH', 'BASIC', 'CUSTOM')),
          CONSTRAINT ck_cs_owner_type CHECK (owner_type IN ('external_account'))
        )
    """)
    # 참조는 조직 안에서 유일하다. 조회는 항상 org 와 함께 한다 —
    # 참조만으로 남의 조직 비밀을 꺼낼 수 없어야 한다.
    op.execute("""
        CREATE UNIQUE INDEX uq_credential_secret_ref
            ON credential_secret (org_id, secret_ref) WHERE deleted_at IS NULL
    """)
    # 한 소유자에 살아 있는 비밀은 하나다. 두 개가 되면 어느 것으로 접속하는지
    # 아무도 모르게 된다.
    op.execute("""
        CREATE UNIQUE INDEX uq_credential_secret_owner
            ON credential_secret (org_id, owner_type, owner_id)
         WHERE deleted_at IS NULL
    """)
    # "옛 키로 암호화된 행이 얼마나 남았나" 를 세는 데 쓴다.
    op.execute("""
        CREATE INDEX ix_credential_secret_key
            ON credential_secret (key_fingerprint) WHERE deleted_at IS NULL
    """)

    op.execute("""
        COMMENT ON TABLE credential_secret IS
        '무인 동기화용 자격증명 암호문. 마스터 키는 DB 에 없다(환경변수). '
        'DB 백업과 마스터 키를 같은 번들에 두면 분리가 무의미해진다 — docs/60.'
    """)

    # 외부 계정이 가리키는 **불투명 참조.** vault 쪽지와 다른 것이라
    # 컬럼을 따로 둔다. API 응답에 절대 싣지 않는다.
    op.execute("""
        ALTER TABLE external_account
            ADD COLUMN credential_secret_ref TEXT
    """)
    op.execute("""
        COMMENT ON COLUMN external_account.credential_secret_ref IS
        'credential_secret.secret_ref 로의 불투명 참조. API 응답 금지. '
        'credentials_vault_entry_id(사람용 쪽지)와 다른 것이다.'
    """)

    # `credential_kind` 는 이제 SecretStore 의 형태 목록을 따른다.
    # M040 의 `OAUTH_CLIENT` 는 `CLIENT_CREDENTIALS` 로 이름이 정리됐고,
    # refresh token 을 쓰는 `OAUTH_REFRESH` 가 별도로 생겼다 — 무인 갱신이
    # 가능한지가 갈리므로 같은 값으로 둘 수 없다.
    op.execute("""
        ALTER TABLE external_account
            DROP CONSTRAINT ck_external_credential_kind,
            ADD CONSTRAINT ck_external_credential_kind
            CHECK (credential_kind IS NULL OR credential_kind IN
                   ('API_KEY', 'ACCESS_SECRET', 'CLIENT_CREDENTIALS',
                    'OAUTH_REFRESH', 'BASIC', 'CUSTOM'))
    """)

    # 새 시험 결과: 참조는 있는데 비밀을 못 읽는 상태.
    # "미설정" 으로 뭉개면 사용자가 다시 설정하고 원인은 그대로 남는다.
    op.execute("""
        ALTER TABLE external_account
            DROP CONSTRAINT ck_external_test_status,
            ADD CONSTRAINT ck_external_test_status
            CHECK (last_test_status IS NULL OR last_test_status IN
                   ('OK', 'CONNECTOR_UNAVAILABLE', 'CREDENTIAL_NOT_CONFIGURED',
                    'CREDENTIAL_BROKEN', 'AUTHENTICATION_FAILED',
                    'CONNECTION_FAILED', 'RATE_LIMITED'))
    """)


def downgrade() -> None:
    # 되돌리면 M040 의 값 목록으로 돌아간다. 새 값을 쓰던 행이 있으면
    # 제약이 걸리므로 먼저 비운다 — 시험 결과와 형태는 다시 만들 수 있는
    # 관측값이지 원장이 아니다.
    op.execute("""
        UPDATE external_account SET last_test_status = NULL
         WHERE last_test_status = 'CREDENTIAL_BROKEN'
    """)
    op.execute("""
        UPDATE external_account SET credential_kind = NULL
         WHERE credential_kind IN ('CLIENT_CREDENTIALS', 'OAUTH_REFRESH')
    """)
    op.execute("""
        ALTER TABLE external_account
            DROP CONSTRAINT ck_external_test_status,
            ADD CONSTRAINT ck_external_test_status
            CHECK (last_test_status IS NULL OR last_test_status IN
                   ('OK', 'CONNECTOR_UNAVAILABLE', 'CREDENTIAL_NOT_CONFIGURED',
                    'AUTHENTICATION_FAILED', 'CONNECTION_FAILED', 'RATE_LIMITED'))
    """)
    op.execute("""
        ALTER TABLE external_account
            DROP CONSTRAINT ck_external_credential_kind,
            ADD CONSTRAINT ck_external_credential_kind
            CHECK (credential_kind IS NULL OR credential_kind IN
                   ('API_KEY', 'ACCESS_SECRET', 'OAUTH_CLIENT', 'BASIC', 'CUSTOM'))
    """)
    op.execute("ALTER TABLE external_account DROP COLUMN credential_secret_ref")
    op.execute("DROP TABLE credential_secret")
