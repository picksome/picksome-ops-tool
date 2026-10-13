"""M056 — 정산 행의 **종류**, 벤더 라인 참조, 정산 예정일.

CORE_DOMAIN_GAP — 실제 파일이 증명했다
-----------------------------------
쿠팡 WING 정산 Excel(`MSF_PURCHASE_REVENUE_DETAIL`) 3개를 실측했다.
주문 하나가 정산서에서 **행 여러 개**로 온다.

```
주문번호 A
├─ 상품 행          옵션 ID = 숫자        판매액 24,000
├─ <기본배송료> 행   옵션 ID = <기본배송료>  판매액 3,000
└─ <추가배송료> 행   옵션 ID = <추가배송료>  판매액 0
```

실측 67행 = 상품 23 + 기본배송료 22 + 추가배송료 22.

**지금 스키마로는 이 셋을 구분할 수 없다.** 셋 다 `gross_revenue` ·
`marketplace_fee` · `net_settlement` 로 들어가면 배송비 매출이 상품 매출과
같은 칸에 섞인다. 그 상태로 다음 단계(실마진·정산 대사)를 하면 상품 마진에
배송비가 얹힌다.

`fulfillment_fee` 에 넣는 안은 **틀렸다.** 그건 우리가 내는 비용이고,
여기 배송료는 구매자에게 받아 정산되는 **매출**이다. 이름이 비슷하다고
넣으면 부호와 의미가 동시에 뒤집힌다.

왜 vendor-specific 이 아닌가
-------------------------
"정산 행의 경제적 종류" 는 어느 마켓이든 있다 — 상품 매출, 배송비, 수수료,
조정, 프로모션. 쿠팡만의 개념이 아니다. `coupang_option_id` 같은 컬럼을
만들지 않았고, 벤더 헤더를 그대로 박지도 않았다.

**관측한 것만 넣는다**
--------------------
```
PRODUCT_SALE          상품 매출 행        실측 23
SHIPPING_FEE_BASE     기본배송료 행       실측 22
SHIPPING_FEE_EXTRA    추가배송료 행       실측 22
UNKNOWN               분류할 수 없는 행    실측 0
```

환불·조정·프로모션 유형은 **만들지 않았다.** 실측 3개 파일에 표본이
0건이다(환불수량 전부 0, 취소완료일 전부 비어 있음). 안 본 것을 어휘에
미리 넣으면 "지원한다" 는 착각을 준다 — 실제 표본이 오면 그때 넓힌다.

`UNKNOWN` 은 있어야 한다. 모르는 `<...>` 표식이 오면 그 행을 조용히
상품으로 세는 것보다 모른다고 말하는 편이 낫다.

line_reference — 벤더가 준 라인 식별자
----------------------------------
`marketplace_order_no` 는 **주문** 단위다. 정산서는 주문 안에서 행을
`옵션 ID` 로 나눈다. 담을 자리가 없으면:

- 멱등 키(`import_idempotency_key`)를 컬럼에서 되짚을 수 없다
- 우리 쪽에 옵션 식별자가 생겨도 라인 단위 매칭으로 못 넘어간다

지금은 우리 `sales_order` 에 쿠팡 옵션 ID 를 담는 자리가 없어서 라인 단위
매칭을 **하지 않는다**(다건 주문은 AMBIGUOUS 로 남는다). 그건 나중 일이고,
그때 이 컬럼이 없으면 재수집부터 다시 해야 한다.

expected_settlement_date — 예정일은 정산일이 아니다
-----------------------------------------------
파일이 주는 날짜는 `정산예정일` 이다. **입금됐다는 증거가 아니다.**

```
settled_at              실제 정산/지급이 일어난 날    ← 모른다. NULL 로 둔다
expected_settlement_date 벤더가 말한 예정일           ← 파일이 주는 값
```

`settled_at` 에 예정일을 넣으면 그 순간 "지급 완료" 가 되고, 현금흐름이
그 위에서 계산된다. 두 칸을 나누는 이유가 그것이다.

`settlement_period_*` 에 예정일을 넣는 안도 버렸다. 그 컬럼은 **정산이
포괄하는 기간**이고, 예정일은 한 점이다. 한 칸에 두 뜻을 담으면 다음 사람이
어느 쪽으로 읽을지 알 수 없다.

batch_run.org_id — 조직 격리
--------------------------
`marketplace_settlement_record.import_batch_id` 가 이미 `batch_run` 을
가리킨다(M001). 그런데 `batch_run` 에 `org_id` 가 없다 — import 실행 기록을
조직으로 나눠 볼 수 없고, 남의 조직 배치를 참조해도 DB 가 모른다.

새 import-run 테이블을 만드는 대신 **이미 배선된 테이블의 구멍을 메운다.**
NULL 허용이라 기존 경로(`integration_ingest`)가 깨지지 않는다.
"""

from alembic import op

revision = "M056"
down_revision = "M055"
branch_labels = None
depends_on = None

