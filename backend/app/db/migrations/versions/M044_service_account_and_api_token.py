"""M044 — 서비스 계정 · API 토큰 · 감사 주체 확장.

왜 사용자 토큰을 재사용하지 않나
----------------------------
AI 에이전트에게 사람의 JWT 를 주면 세 가지가 한꺼번에 무너진다.

```
감사      "김대표가 원가를 바꿨다" 로 남는다. 실제로는 에이전트가 했다
권한      그 사람의 모든 권한을 갖는다. 필요한 건 상품 조회 하나인데
수명      사람 세션은 12시간이다. 무인 실행은 그때마다 멈춘다
```

그래서 **다른 종류의 주체**를 만든다. 사람이 아니고, 로그인하지 않고,
자기 이름으로 감사에 남고, 필요한 것만 할 수 있다.

토큰은 비밀번호와 같은 모델이다
--------------------------
```
외부로 나가는 자격증명   SecretStore   복호화 가능해야 한다 (우리가 써야 하니까)
들어오는 API 토큰        해시          복호화할 이유가 없다 (검증만 하면 된다)
```

**API 토큰을 SecretStore 에 넣지 않는다.** 복원 가능하게 저장할 이유가 없고,
할 수 있게 만들면 언젠가 누군가 조회 화면을 만든다.

해시 선택 근거 (SHA-256)
-----------------------
비밀번호에는 argon2 를 쓴다(`core/security.py`). 여기는 **SHA-256** 이다.
이유는 위협 모델이 다르기 때문이다.

```
사람 비밀번호   엔트로피 30~40비트   사전·규칙 공격이 통한다
                                  → 추측 1회 비용을 올려야 한다 (argon2)

기계 토큰       엔트로피 256비트     사전이 없다. 규칙도 없다
                                  → 2^256 앞에서는 해시 속도가 무의미하다
```

토큰은 `secrets.token_urlsafe(32)` = 32바이트 난수다. DB 가 통째로 유출돼도
SHA-256 을 되돌릴 방법은 전수 탐색뿐이고, 그건 해시가 얼마나 느리든 불가능하다.

반대로 argon2 를 쓰면 **모든 API 호출과 MCP tool 호출마다 100~300ms** 가 붙는다.
얻는 것 없이 치르는 비용이다. 이 판단은 GitHub PAT·Stripe 등이 기계 토큰에
빠른 해시를 쓰는 것과 같은 근거다.

이 근거가 성립하려면 토큰이 **반드시 우리가 생성한 고엔트로피 난수**여야 한다.
사용자가 토큰 문자열을 정하게 하면 근거가 무너진다 — 그래서 그런 API 를 만들지
않았다.

`token_prefix` 는 인증에 쓰지 않는다
--------------------------------
행을 찾는 데만 쓴다. 접두사가 맞다고 인증되면 접두사만 맞히면 되는 셈이다.
실제 판정은 전체 토큰 해시의 **상수 시간 비교**로 한다.
"""

from alembic import op

revision = "M044"
down_revision = "M043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 서비스 계정 ────────────────────────────────────────────
    op.execute("""
        CREATE TABLE service_account (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          name        TEXT NOT NULL,
          description TEXT,

          -- 사람과 같은 role 을 주지 않는다. 할 수 있는 일은 토큰의 scope 가 정한다.
          -- role 을 주면 "ADMIN 서비스 계정" 이 생기고, 그건 사람 계정과 같아진다.
          status TEXT NOT NULL DEFAULT 'ACTIVE',

          last_used_at TIMESTAMPTZ,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_sa_status CHECK (status IN ('ACTIVE', 'DISABLED'))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_service_account_name
            ON service_account (org_id, name) WHERE deleted_at IS NULL
    """)

    # ── API 토큰 ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE api_token (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          service_account_id UUID NOT NULL REFERENCES service_account(id),

          -- 사람이 "어떤 토큰인지" 알아보는 용도. **인증에 쓰지 않는다.**
          token_prefix TEXT NOT NULL,
          -- SHA-256(token). 원문은 어디에도 없다 — 발급 순간 한 번 보여줄 뿐이다.
          token_hash   TEXT NOT NULL,

          label TEXT,

          -- 이 토큰이 할 수 있는 일. 계정이 아니라 **토큰**에 붙인다 —
          -- 같은 에이전트라도 읽기용과 제안용 토큰을 나눌 수 있어야 한다.
          scopes TEXT[] NOT NULL DEFAULT '{}',

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          -- 영구 토큰을 강요하지 않는다. NULL = 만료 없음.
          expires_at   TIMESTAMPTZ,
          last_used_at TIMESTAMPTZ,
          revoked_at   TIMESTAMPTZ,
          revoked_by   UUID REFERENCES app_user(id)
        )
    """)
    # 해시로 찾는다. 접두사는 표시용이라 유일하지 않아도 된다.
    op.execute("""
        CREATE UNIQUE INDEX uq_api_token_hash ON api_token (token_hash)
    """)
    op.execute("""
        CREATE INDEX ix_api_token_account
            ON api_token (service_account_id) WHERE revoked_at IS NULL
    """)
    op.execute("""
        COMMENT ON COLUMN api_token.token_hash IS
        'SHA-256(token). 토큰은 우리가 만든 256비트 난수라 사전 공격이 성립하지 '
        '않는다 — 근거는 이 마이그레이션 docstring 과 docs/61.';
    """)

    # ── 감사 주체 ─────────────────────────────────────────────
    #
    # 지금 `audit_log.actor_user_id` 는 app_user FK 하나뿐이다. 서비스 계정이
    # 한 일을 남기려면 사람 계정을 하나 만들어 주는 방법이 있지만, 그러면
    # 그 계정에 role 이 생기고 사람과 구분되지 않는다.
    #
    # 대신 주체 **종류**를 명시한다. 기존 행은 전부 USER 다 — backfill 하지 않고
    # NULL 을 USER 로 읽는다(아래 COMMENT). 수백만 행을 UPDATE 하지 않기 위해서다.
    op.execute("""
        ALTER TABLE audit_log
            ADD COLUMN actor_type TEXT,
            ADD COLUMN actor_service_account_id UUID REFERENCES service_account(id),
            -- 에이전트 행동이 특정 사용자 요청에서 시작됐다면 그 사람을 남긴다.
            -- "누가 시켰나" 를 모르면 자동화 사고의 책임 소재를 못 찾는다.
            ADD COLUMN initiated_by_user_id UUID REFERENCES app_user(id)
    """)
    op.execute("""
        ALTER TABLE audit_log
            ADD CONSTRAINT ck_audit_actor_type
            CHECK (actor_type IS NULL OR actor_type IN
                   ('USER', 'SYSTEM', 'SERVICE_ACCOUNT'))
    """)
    op.execute("""
        COMMENT ON COLUMN audit_log.actor_type IS
        'NULL 은 USER 로 읽는다. 기존 행을 backfill 하지 않기 위한 선택이다.'
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE audit_log
            DROP CONSTRAINT ck_audit_actor_type,
            DROP COLUMN initiated_by_user_id,
            DROP COLUMN actor_service_account_id,
            DROP COLUMN actor_type
    """)
    op.execute("DROP TABLE api_token")
    op.execute("DROP TABLE service_account")
