"""M059 — 사람이 승인한 기준값(Golden Sample).

왜 새 테이블이 필요한가 (CORE_DOMAIN_GAP)
--------------------------------------
지금 저장소 어디에도 **"사람이 원천을 보고 이 값이 맞다고 확인했다"** 를
담는 곳이 없다. `product_margin_actual` 은 우리가 계산한 값이고,
`marketplace_settlement_record` 는 벤더가 준 값이다. 둘 다 **파생·수신**이지
사람의 판정이 아니다.

Golden Sample 의 목적은 하나다.

```
코드가 바뀌어 계산이 깨졌을 때, 우리 계산과 무관한 기준으로 잡아낸다
```

기준값을 우리 계산에서 복사해 오면 그 순간 이 장치는 무의미해진다 — 코드가
어떻게 바뀌든 항상 통과한다. 그래서 기준값은 **사람이 넣어야** 하고, 사람이
넣은 값을 담을 칸이 필요하다.

재무 사실을 복제하는 것이 아니다
----------------------------
이 테이블은 매출·원가·마진의 **또 다른 출처가 아니다.** 어떤 화면도 여기서
금액을 읽어 표시하지 않는다. 회귀 검사만 읽는다. 그래서 §60 이 금지하는
"검증 편의를 위한 재무 진실 중복 저장" 에 해당하지 않는다.

승인 전에는 기준값이 없다
---------------------
`CANDIDATE` 상태로 만들어지고 `expected_json` 은 비어 있다. 사람이 원천을
확인하고 값을 넣어야 `APPROVED` 가 된다. 자동으로 채우지 않는다.

기준값을 바꾸면 이유가 남는다
------------------------
승인된 값을 조용히 덮으면, 계산이 틀렸을 때 기준값을 고쳐서 통과시킬 수
있다. 그건 회귀 검사를 무력화하는 가장 쉬운 방법이다 — `revision` 을 올리고
`change_reason` 을 받는다(DB CHECK 가 강제한다).
"""

from alembic import op

revision = "M059"
down_revision = "M058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE verification_golden_case (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id        UUID NOT NULL REFERENCES organization(id),
          sales_order_id UUID NOT NULL REFERENCES sales_order(id),

          -- 이 주문이 어떤 상황을 대표하는가. 후보 선정기가 붙인다.
          case_type     TEXT NOT NULL,
          -- 왜 이 주문을 골랐는지. 사람이 확인할 때 읽는다.
          selection_reason TEXT NOT NULL,

          status        TEXT NOT NULL DEFAULT 'CANDIDATE',

          -- 사람이 승인한 기준값. **승인 전에는 NULL 이다.**
          -- 모든 항목을 채울 필요는 없다 — 확인할 수 있는 것만 넣는다.
          -- 값을 모르면 그 항목을 빼거나 null 로 둔다(UNKNOWN 허용).
          expected_json JSONB,
          -- 무엇을 보고 확인했는지 (정산서 파일명·화면·주문번호 등).
          source_evidence TEXT,

          approved_at   TIMESTAMPTZ,
          approved_by   UUID REFERENCES app_user(id),

          -- 기준값을 고칠 때마다 올린다. 1 = 최초 승인.
          revision      INT NOT NULL DEFAULT 0,
          change_reason TEXT,

          note          TEXT,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by    UUID REFERENCES app_user(id),
          updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_by    UUID REFERENCES app_user(id),
          deleted_at    TIMESTAMPTZ,
          deleted_by    UUID REFERENCES app_user(id),

          CONSTRAINT ck_vgc_status
            CHECK (status IN ('CANDIDATE', 'APPROVED', 'RETIRED')),

          -- 승인됐다면 기준값과 승인자·시각이 반드시 있다. 빈 승인을 막는다.
          CONSTRAINT ck_vgc_approved_has_expected
            CHECK (status <> 'APPROVED' OR (
              expected_json IS NOT NULL AND approved_at IS NOT NULL
              AND approved_by IS NOT NULL AND revision >= 1)),

          -- 기준값을 두 번째부터 고칠 때는 이유가 필요하다. 조용한 덮어쓰기 금지.
          CONSTRAINT ck_vgc_revision_reason
            CHECK (revision <= 1 OR change_reason IS NOT NULL)
        )
    """)

    # 한 주문에 살아 있는 golden case 는 하나다. 같은 주문에 서로 다른
    # 기준값이 둘 있으면 어느 쪽이 기준인지 알 수 없다.
    op.execute("""
        CREATE UNIQUE INDEX uq_vgc_order_alive
            ON verification_golden_case (org_id, sales_order_id)
         WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_vgc_org_status
            ON verification_golden_case (org_id, status)
         WHERE deleted_at IS NULL
    """)

    op.execute("""
        COMMENT ON TABLE verification_golden_case IS
        '사람이 원천을 보고 승인한 기준값. 회귀 검사만 읽는다 — 화면이 금액을 여기서 읽지 않는다(M059).'
    """)
    op.execute("""
        COMMENT ON COLUMN verification_golden_case.expected_json IS
        '사람이 넣은 기준값. Seller OS 계산 결과를 복사해 넣으면 회귀 검사가 무의미해진다(M059).'
    """)


def downgrade() -> None:
    # 승인된 기준값이 있으면 되돌리지 않는다. 사람이 원천을 보고 넣은 값이라
    # 다시 만들려면 그 확인을 처음부터 반복해야 한다 — 자동 복구가 불가능하다.
    op.execute("""
        DO $$
        DECLARE remaining INT;
        BEGIN
            SELECT COUNT(*) INTO remaining FROM verification_golden_case
             WHERE status = 'APPROVED' AND deleted_at IS NULL;
            IF remaining > 0 THEN
                RAISE EXCEPTION
                    '[M059_APPROVED_GOLDEN_EXISTS] 승인된 기준값이 % 건 있습니다. '
                    '사람이 원천을 확인해 넣은 값이라 삭제하면 복구할 수 없습니다. '
                    '먼저 내보내 두거나 RETIRED 로 바꾼 뒤 되돌리세요.', remaining;
            END IF;
        END $$
    """)
    op.execute("DROP TABLE IF EXISTS verification_golden_case")
