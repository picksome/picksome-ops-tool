"""M036 — 마진 계산이 쓰는데 저장할 곳이 없던 세 값.

무엇이 문제였나
------------
프론트 마진 엔진(`lib/calc/engine.ts`)은 세 가지를 읽어 계산한다.

```
tax_policy       input_vat_credit_policy    매입세액을 공제하는가
                 import_vat_credit_policy   수입 부가세를 원가에 넣는가
marketplace_fee  fee_vat_included           수수료율이 부가세 포함인가
```

**셋 다 DB 에 컬럼이 없었다.** 그래서 화면은 정책 저장을 메모리 배열
(`settingsAdapter`)로 흉내 내고 있었고, 새로고침하면 값이 사라졌다.
저장되지 않는 세율 화면은 단순한 버그가 아니라 **틀린 마진을 만드는 경로**다.

왜 NULL 을 허용하는가
------------------
기본값을 정해 넣으면 기존 정책이 전부 "확인된 값" 처럼 보인다.
`fee_vat_included` 를 잘못 잡으면 수수료가 통째로 10% 어긋나는데,
그걸 아무 경고 없이 넣는 것은 모르는 것을 아는 척하는 것이다.

`NULL` 은 **"아직 확인하지 않았다"** 다. 화면은 이 값이 비어 있으면
계산 결과에 "확인 필요" 를 붙인다 — 0 과 모름을 구분하는 이 프로젝트의 규칙과 같다.

추측 backfill 을 하지 않는다
------------------------
"쿠팡은 보통 포함이더라" 같은 추정으로 채우지 않는다. 한 번 채워 넣으면
그게 확인된 값이 되고, 나중에 누구도 그것이 추측이었다는 사실을 알 수 없다.
"""

from alembic import op

revision = "M036"
down_revision = "M035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tax_policy
            ADD COLUMN input_vat_credit_policy TEXT,
            ADD COLUMN import_vat_credit_policy TEXT
    """)
    op.execute("""
        ALTER TABLE tax_policy
            ADD CONSTRAINT ck_tax_input_vat_credit
            CHECK (input_vat_credit_policy IS NULL
                   OR input_vat_credit_policy IN
                      ('FULL_CREDIT', 'FLAT_RATE_CREDIT', 'NO_CREDIT'))
    """)
    op.execute("""
        ALTER TABLE tax_policy
            ADD CONSTRAINT ck_tax_import_vat_credit
            CHECK (import_vat_credit_policy IS NULL
                   OR import_vat_credit_policy IN ('CREDITABLE', 'NOT_CREDITABLE'))
    """)
    op.execute("""
        COMMENT ON COLUMN tax_policy.input_vat_credit_policy IS
        '매입세액 공제 방식. NULL 은 "아직 확인하지 않았다" — 화면이 확인 필요로 표시한다.'
    """)

    op.execute("""
        ALTER TABLE marketplace_fee_policy
            ADD COLUMN fee_vat_included BOOLEAN
    """)
    op.execute("""
        COMMENT ON COLUMN marketplace_fee_policy.fee_vat_included IS
        '수수료율이 부가세를 포함한 값인가. 마켓마다 표기가 달라 틀리면 수수료가 10% 어긋난다. NULL 은 미확인.'
    """)
    # backfill 없음 — 추측으로 채우면 그게 확인된 값이 된다.


def downgrade() -> None:
    op.execute("ALTER TABLE marketplace_fee_policy DROP COLUMN IF EXISTS fee_vat_included")
    op.execute("ALTER TABLE tax_policy DROP CONSTRAINT IF EXISTS ck_tax_import_vat_credit")
    op.execute("ALTER TABLE tax_policy DROP CONSTRAINT IF EXISTS ck_tax_input_vat_credit")
    op.execute("""
        ALTER TABLE tax_policy
            DROP COLUMN IF EXISTS import_vat_credit_policy,
            DROP COLUMN IF EXISTS input_vat_credit_policy
    """)
