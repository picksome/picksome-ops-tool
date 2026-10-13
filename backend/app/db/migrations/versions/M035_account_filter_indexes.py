"""M035 — 계정별 조회 인덱스 2종.

추측이 아니라 측정으로 정했다
--------------------------
개발 DB 에는 판매 374건, 정산 0건뿐이다. 그 크기의 EXPLAIN 은 항상 순차 스캔이
나오고, **인덱스가 필요한지에 대해 아무것도 말해 주지 않는다.**

`scripts/explain_cashflow.py` 로 현실적인 규모(판매 30만·정산 12만)를 만들어
같은 질의를 BEFORE/AFTER 로 쟀다. 결과:

```
정산 계정 필터 (M033)        28.200ms → 0.074ms    채택
판매 계정 필터 (M032)
  · 주문이 많은 스토어         0.067ms → 0.098ms    (차이 없음)
  · 주문이 적은 스토어         9.540ms → 0.095ms    채택
```

여기 없는 질의는 인덱스를 만들지 않았다
----------------------------------
현금흐름의 나머지 질의는 이미 기존 인덱스를 쓰고 있고 전부 1ms 미만이었다.

```
확정 입금 (settled_at 범위)   ix_msr_org_settled          0.72ms
수동 기록 (기간)              ix_manual_cash_org_dates    0.15ms   ← M034
예상 출금 (payout_item)       ix_pi_open                  2.90ms
```

**안 쓰이는 인덱스를 만들지 않는다.** 인덱스는 공짜가 아니다 — 모든 INSERT/UPDATE
가 그만큼 느려지고 디스크를 먹는다. "혹시 몰라서" 만든 인덱스가 쌓이면 쓰기가
느려진 이유를 나중에 아무도 설명하지 못한다.

왜 부분 인덱스인가
---------------
`sales_order` 쪽은 `integration_account_id IS NOT NULL` 로 좁힌다. 기존 주문은
대부분 계정이 NULL 이고(M032 이전 데이터는 추측 backfill 을 하지 않았다),
그 행들은 계정 필터 조회의 대상이 아니다. 넣어 봐야 인덱스만 커진다.

`marketplace_settlement_record` 쪽은 NULL 을 포함한다. 정산서는 아직 0건이라
앞으로 들어올 데이터가 전부 계정을 갖는지 알 수 없고, "계정 미상 정산서만
보기" 는 실제로 쓸 만한 조회다.
"""

from alembic import op

revision = "M035"
down_revision = "M034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 정산 계정 필터 — 28.2ms → 0.074ms
    #
    # `ORDER BY created_at DESC LIMIT 50` 이 정렬 없이 끝나는 것이 핵심이다.
    # 인덱스가 없으면 계정에 맞는 행을 전부 모아 top-N 정렬을 한다.
    op.execute("""
        CREATE INDEX ix_msr_org_account_created
            ON marketplace_settlement_record (org_id, integration_account_id, created_at DESC)
         WHERE deleted_at IS NULL
    """)

    # 판매 계정 필터 — 주문이 적은 스토어에서 9.5ms → 0.095ms
    #
    # 기존 `ix_so_org_sale_date` 로도 주문이 많은 스토어는 빠르다. 문제는 반대쪽이다:
    # 계정을 필터로 걸면 org 의 주문을 최신순으로 훑으며 맞지 않는 행을 버리는데,
    # 그 계정의 주문이 드물면 50건을 찾을 때까지 계속 훑는다.
    op.execute("""
        CREATE INDEX ix_so_org_account_date
            ON sales_order (org_id, integration_account_id, sale_date DESC)
         WHERE deleted_at IS NULL AND integration_account_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_so_org_account_date")
    op.execute("DROP INDEX IF EXISTS ix_msr_org_account_created")
