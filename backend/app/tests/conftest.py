"""통합 테스트용 DB 인프라.

전략 (사용자 승인 — 2026-07-07):
  - dev DB(picksome_ops)와 완전히 분리된 전용 테스트 DB(`<dev_db>_test`)를 사용.
  - 세션 1회: DB 없으면 생성 + `alembic upgrade head` 로 스키마 적용 (idempotent).
  - 테스트 1건마다: connection.begin() 트랜잭션 안에서 SAVEPOINT 세션을 열어
    서비스 코드가 내부에서 `db.commit()` 을 호출해도 SAVEPOINT 만 커밋되고
    바깥 트랜잭션은 유지 → 테스트 종료 시 rollback 으로 완전 격리.
    (SQLAlchemy 2.0 `join_transaction_mode="create_savepoint"` 패턴)
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _to_psycopg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


# 테스트 DB 생성은 **운영 애플리케이션 역할이 아니라** 전용 역할이 한다.
# picksome(운영 role)에 CREATEDB 를 주면 애플리케이션이 임의로 DB 를 만들 수 있게 되어
# 최소권한 원칙이 깨진다. backend/scripts/bootstrap_roles.sql 참고.
_DEFAULT_TEST_ROLE = "picksome_test"
_DEFAULT_TEST_PASSWORD = "picksome_test"


def _test_database_url() -> str:
    """테스트 DB 접속 URL. 기본은 `<dev_db>_test` 를 테스트 전용 역할로 접속."""
    override = os.environ.get("TEST_DATABASE_URL")
    if override:
        return override
    base = get_settings().DATABASE_URL
    root, _, dbname = base.rpartition("/")
    # scheme://user:pw@host 부분에서 자격증명만 테스트 역할로 교체
    scheme, _, hostpart = root.partition("://")
    host = hostpart.rpartition("@")[2] if "@" in hostpart else hostpart
    return f"{scheme}://{_DEFAULT_TEST_ROLE}:{_DEFAULT_TEST_PASSWORD}@{host}/{dbname}_test"


def _admin_dsn_for_create(test_psycopg_url: str) -> str:
    """CREATE DATABASE 를 실행할 접속 문자열.

    TEST_ADMIN_DATABASE_URL 로 CI bootstrap 계정을 따로 줄 수 있다.
    없으면 테스트 URL 의 자격증명(= picksome_test)으로 postgres DB 에 붙는다.
    """
    admin_override = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if admin_override:
        return _to_psycopg_dsn(admin_override)
    return f"{test_psycopg_url.rpartition('/')[0]}/postgres"


@pytest.fixture(scope="session")
def _test_db_ready() -> Iterator[str]:
    test_url = _test_database_url()
    psycopg_url = _to_psycopg_dsn(test_url)
    dbname = psycopg_url.rpartition("/")[2]
    admin_dsn = _admin_dsn_for_create(psycopg_url)

    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            can_create = conn.execute(
                "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            if not (can_create and can_create[0]):
                pytest.skip(
                    "테스트 DB 를 만들 권한이 없습니다. "
                    "superuser 로 backend/scripts/bootstrap_roles.sql 을 1회 실행하세요.",
                    allow_module_level=True,
                )
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{dbname}"')
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"테스트 DB 서버에 접속할 수 없습니다({exc.__class__.__name__}). "
            "backend/scripts/bootstrap_roles.sql 로 picksome_test 역할을 준비하세요.",
            allow_module_level=True,
        )

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": test_url},
        check=True,
    )
    yield test_url


@pytest.fixture(scope="session")
def engine(_test_db_ready: str) -> Iterator[Engine]:
    eng = create_engine(_test_db_ready, future=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """트랜잭션 격리된 Session. 테스트 종료 시 모든 변경 rollback."""
    connection = engine.connect()
    trans = connection.begin()
    session_factory = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", future=True
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def secret_store() -> Iterator[object]:
    """테스트마다 빈 메모리 자격증명 저장소.

    왜 메모리인가
    -----------
    `db` fixture 는 트랜잭션을 열고 끝나면 롤백한다. 실제
    `LocalEncryptedSecretStore` 는 자기 세션을 따로 열기 때문에 그 롤백 바깥에
    쓴다 — 테스트끼리 자격증명이 새고, 순서에 따라 결과가 달라진다.

    암호화 구현 자체는 `test_secret_store.py` 가 테스트 세션을 직접 주입해서
    검증한다. 여기서는 "자격증명이 설정된 상태" 를 만들기만 하면 된다.

    운영에서 이 구현을 등록하려 하면 `set_secret_store` 가 막는다.
    """
    from app.core.secret_store import InMemorySecretStore, set_secret_store

    store = InMemorySecretStore()
    set_secret_store(store)
    yield store
    set_secret_store(None)
