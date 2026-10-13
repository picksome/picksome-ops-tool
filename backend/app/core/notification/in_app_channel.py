"""IN_APP 채널 (MVP) — DB notification 1행 저장 + 벨 카운트."""

from __future__ import annotations

from app.core.notification.base import (
    ChannelAdapter,
    DeliveryResult,
    NotificationPayload,
    Recipient,
)


class InAppChannel(ChannelAdapter):
    """저장 자체는 NotificationService 가 수행. 어댑터는 marker 역할만."""

    code = "IN_APP"
    is_mvp_active = True

    def send(self, payload: NotificationPayload, recipient: Recipient) -> DeliveryResult:
        return DeliveryResult(channel_code="IN_APP", delivery_status="SENT")
