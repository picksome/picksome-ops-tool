"""M007 판매채널 동적화 — sale_channel 테이블 신설 + enum_sale_channel 을 TEXT 로 전환.

배경 (docs/27_dynamic_channel_and_bot_integration_plan.md D-287):
  마켓(판매채널)이 Postgres ENUM(enum_sale_channel)이라 "오늘의집처럼 새 마켓을
  추가"하려면 매번 ALTER TYPE + 배포가 필요했다. DB 레벨 제약(ENUM)을 앱 레벨
  검증(services/sale_channel.py::resolve_sale_channel)으로 옮겨서, org 가
  sale_channel row 하나만 추가하면 바로 마켓 수수료 정책/판매등록에 쓸 수 있게 한다.

조치:
  1. sale_channel 테이블 신설. org_id NULL = SYSTEM 카탈로그(모든 org 공용 기본 9종),
     org_id 있음 = 그 org 가 직접 추가한 커스텀 채널.
  2. 기존 9종(COUPANG/SMARTSTORE/ELEVENST/GMARKET/AUCTION/SELF_MALL/OTHER/TEMU/TOSS)을
     is_system_default=TRUE 로 시드 — 값 자체(code)는 안 바뀌므로 기존 데이터 무손상.
  3. marketplace_fee_policy/ad_cost_policy/return_risk_policy/product_margin_expected/
     sales_order/marketplace_settlement_record 6개 테이블의 marketplace 컬럼을
     enum_sale_channel → TEXT 로 변환(USING ::TEXT, 기존 값 그대로 문자열화 — 데이터 무손상).
  4. enum_sale_channel 타입 자체는 이번엔 안 지운다(더는 어떤 컬럼도 안 쓰지만,
     드롭은 별도 정리 마이그레이션에서 — 되돌리기(downgrade) 안전성 우선).

주의:
  다운그레이드는 TEXT → enum_sale_channel 로 되돌리는데, 그 사이 org 가 커스텀
  코드(예: 'OHOUSE')를 넣었으면 enum 에 없는 값이라 실패한다 — 의도된 동작(커스텀
  채널 도입 이후엔 구조적으로 되돌릴 수 없음을 명시).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M007"
down_revision: str | None = "M006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_COLS = """
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by UUID NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by UUID NULL,
  deleted_at TIMESTAMPTZ NULL,
  deleted_by UUID NULL
"""

_SYSTEM_CHANNELS = (
    ("COUPANG", "쿠팡"),
    ("SMARTSTORE", "네이버 스마트스토어"),
    ("ELEVENST", "11번가"),
    ("GMARKET", "지마켓"),
    ("AUCTION", "옥션"),
    ("SELF_MALL", "자사몰"),
    ("OTHER", "기타"),
    ("TEMU", "테무"),
    ("TOSS", "토스"),
)

_MARKETPLACE_COLUMN_TABLES = (
    "marketplace_fee_policy",
    "ad_cost_policy",
    "return_risk_policy",
    "product_margin_expected",
    "sales_order",
    "marketplace_settlement_record",
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE sale_channel (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id UUID NULL REFERENCES organization(id),
          code TEXT NOT NULL,
          display_name TEXT NOT NULL,
          is_system_default BOOLEAN NOT NULL DEFAULT FALSE,
          integration_kind TEXT NOT NULL DEFAULT 'NONE',
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          {AUDIT_COLS},
          CONSTRAINT ck_sale_channel_integration_kind
            CHECK (integration_kind IN ('NONE', 'API', 'BOT'))
        );
        CREATE UNIQUE INDEX uq_sale_channel_org_code
          ON sale_channel(COALESCE(org_id, '00000000-0000-0000-0000-000000000000'), code)
          WHERE deleted_at IS NULL;
        CREATE INDEX ix_sale_channel_org_active ON sale_channel(org_id, is_active);
        """
    )

    for code, name in _SYSTEM_CHANNELS:
        op.execute(
            f"""
            INSERT INTO sale_channel (org_id, code, display_name, is_system_default, is_active)
            VALUES (NULL, '{code}', '{name}', TRUE, TRUE)
            """
        )

    for table in _MARKETPLACE_COLUMN_TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN marketplace TYPE TEXT USING marketplace::TEXT"
        )


def downgrade() -> None:
    for table in _MARKETPLACE_COLUMN_TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN marketplace TYPE enum_sale_channel "
            f"USING marketplace::enum_sale_channel"
        )
    op.execute("DROP TABLE sale_channel")
