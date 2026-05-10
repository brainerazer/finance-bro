"""Backfill resumability — SC#2 + ING-06 + Pitfall 7.

Stale `in_flight` rows are reset to `pending` on lifespan startup; partial
backfills resume from the next pending chunk; 4xx responses inside a chunk
mark the row errored (not silently skipped — Pitfall 3).

PATTERNS.md Archetype B + D — driven via `await runner.tick()` directly,
no `freezegun` on APScheduler internals (Pitfall 9).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import text

from finance_bro.importers.monobank import MonobankImporter
from finance_bro.importers.rate_limit import RateLimitGate
from finance_bro.scheduler.runner import SchedulerRunner

FIXTURES = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as s:
        await s.execute(
            text(
                "TRUNCATE TABLE transactions, import_runs, accounts, "
                "scheduler_state, mono_rate_state RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(
            text("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')")
        )
        await s.commit()
    yield


def _make_runner(session_factory) -> SchedulerRunner:
    gate = RateLimitGate(session_factory)
    importer = MonobankImporter(
        token="dummy-token-32chars-aaaaaaaaaaaaaaaa", gate=gate
    )
    return SchedulerRunner(session_factory=session_factory, importer=importer)


@pytest.mark.asyncio
async def test_recover_in_flight_on_restart(session_factory):
    """Pattern 7: a stale in_flight row is reset to pending by recover_in_flight."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
                VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
                """
            )
        )
        await s.execute(
            text(
                """
                INSERT INTO import_runs
                  (account_id, run_kind, window_from, window_to, status, started_at, attempts)
                VALUES (1, 'live', now()-interval '1 hour', now(), 'in_flight',
                        now() - interval '6 minutes', 1)
                """
            )
        )
        await s.commit()
    # Simulate restart: fresh runner instance reads same DB.
    runner = _make_runner(session_factory)
    swept = await runner.recover_in_flight()
    assert swept == 1
    async with session_factory() as s:
        status = (
            await s.execute(text("SELECT status FROM import_runs"))
        ).scalar_one()
    assert status == "pending"


@pytest.mark.asyncio
async def test_resume_picks_remaining_chunks(session_factory):
    """Enqueue 12 backfill rows; mark 5 as done; instantiate a new runner;
    call tick repeatedly with respx empty statements; assert remaining 7
    rows complete."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
                VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
                """
            )
        )
        await s.commit()

    runner = _make_runner(session_factory)
    ids = await runner.enqueue_backfill(account_id=1, months=12)
    assert len(ids) == 12

    # Mark the first 5 as done (oldest by created_at; they would be claimed first).
    async with session_factory() as s:
        await s.execute(
            text(
                """
                UPDATE import_runs SET status='done', completed_at=now(),
                  statement_count=0, inserted=0
                WHERE id = ANY(:ids)
                """
            ),
            {"ids": ids[:5]},
        )
        await s.commit()

    # Fresh runner — simulates restart mid-backfill.
    runner_b = _make_runner(session_factory)
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=[])
        )
        # 7 remaining chunks; one tick claims-and-executes one chunk.
        for _ in range(7):
            await runner_b.tick()

    async with session_factory() as s:
        done_count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM import_runs "
                    "WHERE run_kind='backfill' AND status='done'"
                )
            )
        ).scalar_one()
        pending_count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM import_runs "
                    "WHERE run_kind='backfill' AND status='pending'"
                )
            )
        ).scalar_one()
    assert done_count == 12
    assert pending_count == 0


@pytest.mark.asyncio
async def test_full_12_month_walk(session_factory):
    """SC#2 + ING-06: full 12-chunk walk with respx returning [] for every chunk."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
                VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
                """
            )
        )
        await s.commit()

    runner = _make_runner(session_factory)
    await runner.enqueue_backfill(account_id=1, months=12)

    empty_payload = json.loads((FIXTURES / "statement_empty.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=empty_payload)
        )
        for _ in range(12):
            await runner.tick()

    async with session_factory() as s:
        statuses = (
            (
                await s.execute(
                    text(
                        "SELECT status FROM import_runs "
                        "WHERE run_kind='backfill' ORDER BY id"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(statuses) == 12
    assert all(s == "done" for s in statuses)


@pytest.mark.asyncio
async def test_4xx_marks_error_not_skip(session_factory):
    """Pitfall 3: a 4xx during a backfill chunk marks status='error', not silent skip.
    The runner translates the 4xx to MonoTransientError and writes last_error."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
                VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
                """
            )
        )
        # Single pending backfill chunk — keep the test focused.
        await s.execute(
            text(
                """
                INSERT INTO import_runs
                  (account_id, run_kind, window_from, window_to, status)
                VALUES (1, 'backfill', now()-interval '60 days',
                        now()-interval '30 days', 'pending')
                """
            )
        )
        await s.commit()

    runner = _make_runner(session_factory)
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(
                400, json={"errorDescription": "Bad request"}
            )
        )
        await runner.tick()

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, last_error FROM import_runs WHERE account_id=1"
                )
            )
        ).first()
    assert row is not None
    assert row.status == "error"
    assert row.last_error is not None
    assert "400" in row.last_error
