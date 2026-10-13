"""마이그레이션 대상 안전장치.

이 파일이 고정하는 것
------------------
1. `ALEMBIC_DATABASE_URL` 이 실제로 대상을 바꾼다
2. 임시 DB 이름이 아니면 파괴적 검증을 **거부**한다
3. 운영 환경에서는 **거부**한다
4. 개발 메인 DB 는 **거부**한다
5. 비밀번호가 어디에도 안 나온다

왜 단위 테스트로 고정하나
--------------------
2026-08-11 에 `ALEMBIC_DATABASE_URL` 이 무시되는 줄 모르고 개발 DB 를 내렸다.
"고쳤다" 로 끝내면 다음에 누군가 env.py 를 정리하다 다시 무시하게 만들 수 있고,
그 사실은 **사고가 나야** 드러난다.
"""

from __future__ import annotations

import pytest

from app.db.migration_target import (
    UnsafeMigrationTargetError,
    assert_safe_for_destructive,
    describe_target,
    is_scratch_name,
)

DEV = "postgresql+psycopg://picksome:secret@localhost:5432/picksome_ops"
SCRATCH = "postgresql+psycopg://picksome_test:pw@localhost:5432/picksome_migcheck_ab12"


def test_scratch_name_recognised():
    assert is_scratch_name("picksome_migcheck")
    assert is_scratch_name("picksome_migcheck_ab12")
    assert is_scratch_name("foo_migration_test")
    # 개발/운영 DB 는 절대 scratch 로 보이면 안 된다.
    assert not is_scratch_name("picksome_ops")
    assert not is_scratch_name("picksome_ops_test")
    # 접두어만 같은 이름에 속지 않는다 — 패턴은 **끝**을 본다.
    assert not is_scratch_name("migcheck_production")


def test_describe_extracts_target_without_secrets():
    t = describe_target(SCRATCH, app_env="dev", development_url=DEV)
    assert (t.host, t.port, t.database) == (
        "localhost", 5432, "picksome_migcheck_ab12")
    assert t.is_scratch is True
    assert t.is_development_main is False
    # 비밀번호가 어디에도 없다 — describe() 는 사람과 로그가 함께 본다.
    assert "pw" not in t.describe()
    assert "secret" not in t.describe()


def test_scratch_target_is_allowed():
    t = describe_target(SCRATCH, app_env="dev", development_url=DEV)
    assert_safe_for_destructive(t)  # 예외가 없어야 한다


def test_development_main_is_rejected():
    """개발 메인 DB 를 되돌리기 검증 대상으로 삼을 수 없다.

    이게 2026-08-11 에 실제로 일어난 일이다.
    """
    t = describe_target(DEV, app_env="dev", development_url=DEV)
    with pytest.raises(UnsafeMigrationTargetError, match="개발 메인"):
        assert_safe_for_destructive(t)


@pytest.mark.parametrize("env", ["prod", "production", "staging", "PROD"])
def test_production_is_rejected_even_when_name_looks_safe(env: str):
    """이름이 scratch 여도 운영 환경이면 거부한다.

    순서가 중요하다 — 이름 검사를 먼저 하면 `_migcheck` 로 끝나는 운영 DB 가
    통과한다.
    """
    t = describe_target(SCRATCH, app_env=env, development_url=DEV)
    with pytest.raises(UnsafeMigrationTargetError, match="APP_ENV"):
        assert_safe_for_destructive(t)


def test_unknown_database_is_rejected():
    """이름이 scratch 가 아니면 거부한다. 모르면 멈춘다(fail-closed)."""
    other = "postgresql+psycopg://u:p@db.example.com:5432/customer_prod"
    t = describe_target(other, app_env="dev", development_url=DEV)
    with pytest.raises(UnsafeMigrationTargetError, match="임시 검증용"):
        assert_safe_for_destructive(t)


def test_env_py_honours_alembic_database_url(monkeypatch):
    """env.py 가 환경변수를 **실제로** 반영하는지.

    문서나 주석이 아니라 코드를 읽어 확인한다. 이 연결이 끊어졌던 것이
    사고의 원인이었다.
    """
    from pathlib import Path

    env_py = (
        Path(__file__).resolve().parents[2] / "db" / "migrations" / "env.py"
    ).read_text(encoding="utf-8")

    # 환경변수를 읽고, 그 값이 sqlalchemy.url 로 들어가야 한다.
    assert "ALEMBIC_DATABASE_URL" in env_py
    assert "set_main_option" in env_py
    # override 가 settings 보다 **먼저** 오는지 — 순서가 뒤집히면 무시된다.
    override_pos = env_py.index("ALEMBIC_DATABASE_URL")
    url_assign = env_py.index('config.set_main_option("sqlalchemy.url"')
    assert override_pos < url_assign
