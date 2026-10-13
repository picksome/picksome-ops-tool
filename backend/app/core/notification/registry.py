"""채널 어댑터 레지스트리.

MVP 활성 채널: EMAIL, IN_APP.
Post-MVP: FEATURE_NOTIFICATION_* 플래그가 True 인 채널만 등록 가능.
"""

from __future__ import annotations

from app.config import get_settings
from app.core.exceptions import FeatureDisabledError
from app.core.notification.base import ChannelAdapter, NotificationChannelCode
from app.core.notification.email_channel import EmailChannel
from app.core.notification.in_app_channel import InAppChannel

# 프로세스 단위 싱글톤. 테스트에서 _REGISTRY.clear() 후 재등록 가능.
_REGISTRY: dict[str, ChannelAdapter] = {}


def register_channel(adapter: ChannelAdapter) -> None:
    """채널 어댑터 등록. 같은 code 재등록은 덮어쓰기 (Post-MVP 외부 확장 지원)."""
    _REGISTRY[adapter.code] = adapter


def _bootstrap() -> None:
    """최초 호출 시 MVP 채널 자동 등록. 멱등."""
    if _REGISTRY:
        return
    register_channel(EmailChannel())
    register_channel(InAppChannel())
    # Post-MVP slots: feature flag 활성화 시 외부에서 register_channel(...)


def get_channel(code: NotificationChannelCode) -> ChannelAdapter:
    """채널 코드로 어댑터 조회. 미등록/비활성이면 FeatureDisabledError."""
    _bootstrap()
    adapter = _REGISTRY.get(code)
    if adapter is None:
        raise FeatureDisabledError(f"채널 {code} 미등록")
    if not adapter.is_mvp_active:
        # MVP 외 채널은 FEATURE_NOTIFICATION_<CODE> 플래그가 True 여야 통과.
        settings = get_settings()
        flag = f"FEATURE_NOTIFICATION_{code}"
        if not getattr(settings, flag, False):
            raise FeatureDisabledError(f"채널 {code} 비활성 ({flag}=false)")
    return adapter


def get_mvp_active_channels() -> list[ChannelAdapter]:
    """notification 발송 fanout 대상 = MVP 활성 채널 전체."""
    _bootstrap()
    return [a for a in _REGISTRY.values() if a.is_mvp_active]
