"""M054 — 원 주문이 장부에 없는 클레임을 `NO_LEDGER_IMPACT` 로 분리한다.

라이브 검증이 찾아낸 오분류
------------------------
M053 을 실제 데이터에 돌린 뒤 확인했더니, 클레임 16건 중 14건이
`financial_effect = 'UNRESOLVED'` 로 앉아 있었다. 전수 확인 결과 **14건 모두
원 주문이 장부에 아예 없었다** — 출고 전에 취소돼 매출로 잡힌 적이 없는
건들이다(어떤 식별자로도 `sales_order` 에 없다).

그런데 `UNRESOLVED` 는 마감 검증에서 BLOCKING 이다.

```
2026-05  CLAIM_FINANCIAL_UNRESOLVED  BLOCKING  5건
2026-06  CLAIM_FINANCIAL_UNRESOLVED  BLOCKING  2건
2026-07  CLAIM_FINANCIAL_UNRESOLVED  BLOCKING  7건
```

세 달이 전부 마감 불가였다. **순매출은 한 원도 틀리지 않았는데도.**

두 상태는 정반대의 사실이다
------------------------
```
UNRESOLVED        확정됐는데 금액을 못 정했다   순매출이 부풀어 있다  → 막아야 한다
NO_LEDGER_IMPACT  원 주문이 장부에 없다        순매출은 정확하다    → 막으면 안 된다
```

`UNRESOLVED` 가 마감을 막는 근거는 "그 상태로 잠그면 순매출이 부풀어 있고,
그 위에서 순이익과 파트너 배분이 전부 계산된다" 는 것이다. 원 주문이 없으면
되돌릴 매출도 없으므로 그 근거가 성립하지 않는다.

조용히 넘기는 것도 아니다
---------------------
`UNMATCHED_CLAIM_ORDER` 운영 이슈는 그대로 남고, 마감 검증도
`CLAIM_WITHOUT_ORDER` WARNING 으로 보여 준다. 주문 동기화가 밀려서 아직 안
들어온 것일 수도 있기 때문이다 — 주문이 뒤늦게 들어오면 다음 동기화가 잇고
그때 매출 조정이 생긴다.

왜 M053 을 고치지 않고 새 마이그레이션인가
-----------------------------------
M053 은 이미 적용됐고 그 위에 실제 데이터 16건이 올라가 있다. 적용된
마이그레이션을 손으로 고치면 DB 상태와 파일이 어긋나고, 그 어긋남은
다음 사람에게 보이지 않는다. **오분류를 찾아낸 사실 자체가 기록될 값어치가
있다** — 이 파일이 그 기록이다.
"""

import logging

import sqlalchemy as sa
from alembic import op

_log = logging.getLogger("alembic.M054")

revision = "M054"
down_revision = "M053"
branch_labels = None
depends_on = None

_OLD = "'NONE', 'PENDING', 'EFFECTIVE', 'REVERSED', 'UNRESOLVED'"
_NEW = _OLD + ", 'NO_LEDGER_IMPACT'"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sales_claim
          DROP CONSTRAINT IF EXISTS ck_sales_claim_financial_effect
    """)
    op.execute(f"""
        ALTER TABLE sales_claim
          ADD CONSTRAINT ck_sales_claim_financial_effect
          CHECK (financial_effect IN ({_NEW}))
    """)

    # 이미 적재된 것을 다시 분류한다.
    #
    # **조건을 좁게 잡는다.** 원 주문이 없다는 것만으로는 부족하다 —
    # 모르는 벤더 코드(`claim_type='UNKNOWN'`)는 그게 취소인지조차 알 수
    # 없으므로 `UNRESOLVED` 로 남긴다. 금액이 이미 정해진 행
    # (`claim_amount IS NOT NULL`)도 건드리지 않는다.
    moved = op.get_bind().execute(sa.text("""
        UPDATE sales_claim
           SET financial_effect = 'NO_LEDGER_IMPACT'
         WHERE financial_effect = 'UNRESOLVED'
           AND sales_order_id IS NULL
           AND deleted_at IS NULL
           AND claim_status = 'COMPLETED'
           AND claim_type IN ('CANCEL', 'RETURN')
           AND claim_amount IS NULL
    """)).rowcount
    _log.info("NO_LEDGER_IMPACT 로 재분류한 클레임: %s건", moved)

    op.execute("""
        COMMENT ON COLUMN sales_claim.financial_effect IS
        'NO_LEDGER_IMPACT = 확정된 취소·반품이지만 원 주문이 장부에 없어 되돌릴 매출이 없다(출고 전 취소). UNRESOLVED 와 달리 마감을 막지 않는다.'
    """)


def downgrade() -> None:
    # 되돌리면 이 행들은 다시 마감을 막게 된다. 그게 되돌리기의 의미다 —
    # 조용히 지우지 않고 원래 상태로 되돌린다.
    op.execute("""
        UPDATE sales_claim SET financial_effect = 'UNRESOLVED'
         WHERE financial_effect = 'NO_LEDGER_IMPACT'
    """)
    op.execute("""
        ALTER TABLE sales_claim
          DROP CONSTRAINT IF EXISTS ck_sales_claim_financial_effect
    """)
    op.execute(f"""
        ALTER TABLE sales_claim
          ADD CONSTRAINT ck_sales_claim_financial_effect
          CHECK (financial_effect IN ({_OLD}))
    """)
