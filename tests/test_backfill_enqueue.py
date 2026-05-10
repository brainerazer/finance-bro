"""SchedulerRunner.enqueue_backfill — D-05 + D-08 + ING-06.

Asserts 12-month backfill produces 12 newest-first 30d chunks per active
card and that the eAid allowlist filter applies (no enqueue for eAid even
when account_id is None — implicit "all active cards").

PATTERNS.md Archetype B: testcontainers Postgres, no HTTP needed
(enqueue_backfill writes to import_runs only).
"""

import pytest
import pytest_asyncio
from sqlalchemy import text

from finance_bro.importers.monobank import MonobankImporter
from finance_bro.importers.rate_limit import RateLimitGate
from finance_bro.scheduler.runner import SchedulerRunner


@pytest_asyncio.fixture(autouse=True)
async def _truncate(session_factory):
    """Truncate before AND after the test — see explanation in
    `tests/test_scheduler_round_robin.py::_truncate`. Tests in this file use
    explicit id=N inserts that would otherwise leak past the test boundary
    and break a sibling test file relying on a fresh accounts sequence."""
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


def _make_runner(session_factory) -> SchedulerRunner:
    gate = RateLimitGate(session_factory)
    importer = MonobankImporter(
        token="dummy-token-32chars-aaaaaaaaaaaaaaaa", gate=gate
    )
    return SchedulerRunner(session_factory=session_factory, importer=importer)


@pytest.mark.asyncio
async def test_twelve_chunks_newest_first(session_factory):
    """ING-06: enqueue_backfill creates 12 import_runs rows with run_kind='backfill',
    status='pending', windows 30d apart in newest-first order."""
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

    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT id, window_from, window_to, run_kind, status FROM import_runs
                    WHERE account_id=1 ORDER BY window_to DESC
                    """
                )
            )
        ).all()
    assert len(rows) == 12
    assert all(r.run_kind == "backfill" for r in rows)
    assert all(r.status == "pending" for r in rows)
    deltas = [(rows[i].window_from, rows[i].window_to) for i in range(12)]
    for i in range(11):
        # Each chunk is 30d wide.
        assert (deltas[i][1] - deltas[i][0]).days == 30
        # Adjacent chunks abut: rows[i].window_from == rows[i+1].window_to.
        assert deltas[i][0] == deltas[i + 1][1]


@pytest.mark.asyncio
async def test_enqueue_backfill_skips_eaid(session_factory):
    """D-01: enqueue_backfill respects list_pollable_cards (no eAid)."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
                  (1, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid'),
                  (2, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
                """
            )
        )
        await s.commit()
    runner = _make_runner(session_factory)
    ids = await runner.enqueue_backfill(account_id=None, months=12)
    # Only card 2, 12 chunks.
    assert len(ids) == 12

    async with session_factory() as s:
        eaid_count = (
            await s.execute(
                text("SELECT count(*) FROM import_runs WHERE account_id=1")
            )
        ).scalar_one()
    assert eaid_count == 0
