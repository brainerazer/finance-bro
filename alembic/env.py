"""Alembic environment for finance-bro.

Reads `DATABASE_URL` from the application Settings (env-only, D-01) and
mounts `Base.metadata` from `finance_bro.db.models` so autogenerate diffs
against the canonical declarative source.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from finance_bro.core.settings import get_settings
from finance_bro.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# If sqlalchemy.url isn't already set on this Config (tests set it via
# cfg.set_main_option), source it from the app Settings.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode — render SQL without a live DB connection.

    Used by `alembic upgrade head --sql` in CI/verification."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
