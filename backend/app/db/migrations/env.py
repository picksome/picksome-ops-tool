import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import models so Alembic autogenerate (when used) can detect them.
# Current MVP migrations are hand-written SQL — autogenerate is not used,
# but importing here keeps metadata available for downstream tooling.
import app.db.models  # noqa: F401
from app.config import get_settings
from app.db.base import Base

config = context.config

# 마이그레이션이 어느 DB 를 향하는지는 **명시적으로 바꿀 수 있어야 한다.**
#
# 예전에는 무조건 `settings.DATABASE_URL` 이었다. 그래서 임시 DB 를 지정했다고
# 믿고 `downgrade` 를 돌리면 조용히 **개발 DB** 가 내려갔다. 실제로 그렇게
# 당했다 — 되돌리기 검증을 하려던 명령이 검증 대상이 아닌 DB 를 건드렸다.
#
# `-x db_url=...` 이나 `ALEMBIC_DATABASE_URL` 로 덮을 수 있게 하고, 덮었으면
# 어느 DB 인지 찍는다. 자격증명이 섞여 있으므로 **DB 이름만** 출력한다.
_override = (
    context.get_x_argument(as_dictionary=True).get("db_url")
    or os.environ.get("ALEMBIC_DATABASE_URL")
)
settings = get_settings()
_url = _override or settings.DATABASE_URL
config.set_main_option("sqlalchemy.url", _url)
if _override:
    print(f"[alembic] 대상 DB 재지정: {_url.rsplit('/', 1)[-1]}")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
