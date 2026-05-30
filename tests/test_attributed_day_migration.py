"""D-09 / T-03-01 / T-03-02: the 0003 backfill rewrites attributed_day on
every existing row using the Kyiv timezone, then tightens the column to NOT
NULL — and the UPDATE provably runs BEFORE the ALTER (Pitfall 3).

A transaction stored at 2026-01-15 23:30 UTC is 2026-01-16 01:30 Kyiv (UTC+2
in January), so its attributed_day must land on 2026-01-16, NOT 2026-01-15.
This is the load-bearing tz-correctness assertion for the migration.
"""
import asyncio

import pytest
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import run_alembic


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    cfg.set_main_option("script_location", "alembic")
    return cfg


@pytest.mark.asyncio
async def test_attributed_day_backfilled_kyiv_correct(pg_url, engine):
    cfg = _cfg(pg_url)
    # Roll back to 0002 — the schema where attributed_day is still nullable.
    await asyncio.to_thread(run_alembic, cfg, "0002", downgrade=True)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts "
                "(source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'attr-day-acc', 'UAH', '{}'::jsonb)"
            )
        )
        acc_id = (
            await conn.execute(
                text("SELECT id FROM accounts WHERE source_account_id='attr-day-acc'")
            )
        ).scalar_one()
        # 23:30 UTC on 2026-01-15 == 01:30 Kyiv on 2026-01-16 (UTC+2 in winter).
        await conn.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, "
                " raw_payload, attributed_day) "
                "VALUES (:a, 'attr-1', -100, 'UAH', "
                "'2026-01-15T23:30:00+00:00'::timestamptz, '{}'::jsonb, NULL)"
            ),
            {"a": acc_id},
        )

    # Upgrade to 0003 — backfill + NOT NULL.
    await asyncio.to_thread(run_alembic, cfg, "0003")

    async with engine.connect() as conn:
        attributed = (
            await conn.execute(
                text(
                    "SELECT attributed_day FROM transactions WHERE source_tx_id='attr-1'"
                )
            )
        ).scalar_one()
        nullable = (
            await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='transactions' AND column_name='attributed_day'"
                )
            )
        ).scalar_one()

    assert str(attributed) == "2026-01-16", (
        "23:30 UTC must attribute to the Kyiv day 2026-01-16, not 2026-01-15"
    )
    assert nullable == "NO", "attributed_day must be NOT NULL after 0003"

    # The column now rejects NULL inserts.
    with pytest.raises(Exception):  # noqa: B017 — IntegrityError surface
        async with engine.begin() as conn:
            acc_id2 = (
                await conn.execute(
                    text(
                        "SELECT id FROM accounts WHERE source_account_id='attr-day-acc'"
                    )
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(account_id, source_tx_id, amount_minor, currency, time, "
                    " raw_payload, attributed_day) "
                    "VALUES (:a, 'attr-null', -1, 'UAH', now(), '{}'::jsonb, NULL)"
                ),
                {"a": acc_id2},
            )


@pytest.mark.asyncio
async def test_fx_tables_and_seed_present_after_upgrade(pg_url, engine):
    cfg = _cfg(pg_url)
    # Ensure we're at head (other test may have left us at 0003 already).
    await asyncio.to_thread(run_alembic, cfg, "head")

    async with engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' ORDER BY tablename"
                )
            )
        ).scalars().all()
        seed = (
            await conn.execute(
                text(
                    "SELECT currency, bootstrap_done FROM tracked_fx_currencies "
                    "ORDER BY currency"
                )
            )
        ).all()

    assert "fx_rates" in tables
    assert "tracked_fx_currencies" in tables
    # Exactly USD + EUR, both un-bootstrapped.
    assert seed == [("EUR", False), ("USD", False)]
