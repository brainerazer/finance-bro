from typing import cast

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finance_bro.core.settings import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Lazily create the async engine + session factory once from settings."""
    global _engine, _factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, echo=False, future=True)
        _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine, cast(async_sessionmaker[AsyncSession], _factory)


def set_engine(engine: AsyncEngine, factory: async_sessionmaker[AsyncSession]) -> None:
    """Test-only entry point used by tests/conftest.py to wire the
    testcontainers Postgres."""
    global _engine, _factory
    _engine = engine
    _factory = factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _factory is None:
        init_engine()
    assert _factory is not None
    return _factory
