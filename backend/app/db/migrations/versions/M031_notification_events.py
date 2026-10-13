"""M031 — IN_APP 알림 이벤트 + 중복 방지.

문제
----
알림 인프라는 있는데 **아무도 부르지 않았다.** `notification` 테이블과 채널
어댑터(`core/notification/*`)는 D-281 에서 만들었지만, 승인이 거절돼도 월 마감이
막혀도 worker 가 죽어도 알림이 생기지 않았다. 화면(`/notifications`)은
`MOCK_NOTIFICATIONS` 를 보고 있었다.

무엇을 고치나
-----------
1. `enum_notification_kind` 에 실제 운영 사건 9종을 추가한다.
   기존 7종은 "정산 마감일" 같은 일정 알림이라 지금 필요한 사건과 결이 다르다.

2. `dedupe_key` 를 둔다. **같은 사건으로 알림이 반복 생성되면 알림함이 못 쓰게 된다.**
   worker 가 5분마다 도는데 "월 마감 차단" 을 매번 새로 만들면 하루에 288건이다.
   사용자는 그 시점부터 벨을 보지 않는다.

왜 UNIQUE 인덱스인가
------------------
애플리케이션에서 "있으면 건너뛰기" 로만 막으면 동시 실행에서 뚫린다.
worker 두 개가 같은 순간에 같은 사건을 보면 둘 다 "없다" 를 읽고 둘 다 넣는다.
DB 제약이 마지막 방어선이다.

수신자별로 유일해야 한다 — 같은 사건을 관리자 3명에게 보내는 것은 중복이 아니다.
채널도 키에 넣는다. IN_APP 과 EMAIL 은 별개 행이고 둘 다 있어야 정상이다.

`deleted_at IS NULL` 을 조건에 넣는 이유: 지운 알림 때문에 같은 사건이 영원히
다시 생기지 못하면, 사용자가 알림을 지운 순간 그 사건을 다시는 못 보게 된다.
"""

from alembic import op

revision = "M031"
down_revision = "M030"
branch_labels = None
depends_on = None


# 실제 운영 사건. 각 값이 왜 필요한지는 services/notification_events.py 에 있다.
_NEW_KINDS = (
    "APPROVAL_REQUESTED",
    "APPROVAL_REJECTED",
    "APPROVAL_APPLY_FAILED",
    "MONTH_CLOSE_BLOCKED",
    "MONTH_CLOSE_READY",
    "PAYOUT_PENDING",
    "ATTACHMENT_DELETE_FAILED",
    "CHANNEL_PRICE_PUBLISH_FAILED",
    "WORKER_FAILURE",
)


def upgrade() -> None:
    # enum 라벨 추가는 되돌릴 수 없다(아래 downgrade 주석 참고).
    # IF NOT EXISTS 로 재실행 안전하게 만든다.
    for kind in _NEW_KINDS:
        op.execute(f"ALTER TYPE enum_notification_kind ADD VALUE IF NOT EXISTS '{kind}'")

    op.execute("ALTER TABLE notification ADD COLUMN dedupe_key TEXT")

    op.execute("""
        COMMENT ON COLUMN notification.dedupe_key IS
        '같은 사건의 반복 생성을 막는 키. NULL 이면 중복 검사를 하지 않는다(사람이 직접 보낸 알림 등).'
    """)

    # 부분 UNIQUE — 키가 있는 행만 검사한다.
    #
    # 이것은 성능 최적화가 아니라 **정합성 제약**이다. 조회 성능용 인덱스는
    # 여기서 추가하지 않는다 — `ix_notification_unread`(M001)가 이미
    # (recipient_user_id, read_at) WHERE read_at IS NULL 을 덮고 있고,
    # 근거 없이 겹치는 인덱스를 늘리면 쓰기 비용만 늘어난다.
    op.execute("""
        CREATE UNIQUE INDEX uq_notification_dedupe
            ON notification (recipient_user_id, channel_code, dedupe_key)
         WHERE dedupe_key IS NOT NULL AND deleted_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_notification_dedupe")
    op.execute("ALTER TABLE notification DROP COLUMN IF EXISTS dedupe_key")
    # ⚠️ enum 라벨은 **되돌리지 않는다.**
    #
    # PostgreSQL 은 enum 값 제거를 지원하지 않는다(타입을 새로 만들고 모든 컬럼을
    # 옮겨야 한다). 그 과정에서 그 값을 쓰는 행이 있으면 데이터를 잃는다.
    # 라벨이 남아 있는 것은 무해하다 — 쓰지 않으면 그만이다.
    # 여기서 억지로 지우면 downgrade 가 데이터 손실 경로가 된다.
