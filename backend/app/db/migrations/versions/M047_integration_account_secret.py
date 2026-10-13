"""M047 — 판매처 계정에 복원 가능한 자격증명 참조를 붙인다.

왜 `external_account` 를 쓰지 않나
------------------------------
둘은 다른 것이다(docs/58 에서 나눈 이유 그대로다).

```
integration_account   쿠팡·네이버·11번가 **판매자 계정**. 주문이 여기 귀속된다
external_account      셀러툴처럼 우리가 나가서 호출하는 외부 시스템
```

네이버 스마트스토어 자격증명을 `external_account` 에 넣으면 "주문이 셀러툴
계정 소속" 같은 상태가 가능해진다. 그래서 판매처 자격증명은 판매처 계정에
붙인다.

기존 컬럼과 무엇이 다른가
--------------------
`integration_account` 에는 이미 두 가지가 있다.

```
credentials_vault_entry_id   사람이 찾아갈 쪽지 (값 없음, 복호화 불가)
api_key_hash                 **들어오는** 봇 요청을 검증하는 해시 (복원 불가)
```

둘 다 **우리가 나가서 호출할 때 쓸 수 없다.** 쪽지에는 값이 없고, 해시는
되돌릴 수 없다. 무인 주문 수집에는 복원 가능한 자격증명이 필요하다 — 그건
`SecretStore` 가 암호화해서 갖는다(M042).

그래서 M042 에서 `external_account` 에 붙인 것과 **같은 종류의 참조**를
여기에도 붙인다. 값도 암호문도 이 테이블에 들어오지 않는다.

`api_key_hash` 와 헷갈리지 않게
---------------------------
```
api_key_hash            들어오는 요청 검증   해시     (봇 → 우리)
credential_secret_ref   나가는 요청 인증     암호문   (우리 → 판매처)
```
방향이 반대다. 컬럼 주석에 남긴다.
"""

from alembic import op

revision = "M047"
down_revision = "M046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # M042 는 소유자 종류를 `external_account` 하나로 좁혀 뒀다. 판매처 계정도
    # 자격증명을 갖게 되므로 허용 목록을 넓힌다 — 제약을 지우지 않고 **넓힌다.**
    # 지우면 다음에 오타 난 owner_type 이 조용히 들어온다.
    op.execute("""
        ALTER TABLE credential_secret
            DROP CONSTRAINT ck_cs_owner_type,
            ADD CONSTRAINT ck_cs_owner_type CHECK (owner_type IN
                ('external_account', 'integration_account'))
    """)
    op.execute("""
        ALTER TABLE integration_account
            ADD COLUMN credential_secret_ref TEXT,
            ADD COLUMN credential_kind TEXT,
            ADD COLUMN credential_updated_at TIMESTAMPTZ,
            ADD COLUMN credential_updated_by UUID REFERENCES app_user(id)
    """)
    op.execute("""
        ALTER TABLE integration_account
            ADD CONSTRAINT ck_ia_credential_kind
            CHECK (credential_kind IS NULL OR credential_kind IN
                   ('API_KEY', 'ACCESS_SECRET', 'CLIENT_CREDENTIALS',
                    'OAUTH_REFRESH', 'BASIC', 'CUSTOM'))
    """)
    op.execute("""
        COMMENT ON COLUMN integration_account.credential_secret_ref IS
        'SecretStore 로의 불투명 참조 — **우리가 판매처를 호출할 때** 쓴다. '
        'api_key_hash 는 반대 방향(봇이 우리를 호출)이라 서로 대체할 수 없다. '
        'API 응답에 싣지 않는다.'
    """)


def downgrade() -> None:
    # 판매처 자격증명을 먼저 정리해야 제약을 되돌릴 수 있다. 암호문은 키 없이는
    # 쓸모가 없지만, 남겨 두면 제약 위반으로 downgrade 가 막힌다.
    op.execute("""
        DELETE FROM credential_secret WHERE owner_type = 'integration_account'
    """)
    op.execute("""
        ALTER TABLE credential_secret
            DROP CONSTRAINT ck_cs_owner_type,
            ADD CONSTRAINT ck_cs_owner_type CHECK (owner_type IN
                ('external_account'))
    """)
    op.execute("""
        ALTER TABLE integration_account
            DROP CONSTRAINT ck_ia_credential_kind,
            DROP COLUMN credential_updated_by,
            DROP COLUMN credential_updated_at,
            DROP COLUMN credential_kind,
            DROP COLUMN credential_secret_ref
    """)
