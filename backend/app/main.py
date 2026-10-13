"""FastAPI 애플리케이션 진입점 (docs/22_api_spec.md §1).

구성:
  - prefix `/api/v1` 로 api_v1_router 일괄 마운트
  - 표준 envelope 응답 핸들러 4종 등록
  - CORS: dev 단계 localhost:3000 (Next.js dev server) 만 허용
  - GET /api/health  : liveness probe (Next.js rewrite 통과용)
  - GET /healthz     : Docker healthcheck 전용 (DB ping 포함)
  - APScheduler      : 정산·부가세·마진·정책·백업 5개 잡 (Asia/Seoul)
  - Sentry           : SENTRY_DSN 설정 시 에러 자동 캡처

OpenAPI:
  /api/docs (Swagger), /api/openapi.json
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.api.v1 import api_v1_router
from app.config import get_settings
from app.core.exceptions import DomainError
from app.core.response import (
    domain_error_handler,
    http_exception_handler,
    internal_error_handler,
    validation_error_handler,
)
from app.db.session import SessionLocal
from app.services.scheduler import create_scheduler

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()

    # Sentry 초기화 (선택)
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                traces_sample_rate=0.1,
                send_default_pii=False,
            )
            _log.info("Sentry initialized.")
        except ImportError:
            _log.warning("sentry-sdk not installed — SENTRY_DSN ignored.")

    # 로그 레벨 적용
    logging.getLogger().setLevel(settings.LOG_LEVEL.upper())

    # APScheduler 시작
    scheduler = create_scheduler()
    scheduler.start()
    _log.info("Scheduler started (%d jobs).", len(scheduler.get_jobs()))

    yield

    scheduler.shutdown(wait=False)
    _log.info("Scheduler stopped.")


def create_app() -> FastAPI:
    settings = get_settings()

    # 구현된 외부 커넥터를 레지스트리에 올린다. 이걸 빠뜨리면 실제로 되는
    # 동기화가 화면에서 "연동 미구현" 으로 보인다. worker 프로세스는
    # `run_workers` 가 따로 부른다 — 그쪽은 이 함수를 지나지 않는다.
    from app.connectors.bootstrap import register_builtin

    register_builtin()
    app = FastAPI(
        title="PICKSOME OPS TOOL API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, internal_error_handler)

    app.include_router(api_v1_router, prefix="/api/v1")

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.APP_ENV}

    @app.get("/healthz", tags=["meta"])
    def healthz() -> JSONResponse:
        """Docker healthcheck 전용 — DB ping 포함."""
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            _log.error("healthz DB ping failed: %s", exc)
            return JSONResponse({"status": "error", "detail": "db unreachable"}, status_code=503)

    return app


app = create_app()
