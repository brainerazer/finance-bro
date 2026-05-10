import asyncio

import pytest
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import run_alembic


@pytest.mark.asyncio
async def test_round_trip(pg_url, engine):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    cfg.set_main_option("script_location", "alembic")
    await asyncio.to_thread(run_alembic, cfg, "base", downgrade=True)
    await asyncio.to_thread(run_alembic, cfg, "head")
    async with engine.connect() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname='public' ORDER BY tablename"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "accounts" in tables
    assert "transactions" in tables
    assert "mono_rate_state" in tables
    assert "alembic_version" in tables
