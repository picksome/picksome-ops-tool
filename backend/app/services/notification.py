"""알림 서비스 (D-281).

본 서비스의 역할 2가지:
1) **사용자용 조회/읽음 처리** (notifications 라우터에서 호출)
2) **시스템용 발송** `send_notification(...)` — 다른 도메인 서비스 (정산/마진 등) 가 호출.
   채널 어댑터 registry 를 통해 MVP 활성 채널만 사용.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.ids import new_id
from app.core.notification import (
    NotificationPayload,
    Recipient,
    get_channel,
    get_mvp_active_channels,
)
from app.domain.notification import (
    ChannelMetaOut,
    NotificationOut,
    NotificationPrefOut,
    NotificationPrefUpdate,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# 본인 알림 조회 / 읽음 처리
# ---------------------------------------------------------------------------

_NOTI_SELECT = """
  SELECT id, kind, title, body, channel_code, target_url,
         sent_at, delivery_status, read_at, meta_json
    FROM notification
"""


def list_my_notifications(
    db: Session, *, user_id: UUID, limit: int = 50, offset: int = 0, only_unread: bool = False
) -> tuple[list[NotificationOut], int]:
    where = ["recipient_user_id = :u", "deleted_at IS NULL"]
    if only_unread:
        where.append("read_at IS NULL")
    where_sql = " AND ".join(where)

    count_row = db.execute(
        text(f"SELECT COUNT(*) AS c FROM notification WHERE {where_sql}"),
        {"u": str(user_id)},
    ).first()
    total = int(count_row.c) if count_row else 0

    rows = db.execute(
        text(f"""
          {_NOTI_SELECT}
           WHERE {where_sql}
           ORDER BY COALESCE(sent_at, created_at) DESC
           LIMIT :limit OFFSET :offset
        """),
        {"u": str(user_id), "limit": limit, "offset": offset},
    ).all()
    return [NotificationOut(**r._mapping) for r in rows], total


def unread_count(db: Session, *, user_id: UUID) -> int:
    row = db.execute(
        text("""
          SELECT COUNT(*) AS c FROM notification
           WHERE recipient_user_id = :u AND read_at IS NULL AND deleted_at IS NULL
        """),
        {"u": str(user_id)},
    ).first()
    return int(row.c) if row else 0


def mark_read(db: Session, *, user_id: UUID, notification_id: UUID) -> None:
    res = db.execute(
        text("""
          UPDATE notification
             SET read_at = :now
           WHERE id = :i AND recipient_user_id = :u AND read_at IS NULL
        """),
        {"now": _now(), "i": str(notification_id), "u": str(user_id)},
    )
    if res.rowcount == 0:
        # 이미 읽었거나 본인 알림이 아님 — 본인 알림이면 (이미 읽음) silent, 아니면 404.
        exists = db.execute(
            text("SELECT 1 FROM notification WHERE id = :i AND recipient_user_id = :u"),
            {"i": str(notification_id), "u": str(user_id)},
        ).first()
        if not exists:
            raise NotFoundError("알림을 찾을 수 없습니다.")
    db.commit()


def mark_all_read(db: Session, *, user_id: UUID) -> int:
    res = db.execute(
        text("""
          UPDATE notification
             SET read_at = :now
           WHERE recipient_user_id = :u AND read_at IS NULL
        """),
        {"now": _now(), "u": str(user_id)},
    )
    db.commit()
    return int(res.rowcount or 0)


# ---------------------------------------------------------------------------
# 채널 메타 / 사용자 환경설정
# ---------------------------------------------------------------------------


def list_active_channels(db: Session) -> list[ChannelMetaOut]:
    # 어댑터 레지스트리 기준. notification_channel 메타 테이블 은 표시정보 (display_name).
    rows = db.execute(
        text("""
          SELECT code, display_name, is_mvp_active, sort_order
            FROM notification_channel
           ORDER BY sort_order, code
        """)
    ).all()
    return [ChannelMetaOut(**r._mapping) for r in rows]


def get_prefs(db: Session, *, user_id: UUID) -> NotificationPrefOut:
    row = db.execute(
        text("""
          SELECT kind_settings_json, channel_settings_json
            FROM notification_pref
           WHERE user_id = :u AND deleted_at IS NULL
        """),
        {"u": str(user_id)},
    ).first()
    if row is None:
        # 미설정 → 기본값 (모든 종류 활성, EMAIL + IN_APP 활성)
        return NotificationPrefOut(
            kind_settings_json={},
            channel_settings_json={"EMAIL": True, "IN_APP": True},
        )
    return NotificationPrefOut(**row._mapping)


def upsert_prefs(
    db: Session, *, user_id: UUID, body: NotificationPrefUpdate
) -> NotificationPrefOut:
    current = get_prefs(db, user_id=user_id)
    new_kinds = body.kind_settings_json if body.kind_settings_json is not None else current.kind_settings_json
    new_channels = (
        body.channel_settings_json
        if body.channel_settings_json is not None
        else current.channel_settings_json
    )
    db.execute(
        text("""
          INSERT INTO notification_pref (
            id, user_id, kind_settings_json, channel_settings_json,
            created_by, updated_by
          ) VALUES (
            :id, :u, CAST(:k AS JSONB), CAST(:c AS JSONB), :u, :u
          )
          ON CONFLICT (user_id) DO UPDATE
             SET kind_settings_json = CAST(:k AS JSONB),
                 channel_settings_json = CAST(:c AS JSONB),
                 updated_at = :now,
                 updated_by = EXCLUDED.updated_by
        """),
        {
            "id": str(new_id()),
            "u": str(user_id),
            "k": _json_dump(new_kinds),
            "c": _json_dump(new_channels),
            "now": _now(),
        },
    )
    db.commit()
    return get_prefs(db, user_id=user_id)


def _json_dump(v: dict[str, Any]) -> str:
    import json

    return json.dumps(v, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 시스템 발송 (다른 도메인 서비스에서 호출)
# ---------------------------------------------------------------------------


def send_notification(
    db: Session,
    *,
    org_id: UUID,
    recipient_user_id: UUID,
    recipient_email: str | None,
    recipient_name: str,
    kind: str,
    title: str,
    body: str | None = None,
    target_url: str | None = None,
    meta: dict[str, Any] | None = None,
    channels: list[str] | None = None,
) -> list[UUID]:
    """선택된 채널 (또는 MVP 활성 채널 전체) 로 알림 발송.

    의사코드:
        for code in channels or get_mvp_active_channels():
            adapter = get_channel(code)         # FEATURE flag 검증 포함
            result  = adapter.send(payload, recipient)
            INSERT notification (...delivery_status=result.delivery_status)

    각 채널에 대해 notification row 1행 (멀티채널 시 N 행).
    IN_APP 은 어댑터가 marker 역할만 — row 저장 자체가 "도착" 의미.
    EMAIL 은 SMTP 미설정 환경에서 QUEUED 로 저장 → 재발송 배치(Post-MVP)에서 처리.
    """
    selected = channels or [a.code for a in get_mvp_active_channels()]
    payload = NotificationPayload(kind=kind, title=title, body=body, target_url=target_url, meta=meta)
    recipient = Recipient(
        user_id=recipient_user_id, org_id=org_id, email=recipient_email, display_name=recipient_name
    )

    created_ids: list[UUID] = []
    for code in selected:
        adapter = get_channel(code)  # type: ignore[arg-type]
        result = adapter.send(payload, recipient)
        noti_id = new_id()
        db.execute(
            text("""
              INSERT INTO notification (
                id, org_id, recipient_user_id, kind, title, body,
                channel_code, target_url, sent_at, delivery_status,
                error_message, meta_json
              ) VALUES (
                :id, :o, :u, CAST(:k AS enum_notification_kind), :t, :b,
                CAST(:c AS enum_notification_channel_code), :tu, :now, :st,
                :err, CAST(:meta AS JSONB)
              )
            """),
            {
                "id": str(noti_id),
                "o": str(org_id),
                "u": str(recipient_user_id),
                "k": kind,
                "t": title,
                "b": body,
                "c": result.channel_code,
                "tu": target_url,
                "now": _now() if result.delivery_status == "SENT" else None,
                "st": result.delivery_status,
                "err": result.error_message,
                "meta": _json_dump(meta) if meta else None,
            },
        )
        created_ids.append(noti_id)
    db.commit()
    return created_ids
