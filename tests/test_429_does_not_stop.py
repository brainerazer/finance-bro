"""429 transient behavior — SC#4 + D-15.

A Mono 429 sets `import_runs.status='error'` (with 429 / Retry-After in
last_error) but does NOT transition `scheduler_state` to `auth_failed` —
rate limits are per-call transients, not auth events. The scheduler keeps
running and the next tick is allowed to try the next pending row.

PATTERNS.md Archetype B: testcontainers Postgres + respx 429 + asyncio.sleep
patched so RateLimitGate doesn't block the test on the 65s window.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import text

from finance_bro.importers.monobank import MonobankImporter
from finance_bro.importers.rate_limit import RateLimitGate
from finance_bro.scheduler.runner import SchedulerRunner


@pytest_asyncio.fixture(autouse=True)
async def _truncate(session_factory):
    """Truncate before AND after — same rationale as
    `tests/test_scheduler_round_robin.py::_truncate`."""
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
async def test_429_marks_run_error_but_state_remains_running(session_factory):
    """D-15: a 429 sets import_runs.status='error' with 429 in last_error
    but does NOT transition scheduler_state to auth_failed."""
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
                  (account_id, run_kind, window_from, window_to, status)
                VALUES (1, 'live', now()-interval '1 hour', now(), 'pending')
                """
            )
        )
        await s.commit()

    runner = _make_runner(session_factory)
    await runner.read_state()
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "60"},
                json={"errorDescription": "Too many requests"},
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
    assert "429" in row.last_error or "Retry-After" in row.last_error
    # Scheduler state is unchanged.
    assert runner._cached_state[0] == "running"
    async with session_factory() as s:
        state = (
            await s.execute(
                text("SELECT state FROM scheduler_state WHERE id=1")
            )
        ).scalar_one()
    assert state == "running"


@pytest.mark.asyncio
async def test_429_without_retry_after_handled(session_factory):
    """Pattern 4: missing Retry-After header → retry_after_seconds=None; no crash."""
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
                  (account_id, run_kind, window_from, window_to, status)
                VALUES (1, 'live', now()-interval '1 hour', now(), 'pending')
                """
            )
        )
        await s.commit()
    runner = _make_runner(session_factory)
    await runner.read_state()
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(429, json={})  # no Retry-After header
        )
        await runner.tick()
    async with session_factory() as s:
        status = (
            await s.execute(
                text("SELECT status FROM import_runs WHERE account_id=1")
            )
        ).scalar_one()
    assert status == "error"
