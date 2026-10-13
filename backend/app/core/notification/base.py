"""ChannelAdapter Protocol + 공통 데이터 객체.

D-281: 알림은 채널 어댑터로 추상화. MVP=EMAIL/IN_APP, Post-MVP=KAKAO/SLACK/TELEGRAM/WEBHOOK.
서비스 계층(`services/notification.py`)은 채널 종류를 모르고 어댑터에 위임만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

# enum_notification_channel 과 동기화. 추가 시 마이그레이션 + registry 보강 필요.
NotificationChannelCode = Literal[
    "EMAIL", "IN_APP", "KAKAO", "SLACK", "TELEGRAM", "WEBHOOK"
]


@dataclass(slots=True)
class NotificationPayload:
    """채널 무관 공통 메시지 페이로드.

    - kind: notification_kind enum (예: SETTLEMENT_LOCKED, EXPENSE_EVIDENCE_MISSING)
    - title/body: 채널별로 변환 (EMAIL=subject/body, IN_APP=벨 알림)
    - target_url: 클릭 시 이동할 화면 (예: `/settlement/2026-06`)
    - meta: 채널 어댑터가 자유롭게 사용 (예: SLACK block_kit)
    """

    kind: str
    title: str
    body: str | None = None
    target_url: str | None = None
    meta: dict[str, Any] | None = None


@dataclass(slots=True)
class Recipient:
    """수신자. user_id 는 IN_APP 저장용, email 은 EMAIL 채널용."""

    user_id: UUID
    org_id: UUID
    email: str | None = None
    display_name: str = ""


@dataclass(slots=True)
class DeliveryResult:
    """발송 결과.

    - SENT: 외부 전송 성공 (또는 IN_APP 저장 성공)
    - FAILED: 에러 발생 (error_message 에 사유)
    - QUEUED: SMTP 미설정 등으로 보류 — 추후 재발송
    - FEATURE_DISABLED: feature flag off
    """

    channel_code: NotificationChannelCode
    delivery_status: Literal["SENT", "FAILED", "QUEUED", "FEATURE_DISABLED"]
    error_message: str | None = None
    external_message_id: str | None = None


class ChannelAdapter(Protocol):
    """채널 어댑터 인터페이스.

    - code: NotificationChannelCode (registry key)
    - is_mvp_active: MVP 활성 채널이면 True (feature flag 검사 면제)
    """

    code: NotificationChannelCode
    is_mvp_active: bool

    def send(self, payload: NotificationPayload, recipient: Recipient) -> DeliveryResult: ...
