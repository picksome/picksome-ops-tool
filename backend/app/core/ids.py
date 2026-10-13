"""UUID 생성기 (D-283).

설계 결정:
- 모든 PK/FK 는 UUID (사용자 노출 ID 는 `business_key` 컬럼 별도)
- 앱 측 기본: UUID v7 (시간순 정렬 가능 — 인덱스 친화적)
- 라이브러리 미설치 환경 fallback: uuid4 (랜덤)
- DB 측 DEFAULT 는 `gen_random_uuid()` (pgcrypto) 로 별도 보장 →
  Python 미경유 INSERT (예: raw SQL, seed) 에서도 PK 누락 방지.
"""

from uuid import UUID, uuid4

try:
    from uuid7 import uuid7 as _uuid7  # type: ignore[import-not-found]

    def new_id() -> UUID:
        """UUID v7 (시간순) 발급. 인덱스 locality 확보."""
        return _uuid7()
except Exception:  # pragma: no cover
    # uuid7 패키지가 없으면 uuid4 fallback.
    # 운영환경에서는 pyproject.toml 의 uuid7 의존성을 강제.
    def new_id() -> UUID:
        return uuid4()