# 실측한 유형만. 안 본 유형을 미리 넣지 않는다(위 docstring).
_ROW_TYPE = "'PRODUCT_SALE', 'SHIPPING_FEE_BASE', 'SHIPPING_FEE_EXTRA', 'UNKNOWN'"


def upgrade() -> None:
    # `marketplace_settlement_record` 는 0건이라 기본값 없는 NOT NULL 로 시작할
    # 수 있다. **기본값을 주지 않는다** — 기본값이 있으면 종류를 안 정한 적재가
    # 조용히 통과하고, 그게 정확히 막으려던 것이다(M055 의 멱등 키와 같은 이유).
    op.execute("""
        DO $$
        DECLARE existing INT;
        BEGIN
            SELECT COUNT(*) INTO existing FROM marketplace_settlement_record;
            IF existing > 0 THEN
                RAISE EXCEPTION
                    '[M056_EXISTING_RECORDS] 정산 레코드가 % 건 있습니다. 행 종류(row_type)를 '
                    '지어낼 수 없으므로 자동으로 채우지 않습니다. 원본에서 다시 import 하거나 '
                    '각 행의 종류를 정한 뒤 이 마이그레이션을 다시 작성하세요.', existing;
            END IF;
        END $$
    """)

    op.execute(f"""
        ALTER TABLE marketplace_settlement_record
          ADD COLUMN row_type TEXT NOT NULL,
          -- 벤더가 준 주문 내 라인 식별자. 쿠팡은 `옵션 ID`(숫자) 또는
          -- `<기본배송료>` 같은 표식이 여기 온다.
          ADD COLUMN line_reference TEXT,
          -- 예정일. `settled_at`(실제 정산일)과 **다른 사실**이다.
          ADD COLUMN expected_settlement_date DATE,

          ADD CONSTRAINT ck_msr_row_type
            CHECK (row_type IN ({_ROW_TYPE}))
    """)

    # import 실행 단위로 결과를 훑는 조회. 대사 화면이 이 축으로 센다.
    op.execute("""
        CREATE INDEX ix_msr_import_batch
            ON marketplace_settlement_record (org_id, import_batch_id)
         WHERE deleted_at IS NULL AND import_batch_id IS NOT NULL
    """)

    # 조직 격리 — NULL 허용이라 기존 경로가 깨지지 않는다.
    op.execute("""
        ALTER TABLE batch_run
          ADD COLUMN org_id UUID REFERENCES organization(id)
    """)
    op.execute("""
        CREATE INDEX ix_batch_run_org_started
            ON batch_run (org_id, started_at DESC)
         WHERE deleted_at IS NULL AND org_id IS NOT NULL
    """)

    op.execute("""
        COMMENT ON COLUMN marketplace_settlement_record.row_type IS
        '정산 행의 경제적 종류. PRODUCT_SALE=상품 매출, SHIPPING_FEE_BASE=기본배송료, SHIPPING_FEE_EXTRA=추가배송료, UNKNOWN=분류 불가. 실제 파일에서 관측한 유형만 있다 — 환불/조정은 표본이 생기면 넓힌다.'
    """)
    op.execute("""
        COMMENT ON COLUMN marketplace_settlement_record.expected_settlement_date IS
        '벤더가 말한 정산 예정일. 입금 증거가 아니다 — 실제 정산일은 settled_at 이고, 모르면 NULL 로 남는다.'
    """)
    op.execute("""
        COMMENT ON COLUMN marketplace_settlement_record.line_reference IS
        '주문 안에서 이 행을 가리키는 벤더 라인 식별자. marketplace_order_no 는 주문 단위라 다건 주문을 구분하지 못한다.'
    """)


def downgrade() -> None:
    # M055 와 같은 규칙 — 사람이 판단한 것이 남아 있으면 멈춘다.
    # 여기서는 행 종류가 사라지면 배송비 매출과 상품 매출을 되살릴 수 없다.
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM marketplace_settlement_record
             WHERE deleted_at IS NULL AND row_type <> 'PRODUCT_SALE';
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '[M056_NON_PRODUCT_ROWS] 상품 매출이 아닌 정산 행이 % 건 있습니다. '
                    '되돌리면 배송비 행과 상품 행을 구분할 수 없게 됩니다.', remaining;
            END IF;
        END $$
    """)

    op.execute("DROP INDEX IF EXISTS ix_batch_run_org_started")
    op.execute("ALTER TABLE batch_run DROP COLUMN IF EXISTS org_id")
    op.execute("DROP INDEX IF EXISTS ix_msr_import_batch")
    op.execute("""
        ALTER TABLE marketplace_settlement_record
          DROP CONSTRAINT IF EXISTS ck_msr_row_type,
          DROP COLUMN IF EXISTS expected_settlement_date,
          DROP COLUMN IF EXISTS line_reference,
          DROP COLUMN IF EXISTS row_type
    """)
