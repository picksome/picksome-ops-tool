"""EMAIL 채널 (MVP).

SMTP 미설정 환경에서는 QUEUED 로 표시 (로그/DB 만 기록).
실제 SMTP 전송은 DevOps 환경 변수 SMTP_* 가 설정될 때 활성화.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings
from app.core.notification.base import (
    ChannelAdapter,
    DeliveryResult,
    NotificationPayload,
    Recipient,
)

_log = logging.getLogger(__name__)


class EmailChannel(ChannelAdapter):
    code = "EMAIL"
    is_mvp_active = True

    def send(self, payload: NotificationPayload, recipient: Recipient) -> DeliveryResult:
        settings = get_settings()

        if recipient.email is None:
            return DeliveryResult(
                channel_code="EMAIL",
                delivery_status="FAILED",
                error_message="수신자 이메일 없음",
            )

        if not settings.SMTP_HOST or not settings.SMTP_USER:
            _log.info("SMTP 미설정 — EMAIL 발송 큐잉 only (kind=%s)", payload.kind)
            return DeliveryResult(channel_code="EMAIL", delivery_status="QUEUED")

        msg = EmailMessage()
        msg["Subject"] = payload.title
        msg["From"] = settings.SMTP_FROM
        msg["To"] = recipient.email
        body = payload.body or ""
        if payload.target_url:
            body += f"\n\n바로가기: {payload.target_url}"
        msg.set_content(body)

        try:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as s:
                s.starttls()
                if settings.SMTP_PASSWORD:
                    s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                s.send_message(msg)
            return DeliveryResult(channel_code="EMAIL", delivery_status="SENT")
        except Exception as e:  # noqa: BLE001
            _log.exception("EMAIL 발송 실패")
            return DeliveryResult(
                channel_code="EMAIL", delivery_status="FAILED", error_message=str(e)[:200]
            )
