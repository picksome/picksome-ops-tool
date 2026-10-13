"""알림 라우터 (D-281, docs/22_api_spec.md §9).

MVP 채널: EMAIL + IN_APP. Post-MVP: KAKAO/SLACK/TELEGRAM/WEBHOOK
  → 새 채널은 core/notification/registry.py 에 ChannelAdapter 등록만 하면 확장.

endpoints (router — 모든 인증 사용자):
  GET  /notifications                       — 본인 알림 목록 (only_unread 필터)
  GET  /notifications/unread-count          — 미읽음 카운트 (헤더 벨 뱃지용)
  POST /notifications/{id}/read             — 단건 읽음 처리
  POST /notifications/mark-all-read         — 전체 읽음 처리
  GET  /notifications/channels              — 활성 채널 메타 정보

endpoints (prefs_router):
  GET   /admin/notification-prefs           — 본인 환경설정 조회
  PATCH /admin/notification-prefs           — 본인 환경설정 갱신 (upsert)

라우터에서 자신의 user_id 로만 조회 — IDOR 차단을 service 가 user_id 필터 강제."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.response import envelope
from app.deps import CurrentUser, get_current_user, get_db
from app.domain.notification import NotificationPrefUpdate
from app.services.notification import (
    get_prefs,
    list_active_channels,
    list_my_notifications,
    mark_all_read,
    mark_read,
    unread_count,
    upsert_prefs,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def list_(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    only_unread: bool = Query(False),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_my_notifications(
        db, user_id=user.user_id, limit=limit, offset=offset, only_unread=only_unread
    )
    return envelope(
        [i.model_dump() for i in items],
        meta={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/unread-count")
def unread(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return envelope({"unread": unread_count(db, user_id=user.user_id)})


@router.post("/{notification_id}/read", status_code=204, response_model=None)
def read_one(
    notification_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    mark_read(db, user_id=user.user_id, notification_id=notification_id)


@router.post("/mark-all-read")
def read_all(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    n = mark_all_read(db, user_id=user.user_id)
    return envelope({"marked": n})


@router.get("/channels")
def channels(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    items = list_active_channels(db)
    return envelope([i.model_dump() for i in items])


# ---------------------------------------------------------------------------
# 본인 환경설정 (admin/notification-prefs — 본인만)
# ---------------------------------------------------------------------------

prefs_router = APIRouter(prefix="/admin/notification-prefs", tags=["notifications.prefs"])


@prefs_router.get("")
def get_my_prefs(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return envelope(get_prefs(db, user_id=user.user_id).model_dump())


@prefs_router.patch("")
def patch_my_prefs(
    body: NotificationPrefUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    out = upsert_prefs(db, user_id=user.user_id, body=body)
    return envelope(out.model_dump())
