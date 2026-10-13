#!/bin/sh
# 컨테이너 시작 시 DB 마이그레이션 후 서버 기동.
# AUTO_MIGRATE=false 로 끄면 알렘빅을 건너뜀 (수동 마이그레이션 시).
set -e

if [ "${AUTO_MIGRATE:-true}" = "true" ]; then
  echo "[entrypoint] alembic upgrade head ..."
  alembic upgrade head
  echo "[entrypoint] migration done."
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
