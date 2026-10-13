"""M043 — 공급처 Excel import 실행 기록.

왜 별도 테이블인가
---------------
`audit_log` 는 **행 하나의 변화**를 남긴다(`domain_row_id` 가 필수다). import 는
54건을 한 번에 건드리는 하나의 사건이고, "그 파일을 언제 누가 어떤 환경에서
돌렸나" 는 개별 공급처 행에 붙지 않는다.

행별 변화는 여전히 `audit_log` 에 남는다. 여기는 그 위의 한 겹이다.

파일 해시를 남기는 이유
-------------------
"지난주에 돌린 그 파일" 이 지금 손에 있는 파일과 같은지 확인할 방법이 필요하다.
파일명은 바뀌고, Excel 은 열기만 해도 수정 시각이 바뀐다. 내용 해시만이
안정적이다.

환경을 남기는 이유
---------------
개발용 CLI 가 운영 DB 를 건드린 적이 있는지 나중에 물을 수 있어야 한다.
막는 것은 코드가 하지만(`APP_ENV` 가드), 기록은 남겨 둔다.

⚠️ 이 파일에는 평문 ID/PW 가 있다
------------------------------
그래서 `credential_skipped` 컬럼이 있다. **버린 개수**를 세는 것이지 값을
담는 것이 아니다. 값은 파서에서 이미 버려져 여기까지 오지 않는다.
"""

from alembic import op

revision = "M043"
down_revision = "M042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE supplier_import_run (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          -- 무엇을 돌렸나
          source_name TEXT NOT NULL,
          -- 내용 SHA-256. 파일명·수정시각은 못 믿는다.
          source_sha256 TEXT NOT NULL,
          sheet_name  TEXT,

          -- ANALYZE(읽기만) / DRY_RUN(계산만) / APPLY(실제 반영)
          mode TEXT NOT NULL,
          -- 어느 환경에서 돌았나. 개발용 CLI 가 어디를 건드렸는지 남는다.
          app_env TEXT NOT NULL,

          created  INTEGER NOT NULL DEFAULT 0,
          updated  INTEGER NOT NULL DEFAULT 0,
          unchanged INTEGER NOT NULL DEFAULT 0,
          conflicts INTEGER NOT NULL DEFAULT 0,
          skipped   INTEGER NOT NULL DEFAULT 0,
          -- 버린 자격증명 **열**의 개수. 값이 아니다.
          credential_skipped INTEGER NOT NULL DEFAULT 0,

          started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ,
          actor_user_id UUID REFERENCES app_user(id),

          CONSTRAINT ck_sir_mode CHECK (mode IN ('ANALYZE', 'DRY_RUN', 'APPLY'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_supplier_import_run_org
            ON supplier_import_run (org_id, started_at DESC)
    """)
    op.execute("""
        COMMENT ON COLUMN supplier_import_run.credential_skipped IS
        '버린 자격증명 열의 개수. 값은 파서에서 이미 폐기돼 여기까지 오지 않는다.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE supplier_import_run")
