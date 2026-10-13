"""M041 — 공급처 마스터에 연락·취급품목 칸 추가 (Excel `도매 매입 사이트들` 대응).

왜 필요한가
---------
운영 Excel 의 공급업체 시트에는 우리가 아직 담을 곳이 없는 칸이 있다.

```
공급업체 유형   → 취급 품목군 ("생활/잡화", "펫", "낚시")
문의            → 담당자 이름
주소            → 공급처 주소
참조            → 그 공급처를 고를 때 참고한 한 줄
```

`supplier_type` 에 넣지 않은 이유
------------------------------
`supplier_type` 은 `WHOLESALE / MANUFACTURER / ...` 처럼 **거래 형태**다.
Excel 의 "공급업체 유형" 은 "생활/잡화, 펫" 처럼 **무엇을 파는가**다. 둘은 다른
축이고, 한 공급처가 여러 품목군을 취급한다("생활/잡화, 펫, 낚시"). 형태 열거형에
품목을 밀어 넣으면 둘 다 못 쓰게 된다.

지금은 Excel 원문을 그대로 담는 자유 텍스트다. 품목군을 정규화하려면 분류
체계를 먼저 정해야 하는데 그건 이 마이그레이션의 일이 아니다 — **추측해서
쪼개지 않는다.**

⚠️ 이 Excel 에는 ID/PW 열이 평문으로 있다
--------------------------------------
**옮겨오지 않는다.** 컬럼도 만들지 않는다. importer 는 값이 있다는 사실만
`credential_present` 로 남긴다(M037 에서 이미 있는 칸이다). 실제 비밀은
`password_vault_entry` 가 보관 위치로만 다룬다 — CLAUDE.md Security Rules.

backfill 없음
------------
전부 NULL 로 시작한다. NULL 은 "없다" 가 아니라 **"아직 모른다"** 다.
"""

from alembic import op

revision = "M041"
down_revision = "M040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE supplier_site
            ADD COLUMN contact_name        TEXT,
            ADD COLUMN address             TEXT,
            -- Excel "공급업체 유형" 원문. 쉼표로 여러 개가 들어온다.
            ADD COLUMN handling_categories TEXT,
            -- Excel "참조". 메모와 섞지 않는다 — 메모는 거래 조건, 이쪽은 출처다.
            ADD COLUMN reference_note      TEXT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE supplier_site
            DROP COLUMN reference_note,
            DROP COLUMN handling_categories,
            DROP COLUMN address,
            DROP COLUMN contact_name
    """)
