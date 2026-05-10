"""SchedulerRunner round-robin behavior — SC#1 + D-01 + D-02.

The eAid card type is excluded by AccountRepo.list_pollable_cards (D-01
fail-closed allowlist); the runner's pick path therefore never sees it.
Allowlisted cards (black/platinum/white) are visited in id-ASC order on
cold start (last_live_per_account is empty), then by oldest completed_at.

PATTERNS.md Archetype B: testcontainers Postgres + respx-mocked importer.
Each test owns its own truncate fixture because runner state is global to
the test session (tick() walks all accounts, all import_runs).
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
    """Reset transactions/import_runs/accounts/scheduler_state and reseed the
    singleton — single-consumer tick() relies on a clean queue per test.

    Truncates BOTH before and after the test. This file's tests use explicit
    id values in INSERT statements (id=1 = eAid, id=2 = black, etc. — the
    round-robin verification needs id-ASC determinism). Without a post-test
    truncate, leftover rows would conflict with the next test file's
    `accounts_pkey`-relying inserts (e.g. `tests/test_schema_invariants.py`
    relies on a fresh sequence starting at 1)."""
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
async def test_eaid_skipped(session_factory):
    """SC#1 + D-01: list_pollable_cards excludes eAid; the runner's pick
    path never sees it. Seed 4 cards (eAid, black, platinum, white) directly,
    call _pick_next_active_card across simulated polls, assert eAid id is
    never returned."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
                  (1, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid'),
                  (2, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
                  (3, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum'),
                  (4, 'mono.card', 'white-id', 'UAH', '{}'::jsonb, 'white')
                """
            )
        )
        await s.commit()

    runner = _make_runner(session_factory)
    picked_ids: set[int] = set()
    for _ in range(10):
        card = await runner._pick_next_active_card()
        if card is None:
            break
        picked_ids.add(card.id)
        # Simulate completion of a live run so the next pick rotates.
        async with session_factory() as s:
            await s.execute(
                text(
                    """
                    INSERT INTO import_runs
                      (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted)
                    VALUES (:aid, 'live', now()-interval '1 hour', now(), 'done', now(), 0, 0)
                    """
                ),
                {"aid": card.id},
            )
            await s.commit()
    assert 1 not in picked_ids  # eAid (id=1) never picked
    assert picked_ids == {2, 3, 4}


@pytest.mark.asyncio
async def test_three_cards_visited_three_ticks(session_factory):
    """SC#1: 3 active cards round-robin; with respx mock returning empty
    statements, runner.tick() x N visits each card. Use a stub gate (sleep
    patched) so tests don't hang waiting on the 65s window."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
                  (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
                  (2, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum'),
                  (3, 'mono.card', 'white-id', 'UAH', '{}'::jsonb, 'white')
                """
            )
        )
        await s.commit()

    runner = _make_runner(session_factory)

    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=[])
        )
        # Tick 1: no pending → enqueues live for card id=1 (oldest = never polled).
        # Tick 2: claims the live row for card 1 → fetches → marks done.
        # Repeat for card 2, then card 3 (4 more ticks: enqueue+execute each).
        for _ in range(6):
            await runner.tick()

    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT account_id, count(*) FROM import_runs
                    WHERE run_kind='live' AND status='done'
                    GROUP BY account_id ORDER BY account_id
                    """
                )
            )
        ).all()
    visited = {r[0] for r in rows}
    assert visited == {1, 2, 3}


@pytest.mark.asyncio
async def test_pick_skips_card_with_in_flight_live_row(session_factory):
    """BL-02: a card whose most recent live run is `in_flight` (e.g. a stale
    row left over from a tick-time crash where _mark_error itself failed)
    must NOT be picked again — that creates a duplicate live row for the
    already-running account.

    Pre-fix behavior: completed_at=None collapses to datetime.min via the
    `or` fallback; the in_flight card "wins" the rotation and gets a
    duplicate enqueue.
    Post-fix: filtered out by _pick_next_active_card; the other (terminal)
    card is picked instead.
    """
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
                  (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
                  (2, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum')
                """
            )
        )
        # Card 1: a stale in_flight live row (started 2 minutes ago — under the
        # 5-minute recover threshold so recover_in_flight wouldn't reset it).
        await s.execute(
            text(
                """
                INSERT INTO import_runs
                  (account_id, run_kind, window_from, window_to, status, started_at)
                VALUES (1, 'live', now()-interval '1 hour', now(), 'in_flight',
                        now() - interval '2 minutes')
                """
            )
        )
        # Card 2: a recently completed live run.
        await s.execute(
            text(
                """
                INSERT INTO import_runs
                  (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted)
                VALUES (2, 'live', now()-interval '1 hour', now()-interval '5 minutes', 'done', now()-interval '5 minutes', 0, 0)
                """
            )
        )
        await s.commit()

    runner = _make_runner(session_factory)
    picked = await runner._pick_next_active_card()
    assert picked is not None
    # Card 1 is in_flight → must be filtered. Card 2 is the only eligible.
    assert picked.id == 2


@pytest.mark.asyncio
async def test_recover_in_flight_runs_per_tick(session_factory):
    """WR-03: stale in_flight rows older than 5 minutes are reset on every
    tick (not only at lifespan startup), so a tick-time _mark_error failure
    does not leave a row stuck in_flight indefinitely."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
                VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
                """
            )
        )
        # Stale in_flight row — older than the 5-minute threshold.
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

    runner = _make_runner(session_factory)
    # No respx mock set up — the stale row will be reset to pending at the top
    # of the tick. Then claim_next_pending picks it up; we don't care about
    # the fetch outcome (httpx will fail), only that the in_flight sweep ran.
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=[])
        )
        await runner.tick()

    async with session_factory() as s:
        # The originally in_flight row was reset to pending then claimed and
        # marked done by the same tick.
        statuses = (
            (
                await s.execute(
                    text(
                        "SELECT status FROM import_runs "
                        "WHERE account_id=1 AND run_kind='live'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "in_flight" not in statuses


@pytest.mark.asyncio
async def test_eaid_skipped_via_tick(session_factory):
    """E2E: the tick path also never picks eAid even with the live-row claim path."""
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
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=[])
        )
        for _ in range(6):
            await runner.tick()

    async with session_factory() as s:
        rows = (
            (await s.execute(text("SELECT DISTINCT account_id FROM import_runs")))
            .scalars()
            .all()
        )
    assert 1 not in rows
