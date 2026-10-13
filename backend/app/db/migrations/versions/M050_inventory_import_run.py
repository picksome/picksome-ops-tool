"""M050 — 재고 가져오기 실행 기록.

왜 별도 테이블인가
---------------
`supplier_import_run` (M043) 을 재사용할 수도 있었지만, 그 테이블의 컬럼은
공급처 가져오기의 어휘다(`credential_skipped` 등). 재고 가져오기의 결과는
`opening / adjustment / unchanged / unmatched / ambiguous` 로 완전히 다르다.

억지로 한 테이블에 담으면 절반이 항상 NULL 인 컬럼이 생기고, 그 NULL 이
"해당 없음" 인지 "아직 안 셈" 인지 구분되지 않는다.

무엇을 남기나
-----------
```
누가·언제·어떤 파일을      actor / filename / sha256 / started_at
어떤 단계로                 mode (ANALYZE / DRY_RUN / APPLY)
무엇이 일어났나             opening / adjustment / unchanged / unmatched / ...
```

**파일 내용은 저장하지 않는다.** 해시만 남긴다 — 파일명은 바뀌고 Excel 은
열기만 해도 수정 시각이 바뀐다. "지난주에 돌린 그 파일" 을 확인할 방법은
내용 해시뿐이다. 원문을 통째로 저장하면 지워야 할 것이 하나 더 생긴다.

멱등성은 여기가 아니라 원장이 지킨다
--------------------------------
같은 파일을 두 번 적용해도 재고가 두 번 늘면 안 된다. 그 보장은 이 테이블이
아니라 `uq_inv_txn_source` 가 한다 — `inventory_transaction.source_id` 에
(파일해시 + 상품 + 기준일)에서 나온 **결정적인** 값을 넣는다.

실행 기록이 아니라 DB 제약이 막는 이유: 실행 기록을 조회해서 판단하면 두
사람이 동시에 올렸을 때 둘 다 "없다" 를 보고 둘 다 넣는다.
"""

from alembic import op

revision = "M050"
down_revision = "M049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE inventory_import_run (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          source_name   TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          mode          TEXT NOT NULL,
          app_env       TEXT NOT NULL,

          -- 실사 기준일. 언제 센 재고인지 모르면 나중에 원장을 못 읽는다.
          as_of_date DATE,

          rows_total  INTEGER NOT NULL DEFAULT 0,
          opening     INTEGER NOT NULL DEFAULT 0,
          adjustment  INTEGER NOT NULL DEFAULT 0,
          unchanged   INTEGER NOT NULL DEFAULT 0,
          unmatched   INTEGER NOT NULL DEFAULT 0,
          ambiguous   INTEGER NOT NULL DEFAULT 0,
          invalid     INTEGER NOT NULL DEFAULT 0,
          -- 이미 있던 원장 줄과 겹쳐 건너뛴 수. 재적용의 정상 결과다.
          duplicate   INTEGER NOT NULL DEFAULT 0,

          started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ,
          actor_user_id UUID REFERENCES app_user(id),

          CONSTRAINT ck_iir_mode CHECK (mode IN ('ANALYZE', 'DRY_RUN', 'APPLY'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_iir_org_time
            ON inventory_import_run (org_id, started_at DESC)
    """)
    op.execute("""
        COMMENT ON COLUMN inventory_import_run.source_sha256 IS
        '파일 내용 해시. 파일명은 바뀌고 Excel 은 열기만 해도 수정 시각이 '
        '바뀌므로, 같은 파일인지 확인할 방법은 내용 해시뿐이다.'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE inventory_import_run")
