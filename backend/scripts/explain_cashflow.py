"""현금흐름·계정 필터 질의의 실제 실행 계획 측정 (Phase 16).

왜 이 스크립트가 필요한가
--------------------
개발 DB 에는 판매 374건, 정산 0건, 지급 0건뿐이다. 그 크기에서 EXPLAIN 을 돌리면
Postgres 는 무조건 순차 스캔을 고르고, 그건 **인덱스가 필요한지에 대해 아무것도
말해 주지 않는다.** "지금 빠르니까 괜찮다" 는 판단은 셀러 한 명이 몇 년 치를
쌓은 뒤에 뒤집힌다.

그래서 현실적인 규모를 만들어 놓고 잰다. 인덱스는 **계획을 보고** 결정한다.

안전
----
테스트 DB(`picksome_ops_test`)에서 트랜잭션을 열고, 끝나면 ROLLBACK 한다.
데이터가 남지 않는다. 개발 DB 를 건드리지 않는다.

    python scripts/explain_cashflow.py
"""

from __future__ import annotations

import os
import sys
import uuid

import psycopg

DSN = os.environ.get(
    "EXPLAIN_DSN",
    "postgresql://picksome_test:picksome_test@localhost:5432/picksome_ops_test",
)

# 측정에서 배운 것 (이 주석이 곧 결론이다)
# -------------------------------------
#   정산 계정 필터   28.2ms → 0.074ms   ix_msr_org_account_created 채택
#   판매 계정 필터
#     · 주문이 많은 스토어   0.067ms → 0.098ms   (차이 없음)
#     · 주문이 적은 스토어   9.540ms → 0.095ms   ix_so_org_account_date 채택
#
# 주문이 많은 스토어만 재면 "인덱스가 필요 없다" 는 결론이 나온다. 위험한 쪽은
# **주문이 적은 스토어**다 — 기존 인덱스로는 org 의 주문을 처음부터 훑으며
# 50건을 찾을 때까지 걸러 낸다. 문 닫은 2호점을 조회하는 순간 그 비용을 낸다.

# 셀러 한 명이 몇 년 운영하면 도달하는 규모.
N_SALES = 300_000
N_SETTLEMENT = 120_000
N_PAYOUT_ITEM = 20_000
N_PAYMENT = 40_000
N_MANUAL = 30_000

ORG = uuid.uuid4()
OTHER_ORG = uuid.uuid4()
# 계정을 여러 개 둔다.
#
# 처음 측정할 때 한 조직의 **모든** 행을 계정 하나에 몰아넣었다. 그러면 계정
# 필터가 아무것도 걸러 내지 않아서 인덱스가 쓸모없다는 잘못된 결론이 나온다.
# 실제 셀러는 스토어를 2~5개 운영한다.
ACCOUNTS = [uuid.uuid4() for _ in range(4)]
ACCOUNT = ACCOUNTS[0]
CHANNEL = uuid.uuid4()

# 실제 코드가 쓰는 질의. 여기 있는 SQL 이 services 와 어긋나면 측정이 무의미하다.
QUERIES: list[tuple[str, str]] = [
    (
        "cashflow 실제 출금 (payout_payment)",
        """
        SELECT pp.id, pp.payment_date, pp.amount, pi.payee_name_snapshot
          FROM payout_payment pp
          JOIN payout_item pi ON pi.id = pp.payout_item_id
         WHERE pp.org_id = %(org)s AND pp.deleted_at IS NULL
           AND pp.payment_date BETWEEN %(s)s AND %(e)s
        """,
    ),
    (
        "cashflow 예상 출금 (payout_item)",
        """
        SELECT pi.id, pi.due_date, pi.due_amount - pi.paid_amount AS outstanding
          FROM payout_item pi
          JOIN payout_batch pb ON pb.id = pi.batch_id
         WHERE pi.org_id = %(org)s AND pi.deleted_at IS NULL
           AND pi.status IN ('APPROVED', 'PARTIALLY_PAID')
           AND pi.due_amount > pi.paid_amount
           AND pi.due_date IS NOT NULL
           AND pi.due_date BETWEEN %(s)s AND %(e)s
        """,
    ),
    (
        "cashflow 확정 입금 (marketplace_settlement_record)",
        """
        SELECT msr.id, msr.settled_at, msr.net_settlement
          FROM marketplace_settlement_record msr
          LEFT JOIN integration_account ia ON ia.id = msr.integration_account_id
         WHERE msr.org_id = %(org)s AND msr.deleted_at IS NULL
           AND msr.settled_at IS NOT NULL
           AND msr.settled_at BETWEEN %(s)s AND %(e)s
        """,
    ),
    (
        "cashflow 수동 기록 (manual_cash_event)",
        """
        SELECT id, direction, status, amount
          FROM manual_cash_event
         WHERE org_id = %(org)s AND deleted_at IS NULL
           AND COALESCE(occurred_at, expected_at) BETWEEN %(s)s AND %(e)s
           AND (source_type IS NULL OR source_type <> ALL(%(counted)s))
        """,
    ),
    (
        "판매 계정 필터 (M032)",
        """
        SELECT id, sale_date, sale_price FROM sales_order
         WHERE org_id = %(org)s AND deleted_at IS NULL
           AND integration_account_id = %(acc)s
         ORDER BY sale_date DESC LIMIT 50
        """,
    ),
    (
        "정산 계정 필터 (M033)",
        """
        SELECT id, settled_at FROM marketplace_settlement_record
         WHERE org_id = %(org)s AND deleted_at IS NULL
           AND integration_account_id = %(acc)s
         ORDER BY created_at DESC LIMIT 50
        """,
    ),
    (
        "미지급 총액 (기간 무관)",
        """
        SELECT COALESCE(SUM(due_amount - paid_amount), 0)
          FROM payout_item
         WHERE org_id = %(org)s AND deleted_at IS NULL
           AND status IN ('APPROVED', 'PARTIALLY_PAID')
           AND due_amount > paid_amount
        """,
    ),
]

