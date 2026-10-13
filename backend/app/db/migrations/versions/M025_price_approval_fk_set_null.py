"""M025 — product_channel_price.approval_request_id FK 를 ON DELETE SET NULL 로.

왜
--
가격 버전은 **그 자체가 기록**이다. 승인 요청은 그 가격이 왜 생겼는지를 가리키는
출처(provenance) 정보다. 둘의 중요도가 다르다.

기본 FK(RESTRICT)면 승인 요청을 지우려 할 때 가격이 그걸 막는다. 그러면
보존 기간이 지난 승인 요청을 정리하는 작업이 통째로 막히고, 정리하려면
**가격 이력을 먼저 지워야 한다** — 순서가 완전히 거꾸로다.

`SET NULL` 이면 승인 요청이 사라져도 가격 버전은 남는다. 출처를 잃는 건 아쉽지만,
가격 이력을 잃는 것보다 훨씬 낫다. 그리고 감사 로그에 같은 정보가 남아 있다.

발견 경위
--------
기존 테스트의 정리 코드(`DELETE FROM approval_request WHERE org_id = ...`)가
FK 위반으로 실패하면서 드러났다. 운영에서는 승인 요청을 soft delete 하므로
당장 문제가 되진 않지만, 그건 "지금 그 경로를 안 쓴다" 는 뜻이지
제약이 맞다는 뜻은 아니다.
"""

from alembic import op

revision = "M025"
down_revision = "M024"
branch_labels = None
depends_on = None

_FK = "product_channel_price_approval_request_id_fkey"


def upgrade() -> None:
    op.execute(f"ALTER TABLE product_channel_price DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(f"""
        ALTER TABLE product_channel_price
          ADD CONSTRAINT {_FK}
          FOREIGN KEY (approval_request_id) REFERENCES approval_request(id)
          ON DELETE SET NULL
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE product_channel_price DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(f"""
        ALTER TABLE product_channel_price
          ADD CONSTRAINT {_FK}
          FOREIGN KEY (approval_request_id) REFERENCES approval_request(id)
    """)
