"""M015 판매채널 3종 추가 — 오늘의집 / 카카오쇼핑 / 29CM.

배경:
  사용자가 실제로 판매 중이라고 밝힌 마켓 중 3곳이 sale_channel 카탈로그에 없었다
  (2026-08-05 확인). 채널이 없으면 판매 등록 시 OTHER 로 떨어지고, OTHER 에는
  마켓 수수료 정책이 없어 수수료가 0 으로 계산된다 — 실 마진이 그만큼 과대 계상된다.

  M007 이 marketplace 컬럼을 ENUM → TEXT 로 바꿔뒀으므로 채널 추가는 row INSERT
  만으로 끝난다. ALTER TYPE 도, enum_sale_channel 수정도 필요 없다.
  (M007 docstring 이 '오늘의집처럼 새 마켓을 추가' 를 바로 이 시나리오의 예시로 든다.)

수수료 정책은 일부러 만들지 않는다:
  이 3곳의 실제 수수료율 근거가 아직 없다. 엑셀 `마켓수수료` 시트에도 없다.
  추정치를 넣으면 그게 정답인 것처럼 굳어지므로(M010 에서 지어낸 추정치를
  실제값으로 교체하느라 데인 전례) 정책은 비워두고, 판매가 실제로 생길 때
  근거와 함께 marketplace_fee_policy 를 추가한다.
  그때까지 이 채널의 판매는 수수료 0 + warning 으로 계산된다.

org_id NULL = SYSTEM 카탈로그(모든 org 공용). M007 의 기존 9종과 같은 취급.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M015"
down_revision: str | None = "M014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_CHANNELS = (
    ("OHOUSE", "오늘의집"),
    ("KAKAO_SHOPPING", "카카오쇼핑"),
    ("TWENTYNINE_CM", "29CM"),
)


def upgrade() -> None:
    for code, name in _NEW_CHANNELS:
        # 재실행/부분적용 대비 — uq_sale_channel_org_code 는 partial unique index 라
        # ON CONFLICT 로 못 잡는다. NOT EXISTS 로 건다.
        op.execute(
            f"""
            INSERT INTO sale_channel (org_id, code, display_name, is_system_default, is_active)
            SELECT NULL, '{code}', '{name}', TRUE, TRUE
             WHERE NOT EXISTS (
               SELECT 1 FROM sale_channel
                WHERE org_id IS NULL AND code = '{code}' AND deleted_at IS NULL
             )
            """
        )


def downgrade() -> None:
    codes = ", ".join(f"'{c}'" for c, _ in _NEW_CHANNELS)
    # 이미 이 채널로 판매가 등록됐다면 지우지 않는다 — 지우면 sales_order.marketplace
    # 가 카탈로그에 없는 값이 되어 조회/정책 매칭이 깨진다.
    op.execute(
        f"""
        DELETE FROM sale_channel
         WHERE org_id IS NULL AND code IN ({codes})
           AND NOT EXISTS (
             SELECT 1 FROM sales_order so WHERE so.marketplace = sale_channel.code
           )
        """
    )
