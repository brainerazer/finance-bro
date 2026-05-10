import os
from collections.abc import AsyncIterator

import pytest_asyncio
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer


@pytest_asyncio.fixture(scope="session")
async def pg_container() -> AsyncIterator[PostgresContainer]:
    with PostgresContainer("postgres:17-bookworm") as pg:
        yield pg


@pytest_asyncio.fixture(scope="session")
async def pg_url(pg_container: PostgresContainer) -> str:
    # testcontainers default URL has psycopg2 driver; rewrite to psycopg v3
    url = pg_container.get_connection_url().replace("psycopg2", "psycopg")
    os.environ["DATABASE_URL"] = url
    os.environ.setdefault("MONO_TOKEN", "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    # Clear any cached settings
    from finance_bro.core import settings as s

    s.get_settings.cache_clear()
    # Run alembic migrations
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    return url


@pytest_asyncio.fixture
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(pg_url, echo=False, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from finance_bro.db.engine import set_engine

    set_engine(engine, factory)
    yield factory


@pytest_asyncio.fixture
async def client(session_factory) -> AsyncIterator[AsyncClient]:
    from finance_bro.main import app

    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
