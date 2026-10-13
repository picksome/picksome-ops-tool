"""M033 — 정산 레코드도 판매자 계정을 갖는다.

왜 필요한가
----------
M032 로 주문은 계정을 갖게 됐다. 그런데 정산서는 여전히 `marketplace` 문자열뿐이다.

```
COUPANG
 ├─ 본점    ← 정산서가 계정 단위로 온다
 └─ 2호점
```

정산 대사는 "이 주문의 정산금이 얼마인가" 를 맞추는 일이다. 주문은 계정을
아는데 정산서는 모르면, 본점 주문에 2호점 정산금이 붙어도 알아챌 방법이 없다.

M032 와 같은 규칙
---------------
- NULL 허용. 기존 데이터는 계정을 모른다
- **추측 backfill 금지.** `marketplace='COUPANG'` 이라는 이유로 쿠팡 계정
  아무거나 고르지 않는다
- FK 는 `ON DELETE NO ACTION` — 기존 5개 참조 테이블과 같은 관례.
  CASCADE 로 두면 계정 정리 작업에 정산 기록이 딸려 사라진다

backfill 을 아예 넣지 않은 이유
-----------------------------
M032 에는 `import_batch_id → batch_run → integration_job → account_id` 라는
**기록된 경로**가 있어서 그것만 채웠다(현재 0건).

정산 레코드에는 그런 경로가 없다. `import_batch_id` 는 `batch_run` 을 가리키지만
정산 import 는 `integration_job` 을 거치지 않는다. 즉 채울 근거가 되는 컬럼이
하나도 없다. 없는 근거로 UPDATE 문을 쓰면 그건 backfill 이 아니라 추측이다.

인덱스
-----
만들지 않는다. 지금 이 컬럼으로 조회하는 곳이 없고(`ix_msr_org_settled` 가
기존 조회를 덮는다), 계정별 정산 화면은 아직 없다.
FK 는 인덱스 없이 동작하고 부모는 soft delete 를 쓴다.
실제 query plan 을 보고 판단한다(Phase 18).

⚠️ `settled_at` 의 의미에 대하여
-----------------------------
이 migration 은 `settled_at` / `net_settlement` 이 **실제 은행 입금**인지에 대해
어떤 가정도 하지 않는다. 스키마에 정의가 없고(M001 에 주석 없음), 레코드도 0건이라
확인할 방법이 없다. 이름만 보고 "지급 완료" 로 해석하면 현금흐름 전체가 거짓이 된다.
Cashflow 는 이 값을 ACTUAL 로 쓰지 않는다 — `docs/54` 참고.
"""

from alembic import op

revision = "M033"
down_revision = "M032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE marketplace_settlement_record
            ADD COLUMN integration_account_id UUID
            REFERENCES integration_account(id)
    """)

    op.execute("""
        COMMENT ON COLUMN marketplace_settlement_record.integration_account_id IS
        '이 정산서가 속한 판매자 계정. NULL 은 "모른다"(legacy/import 시 미지정)이며 "계정 없음"이 아니다. marketplace 문자열로 추론해 채우지 않는다.'
    """)

    # backfill 없음. 위 docstring 의 "backfill 을 아예 넣지 않은 이유" 참고.


def downgrade() -> None:
    op.execute("""
        ALTER TABLE marketplace_settlement_record
            DROP COLUMN IF EXISTS integration_account_id
    """)
