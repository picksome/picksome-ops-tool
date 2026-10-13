"""M048 — 재고 원장 · 발주 · 입고.

왜 잔량 컬럼이 아니라 원장인가
--------------------------
`product.stock = 12` 같은 컬럼을 두면 "왜 12 인가" 에 답할 수 없다. 판매가
빠졌는지, 입고가 두 번 들어갔는지, 사람이 손으로 고쳤는지 구분이 안 되고,
한 번 어긋나면 되돌릴 근거가 없다.

그래서 **움직임을 기록하고 잔량은 합계로 구한다.**

```
inventory_transaction   +50 입고 / -3 판매 / +1 반품 / -2 조정
현재 재고               = SUM(quantity_delta)
```

느려지면 나중에 스냅샷 테이블을 얹는다. 그 전에 스냅샷을 만들면 원장과
스냅샷이 어긋날 때 어느 쪽이 진실인지 정하는 일이 생긴다.

세 가지 원가를 섞지 않는다
----------------------
이게 이 마이그레이션에서 가장 중요한 구분이다.

```
product_source_cost_version   지금 다시 사면 얼마인가      (미래 구매)
inventory_transaction.unit_cost 이 재고를 얼마에 샀는가     (과거 취득)
product_margin_actual          판매된 주문에 적용된 원가    (확정된 과거)
```

셋은 서로를 덮지 않는다. 공급처가 오늘 값을 올려도 **창고에 있는 재고의
취득원가는 바뀌지 않고**, 이미 판매된 주문의 마진은 더더욱 바뀌지 않는다.

재고 단위(grain)
--------------
옵션이 있는 상품은 옵션별로 팔린다. 그래서 `product_variant_id` 를 둔다.
옵션이 없는 상품은 NULL 이다 — 억지로 기본 variant 를 만들지 않는다.

음수 재고를 DB 가 막지 않는다
-------------------------
막을지 말지는 업무 판단이고 아직 정해지지 않았다. 판매처 동기화가 늦으면
잠깐 음수가 될 수 있다. 대신 데이터 품질이 `NEGATIVE_INVENTORY` 로 잡는다 —
조용히 0 으로 만들면 실제로 몇 개가 빈 것인지 영영 모른다.

발주는 최소 흐름만
---------------
```
DRAFT → ORDERED → PARTIALLY_RECEIVED → RECEIVED
                → CANCELLED
```

ERP 승인 단계를 흉내 내지 않는다. 지금 필요한 것은 "무엇을 얼마에 주문했고
얼마나 들어왔나" 다.

발주 단가는 박제한다
-----------------
`purchase_order_line.unit_price` 는 주문 시점의 사실이다. 나중에 소싱 원가가
바뀌어도 과거 발주 금액이 바뀌면 안 된다.
"""

from alembic import op

