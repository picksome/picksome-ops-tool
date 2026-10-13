"""타임존 헬퍼.

CLAUDE.md Tech Stack: Timezone = Asia/Seoul.

- DB 저장 시각 = UTC (TIMESTAMPTZ).
- 사용자 노출 시각 = APP_TIMEZONE (`Asia/Seoul`, config 에서 변경 가능).
- 정산 `year_month`, 비용 발생일 등 "회계 일자" 계산은 항상 local 기준.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings

# 모듈 import 시점에 한 번만 ZoneInfo 인스턴스를 캐싱.
# get_settings() 는 lru_cache 되어 있으므로 재호출 비용 없음.
_TZ = ZoneInfo(get_settings().APP_TIMEZONE)


def now_utc() -> datetime:
    """현재 UTC 시각 (tz-aware). DB 저장·audit timestamp 용."""
    return datetime.now(tz=UTC)


def now_local() -> datetime:
    """현재 local 시각 (Asia/Seoul). 정산월/회계일 판정 용."""
    return datetime.now(tz=_TZ)


def to_local(dt: datetime) -> datetime:
    """tz-naive 입력은 UTC 로 간주 후 local 로 변환.

    예: DB 에서 읽어온 naive datetime → 화면 노출용 KST 로 변환.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TZ)
