"""애플리케이션 설정 (CLAUDE.md Tech Stack, docs/25_devops.md).

환경 변수 우선순위:
  실제 env > .env 파일 > 코드 default

CLAUDE.md 원칙 반영:
  - Asia/Seoul + KRW 고정
  - 민감정보(JWT_SECRET/SMTP_PASSWORD)는 default 값이 노출되지 않도록 env 강제
  - FEATURE_* 플래그로 MVP/Post-MVP 채널 분리 (D-281)
  - SETTLEMENT_REOPEN_LIMIT_DEFAULT 은 org override 가능 (D-285)
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py → parents[2] == 프로젝트 루트 (.env 위치).
# 상대경로("`.env`")로 두면 실행 cwd(예: backend/ 안에서 uvicorn 실행)에 따라
# .env 를 못 찾고 DATABASE_URL 등이 조용히 코드 default 로 폴백하는 문제가 있었음.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """env 기반 런타임 설정.

    `lru_cache` 된 `get_settings()` 로 호출하여 single instance 유지.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # 앱 메타 (CLAUDE.md Tech Stack)
    # ------------------------------------------------------------------
    APP_ENV: str = "dev"  # dev / staging / prod
    APP_TIMEZONE: str = "Asia/Seoul"
    APP_CURRENCY: str = "KRW"

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://picksome:picksome@localhost:5432/picksome",
        description="Postgres DSN (psycopg v3)",
    )

    # ------------------------------------------------------------------
    # 인증 (core/security.py 가 사용)
    # ------------------------------------------------------------------
    JWT_SECRET: str = Field(default="change-me-in-env", min_length=8)
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 12  # access token 12시간

    # ------------------------------------------------------------------
    # SMTP (D-281 EMAIL 채널 — core/notification/channels/email_smtp.py)
    # SMTP_HOST 미설정 시 EMAIL 채널 비활성.
    # ------------------------------------------------------------------
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str = "noreply@picksome.local"

    # ------------------------------------------------------------------
    # 환율 자동화 (M008, docs/27 D-288) — 한국수출입은행 Open API
    # KOREAEXIM_API_KEY 미설정 시 자동 조회 비활성, 수동 입력(exchange_rate.upsert_manual_rate)만 가능
    # (SMTP_HOST 미설정 패턴과 동일).
    # ------------------------------------------------------------------
    KOREAEXIM_API_KEY: str | None = None
    KOREAEXIM_API_BASE_URL: str = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"

    # ------------------------------------------------------------------
    # Feature flags
    #   D-282: EXTERNAL_ACCOUNTANT 라우트는 Post-MVP — 기본 OFF
    #   D-281: KAKAO/SLACK/TELEGRAM/WEBHOOK 어댑터는 Post-MVP — 기본 OFF
    # ------------------------------------------------------------------
    FEATURE_EXTERNAL_ACCOUNTANT: bool = False
    FEATURE_NOTIFICATION_KAKAO: bool = False
    FEATURE_NOTIFICATION_SLACK: bool = False
    FEATURE_NOTIFICATION_TELEGRAM: bool = False
    FEATURE_NOTIFICATION_WEBHOOK: bool = False

    # ------------------------------------------------------------------
    # 정산 정책 (D-285)
    #   organization.reopen_limit_override 가 있으면 우선 적용.
    # ------------------------------------------------------------------
    SETTLEMENT_REOPEN_LIMIT_DEFAULT: int = 3

    # ------------------------------------------------------------------
    # 증빙 파일 저장소 (M027)
    #   BACKEND=local 만 구현돼 있다. S3 호환으로 갈 때 여기에 키를 추가하고
    #   core/attachment_storage.get_storage() 에서 분기한다 — 호출부는 그대로다.
    #   운영에서 local 을 쓰면 인스턴스마다 다른 디스크를 보게 된다.
    # ------------------------------------------------------------------
    ATTACHMENT_STORAGE_BACKEND: str = "local"
    ATTACHMENT_STORAGE_DIR: str = "./var/attachments"

    # ------------------------------------------------------------------
    # 자격증명 저장소 (M042, core/secret_store.py)
    #
    #   무인 동기화를 하려면 프로그램이 API 인증정보를 다시 쓸 수 있어야 한다.
    #   암호문은 DB(credential_secret)에, **키는 여기에** 둔다. 둘을 같은
    #   백업 번들에 넣으면 분리가 무의미해진다 — docs/60.
    #
    #   키가 없으면 자격증명 기능이 CREDENTIAL_STORE_NOT_CONFIGURED 로
    #   거부된다. 평문으로 대신 저장하는 fallback 은 없다.
    #
    #   새 키: python -c "from app.core.secret_store import generate_master_key as g; print(g())"
    #   교체 시 옛 키를 CREDENTIAL_RETIRED_KEYS 에 남겨야 기존 암호문을 읽는다.
    # ------------------------------------------------------------------
    CREDENTIAL_STORE_BACKEND: str = "local"
    CREDENTIAL_MASTER_KEY: str | None = None
    CREDENTIAL_RETIRED_KEYS: str = ""

    # ------------------------------------------------------------------
    # AI / MCP 접근 (M044, app/mcp)
    #
    #   **선택 모듈이다.** 꺼져 있어도 자동화·리포트·정산은 그대로 돈다.
    #   켜면 서비스 계정 토큰을 가진 외부 에이전트가 읽기·제안 tool 을 쓸 수
    #   있다. 끄면 `/api/v1/mcp/*` 가 404 다 — 경로가 없는 것처럼 보인다.
    # ------------------------------------------------------------------
    MCP_ENABLED: bool = False

    # ------------------------------------------------------------------
    # 로깅 / 모니터링
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str | None = None

    # ------------------------------------------------------------------
    # 마이그레이션
    #   AUTO_MIGRATE=false 시 entrypoint 에서 alembic 건너뜀.
    # ------------------------------------------------------------------
    AUTO_MIGRATE: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스당 한 번만 인스턴스화. FastAPI Depends 와 직접 호출 모두 안전."""
    return Settings()
