"""M032 — 주문이 어느 판매자 계정에서 왔는지 기록한다.

문제
----
`sales_order` 가 판매처를 아는 유일한 방법은 `marketplace` 텍스트 컬럼이다
(`sale_channel.code`). 그런데 **한 판매처에 판매자 계정이 여럿일 수 있다** —
쿠팡 본점과 쿠팡 2호점은 같은 `marketplace='COUPANG'` 이다.

그래서 지금은 "쿠팡 매출 300만원" 까지만 말할 수 있고 "본점 200만 / 2호점 100만"
을 말할 수 없다. 정산서는 계정 단위로 오기 때문에, 이 구분 없이는 정산 대사도
계정별 손익도 불가능하다.

`product_channel_listing`(M022)은 이미 `integration_account_id` 로 붙는다.
주문만 문자열에 머물러 있었다.

왜 지금 NOT NULL 이 아닌가
------------------------
기존 주문에는 이 정보가 **어디에도 없다.** 확인한 사실(추측 아님):

  · 기존 주문은 전부 `import_source='EXCEL_IMPORT'` 이고 `import_batch_id` 가 NULL
  · `integration_account` 행이 아직 0건인 조직이 있다
  · 봇 ingest 는 `batch_run.job_id` 를 NULL 로 넣어서
    `import_batch_id → batch_run → integration_job → account_id` 체인이 끊겨 있다

따라서 **자동 backfill 이 가능한 행이 구조적으로 없다.** "쿠팡이니까 쿠팡 계정
아무거나" 로 채우면 그 순간부터 계정별 매출이 거짓이 되고, 나중에 진짜 계정을
알게 돼도 이미 틀린 값이 확정 snapshot 에 박제된 뒤다.

그래서 NULL 을 유지한다. NULL 은 "이 주문이 어느 계정 것인지 모른다" 는 뜻이고,
그건 사실이다.

NOT NULL 전환 조건
-----------------
아래가 전부 참일 때 별도 migration 으로 전환한다.

  1. 수동 주문 등록 화면이 계정 선택을 필수로 받는다
  2. 모든 ingest 경로가 실행 계정을 넘긴다 (이 migration 과 함께 적용됨)
  3. `SELECT COUNT(*) FROM sales_order WHERE integration_account_id IS NULL` 이 0
  4. 남은 legacy NULL 을 사람이 계정에 귀속시키는 도구가 있다

3번을 만족시키려고 legacy 주문을 임의 계정에 넣는 것은 이 설계의 목적을 정면으로
배반한다 — 그럴 바에는 NULL 이 낫다.

인덱스를 왜 안 만드나
------------------
지금 이 컬럼으로 조회하는 곳이 없다. 대시보드·마감·정산 집계는 전부
`org_id + sale_date` 로 돌고, 계정별 집계 화면은 아직 없다(Phase 6 이후).

근거 없이 인덱스를 만들면 쓰기 비용만 늘어난다. FK 는 인덱스 없이도 동작하고,
참조 무결성 검사는 부모(`integration_account.id` = PK)에서 일어난다.
자식 쪽 인덱스가 필요해지는 것은 **부모를 DELETE 할 때**인데, 이 FK 는
`ON DELETE NO ACTION` 이고 `integration_account` 는 soft delete 를 쓴다.

계정별 집계 화면이 생기면 그때 EXPLAIN 을 근거로 추가한다.
"""

from alembic import op

revision = "M032"
down_revision = "M031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ON DELETE NO ACTION — 기존 4개 참조 테이블(integration_job, external_product_mapping,
    # product_channel_listing, channel_price_publish_outbox)과 같은 관례다.
    #
    # CASCADE 로 두면 계정을 지울 때 **과거 주문이 함께 사라진다.** 매출 기록이
    # 계정 정리 작업에 딸려 없어지는 것은 어떤 경우에도 옳지 않다.
    # SET NULL 도 쓰지 않는다 — 확정된 달의 귀속이 조용히 풀리면 마감 snapshot 과
    # 원장이 어긋난다. 참조가 남아 있으면 삭제를 거부하는 편이 낫다.
    op.execute("""
        ALTER TABLE sales_order
            ADD COLUMN integration_account_id UUID
            REFERENCES integration_account(id)
    """)

    op.execute("""
        COMMENT ON COLUMN sales_order.integration_account_id IS
        '이 주문이 유입된 판매자 계정. NULL 은 "모른다"(legacy 또는 수동 등록 시 미지정)이며 "계정 없음"이 아니다. marketplace 문자열로 추론해 채우지 않는다.'
    """)

    # ── backfill ──
    #
    # **확실하게 하나로 결정되는 주문만** 채운다. 여기서 채워지는 행은
    # 자동 수집 경로로 들어왔고, 그 배치가 어느 계정의 job 이었는지가
    # DB 에 남아 있는 경우뿐이다.
    #
    # 지금 데이터에서는 0건이 맞는다(`batch_run.job_id` 가 전부 NULL). 그래도
    # 이 구문을 남기는 이유: 다른 환경에 `integration_job` 을 통해 들어온 주문이
    # 있을 수 있고, 그건 추측이 아니라 **기록된 사실**이라 채워도 안전하다.
    #
    # `marketplace` 문자열로 계정을 찾는 backfill 은 **의도적으로 넣지 않는다.**
    # 같은 채널에 계정이 여럿이면 어느 쪽인지 알 수 없고, 하나뿐이라 해도
    # "그 주문이 그 계정에서 왔다" 는 보장이 없다(엑셀로 넣은 과거 주문일 수 있다).
    op.execute("""
        UPDATE sales_order so
           SET integration_account_id = ij.account_id
          FROM batch_run br
          JOIN integration_job ij ON ij.id = br.job_id
         WHERE so.import_batch_id = br.id
           AND so.integration_account_id IS NULL
           AND ij.account_id IS NOT NULL
           -- 계정은 주문과 같은 조직이어야 한다. 아니면 데이터가 이미 깨진 것이므로
           -- 여기서 조용히 이어 붙이지 않는다.
           AND EXISTS (
                 SELECT 1 FROM integration_account ia
                  WHERE ia.id = ij.account_id AND ia.org_id = so.org_id
               )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE sales_order DROP COLUMN IF EXISTS integration_account_id")
