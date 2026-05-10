"""GET /api/import/status — D-14 shape + ING-08 + SC#4.

Asserts the full status response: scheduler section, per-account snapshot
(including allowlist-filtered eAid cards — Pitfall 10), and aggregate
backfill state. The 401-vs-429 distinction (SC#4) is verified by seeding
both a sticky `auth_failed` scheduler_state row and a 429-bearing
import_runs row, then asserting the response distinguishes them.
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_status_response_shape(client, session_factory):
    """ING-08 D-14: GET /api/import/status returns the full schema with all
    keys present.

    Seed 2 cards (1 black, 1 eAid) + a completed live run for the black card
    + 3 pending backfill rows for it. Assert the JSON has the full nested
    shape and the values are sensible.
    """
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
                  (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
                  (2, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid')
                """
            )
        )
        # One completed live run for the black card.
        await s.execute(
            text(
                """
                INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted)
                VALUES (1, 'live', now()-interval '1 hour', now()-interval '5 minutes', 'done', now()-interval '5 minutes', 7, 7)
                """
            )
        )
        # 3 pending backfill rows for the black card.
        await s.execute(
            text(
                """
                INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status)
                VALUES (1, 'backfill', now()-interval '60 days', now()-interval '30 days', 'pending'),
                       (1, 'backfill', now()-interval '90 days', now()-interval '60 days', 'pending'),
                       (1, 'backfill', now()-interval '120 days', now()-interval '90 days', 'pending')
                """
            )
        )
        await s.commit()

    r = await client.get("/api/import/status")
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level shape.
    assert set(body.keys()) >= {"scheduler", "accounts", "backfill"}

    # scheduler section (D-14).
    assert body["scheduler"]["state"] == "running"
    assert "since" in body["scheduler"]
    assert body["scheduler"]["last_error"] is None

    # accounts section: BOTH cards present (Pitfall 10 — eAid visible with
    # mono_type='eAid' so the user can see why it's not being polled).
    assert len(body["accounts"]) == 2
    by_aid = {a["account_id"]: a for a in body["accounts"]}
    assert by_aid[1]["mono_type"] == "black"
    assert by_aid[1]["last_polled_at"] is not None
    assert by_aid[1]["last_poll_inserted"] == 7
    assert by_aid[1]["last_poll_statement_count"] == 7
    assert by_aid[1]["last_status"] == "done"
    assert by_aid[1]["backfill_remaining"] == 3
    assert by_aid[1]["backfill_total"] == 3
    assert by_aid[1]["last_poll_updated"] == 0  # v1 always 0 — D-14
    assert by_aid[2]["mono_type"] == "eAid"
    # eAid never polled (excluded by allowlist) → last_polled_at is None.
    assert by_aid[2]["last_polled_at"] is None

    # backfill section.
    assert body["backfill"]["state"] == "running"  # 3 pending → running
    assert body["backfill"]["runs_remaining"] == 3
    assert body["backfill"]["runs_total"] == 3


@pytest.mark.asyncio
async def test_last_polled_at_per_account(client, session_factory):
    """ING-08: each account's last_polled_at reflects its most recent live run."""
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
        # Card 1: two completed runs; the more recent should appear in status.
        await s.execute(
            text(
                """
                INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted) VALUES
                  (1, 'live', now()-interval '2 hours', now()-interval '90 minutes', 'done', now()-interval '90 minutes', 1, 1),
                  (1, 'live', now()-interval '1 hour',  now()-interval '5 minutes',  'done', now()-interval '5 minutes',  3, 3)
                """
            )
        )
        # Card 2: one completed run.
        await s.execute(
            text(
                """
                INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted)
                VALUES (2, 'live', now()-interval '30 minutes', now()-interval '20 minutes', 'done', now()-interval '20 minutes', 0, 0)
                """
            )
        )
        await s.commit()

    r = await client.get("/api/import/status")
    assert r.status_code == 200
    by_aid = {a["account_id"]: a for a in r.json()["accounts"]}
    # Card 1's last_polled_at is the more recent (5 minutes ago, with inserted=3, not 1).
    assert by_aid[1]["last_poll_inserted"] == 3
    # Card 2's run is independent.
    assert by_aid[2]["last_poll_inserted"] == 0


@pytest.mark.asyncio
async def test_401_vs_429_distinguished(client, session_factory):
    """SC#4: scheduler.state='auth_failed' (401 banner) is distinct from a
    per-account 429.

    Seed scheduler_state to auth_failed; seed a live run with last_error
    containing 429. Assert response distinguishes them: scheduler.state ==
    'auth_failed' for the global state; the per-account row carries 429 in
    last_error (NOT promoted to the global scheduler section).
    """
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE scheduler_state SET state='auth_failed', "
                "last_error='Mono token rejected (401)' WHERE id=1"
            )
        )
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
                INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, last_error)
                VALUES (1, 'live', now()-interval '1 hour', now()-interval '5 minutes', 'error', now()-interval '5 minutes', '429 (Retry-After=60)')
                """
            )
        )
        await s.commit()

    r = await client.get("/api/import/status")
    assert r.status_code == 200
    body = r.json()
    # 401 banner state at the scheduler level.
    assert body["scheduler"]["state"] == "auth_failed"
    assert "401" in body["scheduler"]["last_error"]
    # 429 surfaces per-account, NOT at scheduler level.
    by_aid = {a["account_id"]: a for a in body["accounts"]}
    assert "429" in by_aid[1]["last_error"]
    assert by_aid[1]["last_status"] == "error"


@pytest.mark.asyncio
async def test_status_idle_backfill_with_no_pending(client, session_factory):
    """D-14: backfill.state='idle' when nothing is pending."""
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

    r = await client.get("/api/import/status")
    assert r.status_code == 200
    body = r.json()
    assert body["backfill"]["state"] == "idle"
    assert body["backfill"]["runs_remaining"] == 0
    assert body["backfill"]["runs_total"] == 0