revision = "M048"
down_revision = "M047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 재고 원장 ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE inventory_transaction (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          product_id         UUID NOT NULL REFERENCES product(id),
          -- 옵션이 있는 상품만 채운다. 없으면 NULL — 기본 variant 를 지어내지 않는다.
          product_variant_id UUID REFERENCES product_variant(id),

          transaction_type TEXT NOT NULL,

          -- 부호 있는 변화량. 입고 +, 판매 -, 조정은 양쪽 다 가능.
          -- 0 은 아무 일도 안 일어난 것이라 기록할 이유가 없다.
          quantity_delta INTEGER NOT NULL,

          -- 이 움직임의 단가. **취득원가**이고 소싱 원가와 다른 것이다.
          -- 판매·조정처럼 취득이 아닌 움직임은 NULL 이다 — 0 이 아니다.
          unit_cost INTEGER,
          currency  TEXT NOT NULL DEFAULT 'KRW',

          occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          -- 어디서 비롯됐나. FK 를 걸지 않는다 — 출처 종류가 여럿이다.
          source_table TEXT,
          source_id    UUID,

          -- 사람이 손으로 조정했다면 사유가 필수다(서비스가 강제한다).
          reason TEXT,

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_inv_txn_type CHECK (transaction_type IN
            ('OPENING', 'PURCHASE_RECEIPT', 'SALE', 'RETURN', 'ADJUSTMENT')),
          CONSTRAINT ck_inv_txn_nonzero CHECK (quantity_delta <> 0),
          CONSTRAINT ck_inv_txn_cost CHECK (unit_cost IS NULL OR unit_cost >= 0)
        )
    """)
    op.execute("""
        CREATE INDEX ix_inv_txn_item
            ON inventory_transaction (org_id, product_id, product_variant_id)
    """)
    op.execute("""
        CREATE INDEX ix_inv_txn_occurred
            ON inventory_transaction (org_id, occurred_at DESC)
    """)
    # 같은 출처가 두 번 들어오는 것을 DB 가 막는다. 판매 한 건이 재고를 두 번
    # 깎으면 원장 전체를 믿을 수 없게 된다.
    op.execute("""
        CREATE UNIQUE INDEX uq_inv_txn_source
            ON inventory_transaction (org_id, source_table, source_id)
         WHERE source_table IS NOT NULL AND source_id IS NOT NULL
    """)
    op.execute("""
        COMMENT ON COLUMN inventory_transaction.unit_cost IS
        '이 재고를 얼마에 취득했는가. product_source_cost_version(지금 다시 사면 '
        '얼마인가)과 다른 값이고 서로 덮지 않는다.'
    """)

    # ── 발주 ──────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE purchase_order (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),

          -- 사람이 부르는 번호. 행 id 를 화면에 노출하지 않기 위한 것이다.
          business_key TEXT NOT NULL,

          supplier_site_id UUID NOT NULL REFERENCES supplier_site(id),

          status TEXT NOT NULL DEFAULT 'DRAFT',

          currency TEXT NOT NULL DEFAULT 'KRW',
          ordered_at  TIMESTAMPTZ,
          expected_at DATE,
          memo TEXT,

          -- 자동화가 만든 초안이면 어느 실행에서 나왔는지 남는다.
          automation_run_id UUID REFERENCES automation_run(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by UUID REFERENCES app_user(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by UUID REFERENCES app_user(id),
          deleted_at TIMESTAMPTZ,
          deleted_by UUID REFERENCES app_user(id),

          CONSTRAINT ck_po_status CHECK (status IN
            ('DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED'))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_purchase_order_key
            ON purchase_order (org_id, business_key) WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_purchase_order_open
            ON purchase_order (org_id, status)
         WHERE deleted_at IS NULL AND status IN ('ORDERED', 'PARTIALLY_RECEIVED')
    """)

    op.execute("""
        CREATE TABLE purchase_order_line (
          id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NOT NULL REFERENCES organization(id),
          purchase_order_id UUID NOT NULL REFERENCES purchase_order(id),

          product_id         UUID NOT NULL REFERENCES product(id),
          product_variant_id UUID REFERENCES product_variant(id),

          quantity          INTEGER NOT NULL,
          -- 이미 입고된 수량. 부분 입고를 표현한다.
          received_quantity INTEGER NOT NULL DEFAULT 0,

          -- **주문 시점의 사실.** 나중에 소싱 원가가 바뀌어도 이 값은 안 바뀐다.
          unit_price INTEGER NOT NULL,

          -- 이 발주를 만들 때 근거가 된 소싱처. 원가 출처를 되짚을 수 있어야 한다.
          product_source_id UUID REFERENCES product_source(id),

          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

          CONSTRAINT ck_pol_qty CHECK (quantity > 0),
          CONSTRAINT ck_pol_received CHECK (received_quantity >= 0),
          -- 주문보다 많이 받는 것은 실제로 일어난다(덤·오배송). 막지 않는다.
          CONSTRAINT ck_pol_price CHECK (unit_price >= 0)
        )
    """)
    op.execute("""
        CREATE INDEX ix_pol_order ON purchase_order_line (purchase_order_id)
    """)
    op.execute("""
        CREATE INDEX ix_pol_item
            ON purchase_order_line (org_id, product_id, product_variant_id)
    """)

    # ── 기존 stock_quantity 와의 관계 ─────────────────────────────
    # `product_variant.stock_quantity` 는 엑셀 시절부터 있던 **사람이 손으로 적는
    # 칸**이다. 이력이 없고 옵션 화면에서 직접 덮어쓴다.
    #
    # 재고 출처가 둘이면 둘은 반드시 어긋나고, 어긋났을 때 어느 쪽이 맞는지
    # 정할 방법이 없다. 그래서 **원장이 정답이고 이 칸은 참고값**이라고 못을
    # 박는다. 지금 지우지 않는 이유는 옵션 화면이 아직 쓰고 있어서다(값이 든
    # 행은 0건이라 데이터 손실 위험은 없다). 화면이 원장으로 옮겨간 뒤 제거한다.
    op.execute("""
        COMMENT ON COLUMN product_variant.stock_quantity IS
        '[비권위] 손으로 적는 참고값. 실제 재고는 inventory_transaction 합계다. '
        '두 값이 다르면 원장이 맞다. 옵션 화면 이전 후 제거 예정.'
    """)


def downgrade() -> None:
    # 주석도 되돌린다. 원장이 사라진 뒤에 "실제 재고는 inventory_transaction 에
    # 있다" 는 주석이 남으면 없는 테이블을 가리키게 된다.
    op.execute("COMMENT ON COLUMN product_variant.stock_quantity IS NULL")
    op.execute("DROP TABLE purchase_order_line")
    op.execute("DROP TABLE purchase_order")
    op.execute("DROP TABLE inventory_transaction")
