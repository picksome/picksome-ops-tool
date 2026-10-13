"""알림 채널 어댑터 (D-281).

MVP: EMAIL, IN_APP.
Post-MVP: KAKAO, SLACK, TELEGRAM, WEBHOOK (FEATURE_NOTIFICATION_* 플래그로 활성화).
"""

from app.core.notification.base import (
    ChannelAdapter,
    DeliveryResult,
    NotificationPayload,
    Recipient,
)
from app.core.notification.email_channel import EmailChannel
from app.core.notification.in_app_channel import InAppChannel
from app.core.notification.registry import (
    get_channel,
    get_mvp_active_channels,
    register_channel,
)

__all__ = [
    "ChannelAdapter",
    "DeliveryResult",
    "EmailChannel",
    "InAppChannel",
    "NotificationPayload",
    "Recipient",
    "get_channel",
    "get_mvp_active_channels",
    "register_channel",
]
