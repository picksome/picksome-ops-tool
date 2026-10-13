"""알림 도메인 schemas (D-281).

MVP 활성 채널: EMAIL, IN_APP.
Post-MVP 채널 (KAKAO/SLACK/TELEGRAM/WEBHOOK) 은 FEATURE_NOTIFICATION_* 플래그로 활성화.

kind: SETTLEMENT_DEADLINE / VAT_DEADLINE / MARGIN_DROP / STOCKOUT_RISK /
      POLICY_REVIEW / ACCOUNTANT_COMMENT / SYSTEM

NotificationPref:
- kind_settings_json:    {kind: {enabled: bool, channels: [code, ...]}}
- channel_settings_json: {code: {enabled: bool, ...채널별 설정}}
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

NotificationKind = str  # enum_notification_kind (DB enum 으로 강제)
NotificationChannelCode = str  # enum_notification_channel_code


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: NotificationKind
    title: str
    body: str | None = None
    channel_code: NotificationChannelCode
    target_url: str | None = None
    sent_at: datetime | None = None
    delivery_status: str
    read_at: datetime | None = None
    meta_json: dict[str, Any] | None = None


class UnreadCountOut(BaseModel):
    unread: int


class ChannelMetaOut(BaseModel):
    code: NotificationChannelCode
    display_name: str
    is_mvp_active: bool
    sort_order: int


class NotificationPrefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind_settings_json: dict[str, Any]
    channel_settings_json: dict[str, Any]


class NotificationPrefUpdate(BaseModel):
    kind_settings_json: dict[str, Any] | None = Field(default=None)
    channel_settings_json: dict[str, Any] | None = Field(default=None)
