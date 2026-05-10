"""401 sticky behavior — SC#4 + D-15.

A 401 from Mono sets `scheduler_state.state='auth_failed'` and PERSISTS so
a fresh runner instance reading the same DB observes the sticky bit and
the next tick is a no-op (no Mono call). Tests both the in-process cache
flip and the cross-restart simulation.

PATTERNS.md Archetype C + D — respx-mocked 401 + fresh runner reads disk
(mirroring tests/test_rate_limit_gate.py::test_persists_across_restart).
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
async def test_401_persists_across_restart(session_factory):
    """D-15: a 401 from Mono sets scheduler_state.state='auth_failed' and persists
    so a fresh runner instance reads sticky.

    Verifies: in-process cache flips, DB row updated, fresh runner reads sticky bit
    on read_state(), subsequent tick on the fresh runner is a no-op."""
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

    runner_a = _make_runner(session_factory)
    await runner_a.read_state()  # cache state='running'
    assert runner_a._cached_state[0] == "running"

    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(
                401, json={"errorDescription": "Unknown 'X-Token'"}
            )
        )
        await runner_a.tick()

    # In-process cache flipped.
    assert runner_a._cached_state[0] == "auth_failed"
    # DB persisted.
    async with session_factory() as s:
        state = (
            await s.execute(
                text("SELECT state FROM scheduler_state WHERE id=1")
            )
        ).scalar_one()
    assert state == "auth_failed"

    # Fresh runner reads sticky bit.
    runner_b = _make_runner(session_factory)
    state_b, err_b = await runner_b.read_state()
    assert state_b == "auth_failed"
    assert err_b is not None and "401" in err_b

    # Subsequent tick is a no-op — must not raise, must not call Mono.
    # Re-enter respx mock so any unintended call would error loudly.
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        # Intentionally don't register any route — any HTTP call would fail.
        await runner_b.tick()
        assert mock.calls.call_count == 0

    # And the import_runs row stays in error/pending — not in_flight.
    async with session_factory() as s:
        statuses = (
            (await s.execute(text("SELECT status FROM import_runs ORDER BY id")))
            .scalars()
            .all()
        )
    # The first tick claimed and errored the live row; no new claim happened
    # in runner_b's tick since auth_failed short-circuits.
    assert "error" in statuses
