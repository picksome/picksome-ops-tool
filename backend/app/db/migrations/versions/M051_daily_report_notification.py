"""M051 — 일일 리포트 알림 종류.

`enum_notification_kind` 에 `DAILY_REPORT_READY` 를 더한다.

왜 enum 을 늘리나
--------------
`kind` 를 자유 문자열로 바꾸면 오타가 새 종류로 조용히 들어가고, 사용자는
"알림이 안 온다" 만 겪는다. 종류를 늘리는 비용(마이그레이션 한 줄)이
그 위험보다 싸다.

왜 알림으로 전달하나
-----------------
리포트 전달을 위해 별도 `report_delivery` 테이블을 만들지 않는다. 알림에는
이미 필요한 것이 다 있다.

```
수신자 결정      역할 기반
중복 방지        dedupe_key
읽음 처리        read_at
채널 확장 자리    channel_code
```

`dedupe_key = "daily_report:<날짜>"` 로 두면 worker 가 두 번 돌아도 같은 날
리포트 알림이 두 번 가지 않는다 — 이게 §64 의 요구다.

EMAIL 은 지금 켜지 않는다
----------------------
SMTP provider 가 실제로 없으므로 채널을 IN_APP 하나만 쓴다. 여기서 EMAIL 을
넣으면 보내지지도 않을 QUEUED 행만 매일 쌓이고, 화면에는 "발송됨" 으로
보이게 된다.
"""

from alembic import op

revision = "M051"
down_revision = "M050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS` 를 쓴다. 이 마이그레이션이 두 번 돌 일은 없지만,
    # enum 값 추가는 되돌릴 수 없어서 방어해 두는 편이 낫다.
    op.execute("""
        ALTER TYPE enum_notification_kind ADD VALUE IF NOT EXISTS
        'DAILY_REPORT_READY'
    """)


def downgrade() -> None:
    # PostgreSQL 은 enum 값 제거를 지원하지 않는다.
    #
    # 타입을 다시 만들고 모든 참조 컬럼을 갈아끼우는 방법이 있지만, 그 과정에서
    # 이미 그 값을 쓰고 있는 알림 행을 어떻게 할지 정해야 한다(지울 것인가,
    # 다른 값으로 바꿀 것인가). 되돌리기 스크립트가 데이터를 판단하기 시작하면
    # 그 자체가 사고의 원인이 된다.
    #
    # 쓰이지 않는 enum 값이 남아 있는 것은 해가 없다. 그대로 둔다.
    pass
