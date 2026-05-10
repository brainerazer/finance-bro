import asyncio
import os
from collections.abc import AsyncIterator

import pytest_asyncio
from alembic.config import Config
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command


def run_alembic(cfg: Config, target: str, *, downgrade: bool = False) -> None:
    """Sync helper — Alembic's `command.upgrade/downgrade` calls
    `asyncio.run()` internally for async env.py. We must run it in a thread
    so the test's existing event loop isn't disturbed."""
    if downgrade:
        command.downgrade(cfg, target)
    else:
        command.upgrade(cfg, target)


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
    # Phase 2 (Plan 02-03): the lifespan now starts an APScheduler that fires
    # `runner.tick()` every 10s. In test mode we want `app.state.runner` to
    # exist (so any test that wants to call runner methods directly can), but
    # we do NOT want the IntervalTrigger to fire during the test session.
    # APP_DISABLE_SCHEDULER=1 makes the lifespan skip `scheduler.start()` while
    # still building the runner + recovering in_flight + reading state.
    os.environ["APP_DISABLE_SCHEDULER"] = "1"
    # Clear any cached settings
    from finance_bro.core import settings as s

    s.get_settings.cache_clear()
    # Run alembic migrations in a worker thread — Alembic's online runner uses
    # asyncio.run() internally, which collides with the active test loop.
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "alembic")
    await asyncio.to_thread(run_alembic, cfg, "head")
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
    """ASGI test client. Truncates app-owned tables (`transactions`,
    `import_runs`, `accounts`, `scheduler_state`, `mono_rate_state`) BOTH
    before and after the test so that:

    - Phase 1 + Phase 2 route tests start with a clean slate and a re-seeded
      `scheduler_state` singleton (migration 0002 seeds it but TRUNCATE wipes
      it).
    - Tests that INSERT explicit primary-key values (e.g. id=1 for
      round-robin determinism — see test_force_poll_endpoint,
      test_import_status_shape, test_idempotency) don't leak past their
      boundary into sibling files that rely on a fresh `accounts_id_seq`
      (e.g. test_money_invariants does INSERT INTO accounts (...) WITHOUT
      an explicit id; the sequence resetting to 1 plus a leftover id=1 row
      = pkey collision). 02-03 SUMMARY documented the same deviation class
      for its session_factory-using tests; this is the corollary for client
      fixtures that contain explicit-id INSERTs.

    Tests that bypass the HTTP layer and use `session_factory` directly
    keep their own isolation (per-test autouse truncate fixtures or unique
    source_account_id values)."""
    from sqlalchemy import text

    from finance_bro.main import app

    truncate_sql = text(
        "TRUNCATE TABLE transactions, import_runs, accounts, "
        "scheduler_state, mono_rate_state RESTART IDENTITY CASCADE"
    )
    reseed_sql = text(
        "INSERT INTO scheduler_state (id, state) VALUES (1, 'running')"
    )

    async with session_factory() as s:
        await s.execute(truncate_sql)
        await s.execute(reseed_sql)
        await s.commit()

    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        yield ac

    async with session_factory() as s:
        await s.execute(truncate_sql)
        await s.execute(reseed_sql)
        await s.commit()