# 측정 대상 후보. 여기 있는 것이 곧 마이그레이션이 되는 게 아니다 —
# BEFORE/AFTER 를 비교해서 실제로 빨라진 것만 채택한다.
CANDIDATE_INDEXES = [
    """
    CREATE INDEX ix_msr_org_account_created
        ON marketplace_settlement_record (org_id, integration_account_id, created_at DESC)
     WHERE deleted_at IS NULL
    """,
    """
    CREATE INDEX ix_so_org_account_date
        ON sales_order (org_id, integration_account_id, sale_date DESC)
     WHERE deleted_at IS NULL AND integration_account_id IS NOT NULL
    """,
]

PARAMS = {
    "org": str(ORG),
    "acc": str(ACCOUNT),
    "s": "2026-05-01",
    "e": "2026-05-31",
    "counted": ["payout_payment", "payout_item", "marketplace_settlement_record"],
}


def seed(cur: psycopg.Cursor) -> None:
    """조직 두 개에 볼륨을 만든다.

    **다른 조직 데이터를 섞는 게 중요하다.** 한 조직만 채우면 `org_id` 조건이
    아무것도 걸러 내지 않아서, 인덱스가 필요 없다는 잘못된 결론이 나온다.
    """
    for org in (ORG, OTHER_ORG):
        cur.execute(
            "INSERT INTO organization (id, business_name, business_type)"
            " VALUES (%s, %s, 'SOLE_PROPRIETOR')",
            (str(org), f"측정용 {org.hex[:6]}"),
        )
    cur.execute(
        "INSERT INTO sale_channel (id, org_id, code, display_name)"
        " VALUES (%s, %s, 'COUPANG', '쿠팡')",
        (str(CHANNEL), str(ORG)),
    )
    for n, acc in enumerate(ACCOUNTS):
        cur.execute(
            "INSERT INTO integration_account"
            " (id, org_id, provider, account_label, sale_channel_id, auth_kind, status)"
            " VALUES (%s, %s, 'COUPANG', %s, %s, 'BOT_TOKEN', 'ACTIVE')",
            (str(acc), str(ORG), f"스토어{n + 1}", str(CHANNEL)),
        )

    print(f"  판매 {N_SALES:,}건...", flush=True)
    cur.execute(
        """
        INSERT INTO sales_order
          (org_id, business_key, sale_date, marketplace, marketplace_order_no,
           product_snapshot_name, quantity, sale_price, purchase_cost, line_no,
           integration_account_id)
        SELECT CASE WHEN i %% 2 = 0 THEN %(org)s::uuid ELSE %(other)s::uuid END,
               'BK-' || i,
               DATE '2024-01-01' + (i %% 900),
               'COUPANG', 'ORD-' || i, '상품', 1, 10000 + (i %% 5000), 5000, 1,
               CASE WHEN i %% 2 = 0
                    THEN (ARRAY[%(a0)s, %(a1)s, %(a2)s, %(a3)s])[(i %% 4) + 1]::uuid
                    ELSE NULL END
          FROM generate_series(1, %(n)s) AS i
        """,
        {"org": str(ORG), "other": str(OTHER_ORG), "n": N_SALES,
         **{f"a{i}": str(a) for i, a in enumerate(ACCOUNTS)}},
    )

    print(f"  정산 {N_SETTLEMENT:,}건...", flush=True)
    cur.execute(
        """
        INSERT INTO marketplace_settlement_record
          (org_id, marketplace, settlement_period_start, settlement_period_end,
           marketplace_order_no, gross_revenue, marketplace_fee, ad_cost,
           return_cost, fulfillment_fee, other_deductions, net_settlement,
           settled_at, import_file_hash, import_idempotency_key,
           integration_account_id, created_at)
        SELECT CASE WHEN i %% 2 = 0 THEN %(org)s::uuid ELSE %(other)s::uuid END,
               'COUPANG',
               DATE '2024-01-01' + (i %% 900), DATE '2024-01-01' + (i %% 900),
               'SORD-' || i, 10000, 1000, 0, 0, 0, 0, 9000,
               DATE '2024-01-01' + (i %% 900), 'h-' || i,
               -- 원본 행 식별자 (M055). 행마다 유일해야 UNIQUE 를 통과한다.
               'PERF:' || i,
               CASE WHEN i %% 2 = 0
                    THEN (ARRAY[%(a0)s, %(a1)s, %(a2)s, %(a3)s])[(i %% 4) + 1]::uuid
                    ELSE NULL END,
               -- 한 문장으로 넣으면 DEFAULT now() 가 전부 같은 값이 된다.
               -- 그러면 ORDER BY created_at 이 6만 건 동률이 되어 인덱스 정렬
               -- 효과가 없는 것처럼 보인다 — 실제로 이 함정에 한 번 빠졌다.
               now() - (i || ' minutes')::interval
          FROM generate_series(1, %(n)s) AS i
        """,
        {"org": str(ORG), "other": str(OTHER_ORG), "n": N_SETTLEMENT,
         **{f"a{i}": str(a) for i, a in enumerate(ACCOUNTS)}},
    )

    print(f"  지급 항목 {N_PAYOUT_ITEM:,}건 + 지급 {N_PAYMENT:,}건...", flush=True)
    cur.execute(
        """
        INSERT INTO payout_batch (id, org_id, business_key, title, status)
        SELECT gen_random_uuid(),
               CASE WHEN i %% 2 = 0 THEN %(org)s::uuid ELSE %(other)s::uuid END,
               'PB-' || i, '배치 ' || i, 'APPROVED'
          FROM generate_series(1, 200) AS i
        """,
        {"org": str(ORG), "other": str(OTHER_ORG)},
    )
    cur.execute(
        """
        INSERT INTO payout_item
          (org_id, batch_id, item_type, accounting_effect, payee_name_snapshot,
           due_amount, paid_amount, status, due_date)
        SELECT b.org_id, b.id, 'PARTNER_DISTRIBUTION', 'PROFIT_APPROPRIATION',
               '파트너', 1000000, 0,
               CASE WHEN i %% 3 = 0 THEN 'APPROVED' ELSE 'CALCULATED' END,
               DATE '2024-01-01' + (i %% 900)
          FROM generate_series(1, %(n)s) AS i
          JOIN LATERAL (
              SELECT id, org_id FROM payout_batch
               OFFSET (i %% 200) LIMIT 1
          ) b ON TRUE
        """,
        {"n": N_PAYOUT_ITEM},
    )
    cur.execute(
        """
        INSERT INTO payout_payment (org_id, payout_item_id, payment_date, amount)
        SELECT pi.org_id, pi.id, DATE '2024-01-01' + (i %% 900), 1000
          FROM generate_series(1, %(n)s) AS i
          JOIN LATERAL (
              SELECT id, org_id FROM payout_item OFFSET (i %% 10000) LIMIT 1
          ) pi ON TRUE
        """,
        {"n": N_PAYMENT},
    )

    print(f"  수동 현금 {N_MANUAL:,}건...", flush=True)
    cur.execute(
        """
        INSERT INTO manual_cash_event
          (org_id, direction, status, amount, occurred_at)
        SELECT CASE WHEN i %% 2 = 0 THEN %(org)s::uuid ELSE %(other)s::uuid END,
               'OUTFLOW', 'ACTUAL', 10000, DATE '2024-01-01' + (i %% 900)
          FROM generate_series(1, %(n)s) AS i
        """,
        {"org": str(ORG), "other": str(OTHER_ORG), "n": N_MANUAL},
    )


def main() -> int:
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        print("볼륨 생성 중 (끝나면 ROLLBACK — 데이터는 남지 않는다)")
        seed(cur)

        print("\nANALYZE...", flush=True)
        cur.execute(
            "ANALYZE sales_order, marketplace_settlement_record,"
            " payout_item, payout_payment, manual_cash_event"
        )

        print("\n" + "=" * 70)
        for label, sql in QUERIES:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql, PARAMS)
            lines = [r[0] for r in cur.fetchall()]
            plan = "\n".join(lines)
            scan = "Seq Scan" if "Seq Scan" in plan else "Index"
            timing = next(
                (ln.strip() for ln in lines if ln.strip().startswith("Execution Time")),
                "?",
            )
            print(f"\n### {label}\n  [{scan}]  {timing}")
            for ln in lines[:6]:
                print("   " + ln)

        conn.rollback()
        print("\n" + "=" * 70)
        print("ROLLBACK 완료 — 측정 데이터는 남지 않았다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
