"""POST /api/backfill — D-07 + CR-02.

Confirms the route's 202 happy path (delegated to runner.enqueue_backfill)
and the CR-02 fix that translates a runner ValueError into 404 when the
caller pins to an unknown / non-pollable account_id.
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_backfill_returns_202_for_pollable_card(client, session_factory):
    """D-07: POST /api/backfill enqueues 12 chunks and returns 202 + run_ids."""
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

    r = await client.post("/api/backfill", json={"account_id": 1, "months": 12})
    assert r.status_code == 202, r.text
    body = r.json()
    assert "run_ids" in body
    assert len(body["run_ids"]) == 12


@pytest.mark.asyncio
async def test_backfill_404_for_unknown_account(client):
    """CR-02: account_id that does not resolve to a pollable card → 404."""
    r = await client.post("/api/backfill", json={"account_id": 99999, "months": 12})
    assert r.status_code == 404, r.text
    body = r.json()
    assert "detail" in body
    assert "99999" in body["detail"]


@pytest.mark.asyncio
async def test_backfill_404_for_eaid_account(client, session_factory):
    """CR-02: account_id pinning to an eAid card surfaces as 404 — the
    allowlist filter is a real input-validation rejection, not a silent
    no-op."""
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
                VALUES (1, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid')
                """
            )
        )
        await s.commit()

    r = await client.post("/api/backfill", json={"account_id": 1, "months": 12})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_backfill_no_account_id_returns_empty(client):
    """CR-02 boundary: omitted account_id with no pollable cards is NOT a 404
    — empty result is the correct steady-state truth for a fresh install."""
    r = await client.post("/api/backfill", json={"months": 12})
    assert r.status_code == 202
    assert r.json()["run_ids"] == []
