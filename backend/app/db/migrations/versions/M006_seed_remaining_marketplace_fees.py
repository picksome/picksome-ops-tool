"""M006 마켓 수수료 시드 확장 — 지마켓/옥션/11번가/스마트스토어(네이버) placeholder.

배경:
  enum_sale_channel(M001) 에는 SMARTSTORE/ELEVENST/GMARKET/AUCTION 이 이미
  값으로 존재했지만, marketplace_fee_policy 시드는 COUPANG(M002) 과
  TEMU/TOSS(M004) 뿐이었다 — 이 4개 채널은 값을 골라도 수수료 0원으로
  계산되는 조용한 버그 상태였다. "쇼핑몰 별로 다른 수수료 값 적용" 요청에
  대응해 나머지 4개 채널도 동일 관례(RATE, NEEDS_REVIEW)로 채운다.

조치:
  marketplace_fee_policy 에 4개 마켓 placeholder row 추가
  (SYSTEM 템플릿 + PICKSOME org 양쪽 — M004 와 동일 패턴).
  수치는 카테고리 평균 근사치이며 실제 값 확인 전까지 NEEDS_REVIEW.
  스마트스토어는 매출연동수수료만 포함 — 네이버페이 결제수수료는
  payment_fee_policy 영역(별도 관리, 이 시드에 포함하지 않음).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M006"
down_revision: str | None = "M005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_ORG_ID = "00000000-0000-0000-0000-0000000000ff"
PICKSOME_ORG_ID = "00000000-0000-0000-0000-000000000001"

# (marketplace, rate, memo)
_NEW_FEE_ROWS = (
    (
        "GMARKET", "0.1100",
        "오픈마켓 카테고리 평균 근사치 — 실제 수수료율 확인 전 임시값, 운영 검증 후 갱신",
    ),
    (
        "AUCTION", "0.1100",
        "지마켓과 같은 ESM 계열이나 수수료는 별도 관리 — 실제 수수료율 확인 전 임시값",
    ),
    (
        "ELEVENST", "0.1100",
        "오픈마켓 카테고리 평균 근사치 — 실제 수수료율 확인 전 임시값, 운영 검증 후 갱신",
    ),
    (
        "SMARTSTORE", "0.0363",
        "매출연동수수료만 포함(네이버페이 결제수수료는 payment_fee_policy 별도) — 임시값",
    ),
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
    for org_id in _existing_orgs(SYSTEM_ORG_ID, PICKSOME_ORG_ID):
        for marketplace, rate, memo in _NEW_FEE_ROWS:
            op.execute(
                f"""
                INSERT INTO marketplace_fee_policy
                  (org_id, version, effective_from, source_type, reliability_status, is_active,
                   marketplace, fee_type, rate, memo)
                VALUES
                  ('{org_id}', 1, '2026-01-01', 'MANUAL', 'NEEDS_REVIEW', TRUE,
                   '{marketplace}', 'RATE', {rate}, '{memo}')
                """
            )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM marketplace_fee_policy
         WHERE org_id IN ('{SYSTEM_ORG_ID}', '{PICKSOME_ORG_ID}')
           AND marketplace IN ('GMARKET', 'AUCTION', 'ELEVENST', 'SMARTSTORE')
        """
    )
