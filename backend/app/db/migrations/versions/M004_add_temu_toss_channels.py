"""M004 마켓 확장 — 테무(TEMU), 토스(TOSS) 판매채널 추가.

배경:
  `enum_sale_channel`(M001)은 COUPANG/SMARTSTORE/ELEVENST/GMARKET/AUCTION/
  SELF_MALL/OTHER 만 있었다. 네이버는 SMARTSTORE로 이미 커버되지만 테무·토스는
  없어서 이 채널로 판매하는 상품의 마진 계산이 불가능했다.

조치:
  - enum_sale_channel 에 TEMU, TOSS 추가.
  - marketplace_fee_policy 에 두 마켓 placeholder row 추가
    (SYSTEM 템플릿 + 이미 존재하는 PICKSOME org 양쪽 — PICKSOME은
    picksome_init.py 의 SYSTEM 템플릿 복사가 최초 1회만 실행되므로
    새 SYSTEM row 는 자동으로 안 따라가 직접 넣어야 함).
    수수료율은 실제 값 확인 전까지 NEEDS_REVIEW로 표시 (COUPANG 시드와 동일 관례).

주의 (M003과 동일한 Postgres 제약):
  트랜잭션 내 ALTER TYPE ADD VALUE는 같은 트랜잭션에서 바로 못 쓰므로
  autocommit_block으로 분리.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M004"
down_revision: str | None = "M003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_ORG_ID = "00000000-0000-0000-0000-0000000000ff"
PICKSOME_ORG_ID = "00000000-0000-0000-0000-000000000001"

_NEW_CHANNELS = ("TEMU", "TOSS")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_CHANNELS:
            op.execute(f"ALTER TYPE enum_sale_channel ADD VALUE IF NOT EXISTS '{value}'")

    # ⚠️ PICKSOME_ORG_ID 는 마이그레이션이 아니라 scripts/internal/picksome_init.py 가 만든다.
    # 그래서 **신규 DB(테스트 DB 포함)에는 그 organization 행이 없다.**
    # 원래 코드는 무조건 INSERT 해서 FK 위반으로 마이그레이션이 통째로 실패했고,
    # 그 탓에 통합 테스트가 fresh DB 에서 한 번도 돌지 못했다(CREATEDB 권한 이슈에 가려져 있었다).
    #
    # 존재하는 org 에만 넣는다. dev DB 에서는 두 org 가 다 있으므로 결과가 동일하고,
    # fresh DB 에서는 SYSTEM 만 시드되어 마이그레이션이 끝까지 통과한다.
    for org_id in (SYSTEM_ORG_ID, PICKSOME_ORG_ID):
        op.execute(
            f"""
            INSERT INTO marketplace_fee_policy
              (org_id, version, effective_from, source_type, reliability_status, is_active,
               marketplace, fee_type, rate, memo)
            SELECT o.id, 1, '2026-01-01', 'MANUAL', 'NEEDS_REVIEW', TRUE,
                   -- M004 시점에는 marketplace 가 아직 enum_sale_channel 이다(M007 이 TEXT 로 바꾼다).
                   -- VALUES 에서 온 text 를 명시적으로 캐스팅해야 한다.
                   v.marketplace::enum_sale_channel, 'RATE', v.rate::numeric,
                   '실제 수수료율 확인 전 임시값 — 운영 검증 후 갱신'
              FROM organization o
             CROSS JOIN (VALUES ('TEMU', 0.0800), ('TOSS', 0.0300))
                        AS v(marketplace, rate)
             WHERE o.id = '{org_id}'
            """
        )


def downgrade() -> None:
    # 시드해둔 placeholder 정책만 회수. enum 값 자체는 Postgres 제약상
    # 컬럼 6개(marketplace_fee_policy/return_risk_policy/ad_cost_policy/
    # product_margin_expected/sales_order/marketplace_settlement_record)를
    # 타입 재생성해야 해서, TEMU/TOSS 로 이미 실거래가 쌓인 경우엔 이
    # 다운그레이드를 실행하지 말 것 (데이터 유실 위험 — 재생성 전 수동 마이그레이션 필요).
    op.execute(
        f"""
        DELETE FROM marketplace_fee_policy
         WHERE org_id IN ('{SYSTEM_ORG_ID}', '{PICKSOME_ORG_ID}')
           AND marketplace IN ('TEMU', 'TOSS')
        """
    )
