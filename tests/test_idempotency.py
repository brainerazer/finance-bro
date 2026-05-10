"""Idempotency — SC#3: re-importing produces no duplicate rows.

Phase 2 D-16 reshape: POST /api/import returns 202 + {enqueued: [...]}; the
fetch+insert happens on the next scheduler tick. To exercise the full
"second import is a user-visible no-op" contract end-to-end we drive
`runner.tick()` directly after each enqueue (the scheduler is disabled in
tests via APP_DISABLE_SCHEDULER=1, so we must drive ticks manually).

The assertion the user actually cares about is `len(r.json()) == 2` — one
row per Mono id, no matter how many times we hit /api/import. ON CONFLICT
DO UPDATE (Phase 2 02-02) means the second tick touches both rows via
UPDATE rather than skipping them via DO NOTHING; the single-row invariant
is unchanged.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import text

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_second_import_is_noop(client, runner, session_factory):
    ci = json.loads((FIXTURES / "client_info_minimal.json").read_text())
    stmt = json.loads((FIXTURES / "statement_two_items.json").read_text())

    # Seed the discovered card so enqueue_live_for_all_active_cards has work,
    # avoiding the runner's cold-boot discovery path inside tick.
    async with session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload, mono_type)
                VALUES ('mono.card', 'card-id-1', 'UAH', '{}'::jsonb, 'black')
                """
            )
        )
        await s.commit()

    # WR-07: `runner` is a conftest fixture that returns app.state.runner —
    # no longer reaching into httpx's private `_transport` attribute.

    with (
        respx.mock(base_url="https://api.monobank.ua", assert_all_called=False) as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=ci)
        )
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=stmt)
        )
        # First enqueue + tick to consume.
        first = await client.post("/api/import")
        await runner.tick()  # consume the live row → fetches + inserts
        # Second enqueue + tick — second tick should hit DO UPDATE, not DO NOTHING.
        second = await client.post("/api/import")
        await runner.tick()

    assert first.status_code == 202
    assert second.status_code == 202
    # D-16: response is {enqueued: [{account_id, run_id}, ...]}; one row per
    # active card per call.
    assert len(first.json()["enqueued"]) == 1
    assert len(second.json()["enqueued"]) == 1

    # SC#3: the user-visible single-row invariant — one row per Mono id, no
    # duplicates after the second import. ON CONFLICT DO UPDATE (Phase 2 02-02)
    # touches the two rows in place rather than skipping them via DO NOTHING;
    # the assertion below is unchanged.
    r = await client.get("/api/transactions")
    assert len(r.json()) == 2
