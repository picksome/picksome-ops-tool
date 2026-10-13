"""M010 마켓 수수료 실제 요율 반영 + 정책 적용 시작일 소급.

배경:
  1) 수수료율이 전부 추정치였다.
     M002/M004/M006 이 넣은 marketplace_fee_policy 값은 내가 카테고리 평균으로
     추정한 placeholder(NEEDS_REVIEW)였다. 사용자 엑셀
     `마진계산기&장부 (2).xlsx` 의 `마켓수수료` 시트에 실제 운영 기준 요율이
     있었으므로 그 값으로 교체한다. 차이가 작지 않다 —
     예: SMARTSTORE 0.0363 → 0.06, ELEVENST 0.11 → 0.15.

  2) 모든 정책의 effective_from 이 2026-01-01 이었다.
     그런데 실제 판매 데이터는 2025-01-05 부터 있다(전체 365건 중 272건, 75%가
     2025년). services/policy.py 의 조회 조건이 `effective_from <= :at` 이므로
     2025년 거래는 **적용 정책을 하나도 못 찾는 상태**였다 — 마진·정산 계산이
     조용히 0원 수수료로 돌거나 실패한다. 실데이터 검증을 하려면 반드시 풀어야
     하는 선행 조건이라 같이 처리한다.

왜 새 버전(v2)을 만들지 않고 기존 row 를 고치는가:
  정책 버전 관리의 목적은 "과거 계산의 재현"이다(CLAUDE.md). 그런데 이 시점에
  product_margin_expected / product_margin_actual / monthly_settlement /
  policy_snapshot 이 **전부 0건**이다 — 이 정책들로 계산해 박제한 결과가
  하나도 없으므로 소급 수정으로 잃는 이력이 없다. 오히려 v2 를 만들면
  effective_from 이 겹쳐 `ORDER BY effective_from DESC LIMIT 1` 이 어느 쪽을
  고를지 모호해진다. 이후에 실제 정산이 LOCKED 된 뒤에는 반드시 새 버전으로만
  바꿔야 한다.

reliability_status:
  엑셀 시트 제목이 "참고용 수수료"이므로 마켓 공식문서로 교차검증된 값은 아니다.
  다만 사용자가 직접 관리해 온 운영 기준값이므로, 내가 지어낸 추정치와는
  구분되어야 한다. NEEDS_REVIEW(계산 시 경고 발생, calculation.py:175) 대신
  MANUAL_OVERRIDE 로 둔다. 엑셀에 없는 TEMU/TOSS 는 추정치 그대로이므로
  NEEDS_REVIEW 를 유지한다.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M010"
down_revision: str | None = "M009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_ORG_ID = "00000000-0000-0000-0000-0000000000ff"
PICKSOME_ORG_ID = "00000000-0000-0000-0000-000000000001"

# 판매 데이터 최초 건이 2025-01-05 이므로 그 이전으로 잡는다.
BACKDATED_FROM = "2025-01-01"
PREVIOUS_FROM = "2026-01-01"

# 정책 계열 테이블 전체 (effective_from 컬럼 보유)
_POLICY_TABLES = (
    "ad_cost_policy",
    "evidence_type_policy",
    "expense_category_policy",
    "fulfillment_fee_policy",
    "marketplace_fee_policy",
    "partner_share_policy",
    "payment_fee_policy",
    "return_risk_policy",
    "settlement_policy",
    "target_margin_policy",
    "tax_policy",
)

# 엑셀 `마켓수수료` 시트 실제값 — (marketplace, rate)
_REAL_FEE_RATES = (
    ("COUPANG", "0.1100"),
    ("SMARTSTORE", "0.0600"),
    ("GMARKET", "0.1300"),
    ("AUCTION", "0.1300"),
    ("ELEVENST", "0.1500"),
    ("WEMAKEPRICE", "0.0400"),
    ("INTERPARK", "0.1400"),
)

# 엑셀에는 있으나 sale_channel 에 없던 채널 — (code, display_name)
_NEW_CHANNELS = (
    ("WEMAKEPRICE", "위메프"),
    ("INTERPARK", "인터파크"),
)

_FEE_MEMO = (
    "엑셀 `마진계산기&장부` 마켓수수료 시트(참고용 수수료)의 운영 기준값. "
    "M002/M004/M006 의 추정 placeholder 를 대체."
)


def _existing_orgs(*org_ids: str) -> list[str]:
    """실제로 존재하는 org 만 돌려준다.

    PICKSOME_ORG_ID 는 마이그레이션이 아니라 scripts/internal/picksome_init.py 가 만든다.
    그래서 신규 DB(테스트 DB 포함)에는 그 organization 행이 없고, 무조건 INSERT 하면
    FK 위반으로 마이그레이션 전체가 실패한다. dev DB 에서는 두 org 가 다 있어 결과가 같다.
    """
    from alembic import op as _op

    rows = _op.get_bind().exec_driver_sql(
        "SELECT id::text FROM organization WHERE id::text = ANY(%(ids)s)",
        {"ids": list(org_ids)},
    ).fetchall()
    return [r[0] for r in rows]


def upgrade() -> None:
    # ── 1. 정책 적용 시작일 소급 ──────────────────────────────────
    # 2026-01-01 로 시드된 것만 옮긴다. 이후에 다른 날짜로 만들어진 정책이
    # 있다면 건드리지 않는다.
    for table in _POLICY_TABLES:
        op.execute(
            f"""
            UPDATE {table}
               SET effective_from = DATE '{BACKDATED_FROM}',
                   updated_at     = NOW()
             WHERE effective_from = DATE '{PREVIOUS_FROM}'
               AND deleted_at IS NULL
            """
        )

    # ── 2. 신규 판매 채널 (위메프/인터파크) ───────────────────────
    # sale_channel 은 M007 이후 org_id NULL = 시스템 공통 채널이다.
    for code, display_name in _NEW_CHANNELS:
        op.execute(
            f"""
            INSERT INTO sale_channel
              (id, org_id, code, display_name, is_system_default, is_active,
               created_at, updated_at)
            SELECT gen_random_uuid(), NULL, '{code}', '{display_name}', TRUE, TRUE,
                   NOW(), NOW()
             WHERE NOT EXISTS (
                   SELECT 1 FROM sale_channel
                    WHERE code = '{code}' AND org_id IS NULL AND deleted_at IS NULL)
            """
        )

    # ── 3. 수수료 실제 요율 반영 ──────────────────────────────────
    for org_id in _existing_orgs(SYSTEM_ORG_ID, PICKSOME_ORG_ID):
        for marketplace, rate in _REAL_FEE_RATES:
            # 기존 row 가 있으면 갱신
            op.execute(
                f"""
                UPDATE marketplace_fee_policy
                   SET rate               = {rate},
                       version            = version + 1,
                       source_type        = 'IMPORTED',
                       reliability_status = 'MANUAL_OVERRIDE',
                       effective_from     = DATE '{BACKDATED_FROM}',
                       memo               = '{_FEE_MEMO}',
                       updated_at         = NOW()
                 WHERE org_id      = '{org_id}'
                   AND marketplace = '{marketplace}'
                   AND deleted_at IS NULL
                """
            )
            # 없으면(위메프/인터파크) 새로 넣는다
            op.execute(
                f"""
                INSERT INTO marketplace_fee_policy
                  (id, org_id, version, effective_from, source_type,
                   reliability_status, is_active, marketplace, fee_type, rate,
                   memo, created_at, updated_at)
                SELECT gen_random_uuid(), '{org_id}', 1, DATE '{BACKDATED_FROM}',
                       'IMPORTED', 'MANUAL_OVERRIDE', TRUE,
                       '{marketplace}', 'RATE', {rate}, '{_FEE_MEMO}', NOW(), NOW()
                 WHERE NOT EXISTS (
                       SELECT 1 FROM marketplace_fee_policy
                        WHERE org_id = '{org_id}' AND marketplace = '{marketplace}'
                          AND deleted_at IS NULL)
                """
            )


def downgrade() -> None:
    # 수수료 원복 — M002/M004/M006 의 추정치로 되돌린다.
    _ORIGINAL = (
        ("COUPANG", "0.1080"),
        ("SMARTSTORE", "0.0363"),
        ("GMARKET", "0.1100"),
        ("AUCTION", "0.1100"),
        ("ELEVENST", "0.1100"),
    )
    for org_id in _existing_orgs(SYSTEM_ORG_ID, PICKSOME_ORG_ID):
        for marketplace, rate in _ORIGINAL:
            op.execute(
                f"""
                UPDATE marketplace_fee_policy
                   SET rate               = {rate},
                       version            = GREATEST(version - 1, 1),
                       source_type        = 'MANUAL',
                       reliability_status = 'NEEDS_REVIEW',
                       effective_from     = DATE '{PREVIOUS_FROM}',
                       updated_at         = NOW()
                 WHERE org_id = '{org_id}' AND marketplace = '{marketplace}'
                """
            )
        op.execute(
            f"""
            DELETE FROM marketplace_fee_policy
             WHERE org_id = '{org_id}'
               AND marketplace IN ('WEMAKEPRICE', 'INTERPARK')
            """
        )

    op.execute(
        "DELETE FROM sale_channel WHERE code IN ('WEMAKEPRICE', 'INTERPARK')"
        " AND org_id IS NULL"
    )

    for table in _POLICY_TABLES:
        op.execute(
            f"""
            UPDATE {table}
               SET effective_from = DATE '{PREVIOUS_FROM}',
                   updated_at     = NOW()
             WHERE effective_from = DATE '{BACKDATED_FROM}'
               AND deleted_at IS NULL
            """
        )
