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
    # Phase 2 (migration 0002) — new schema objects must round-trip cleanly.
    assert "import_runs" in tables
    assert "scheduler_state" in tables
    async with engine.connect() as conn:
        mono_type_present = (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='accounts' AND column_name='mono_type'"
                )
            )
        ).first()
        scheduler_state_seed = (
            await conn.execute(
                text("SELECT state FROM scheduler_state WHERE id = 1")
            )
        ).first()
    assert mono_type_present is not None, "accounts.mono_type column missing after migration"
    assert scheduler_state_seed is not None, "scheduler_state singleton row missing"
    assert scheduler_state_seed[0] == "running"
