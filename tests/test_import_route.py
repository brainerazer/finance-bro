"""Force-poll route — Phase 2 D-16 reshape.

Phase 1's synchronous body shape (statement_count / inserted /
skipped_duplicates / polled_account_id) is GONE. The route now enqueues
import_runs rows and returns 202 + {enqueued: [{account_id, run_id}]}.

End-to-end fetch+insert behavior (the "tick consumes the row and inserts
transactions" path) is covered by tests/test_scheduler_round_robin.py and
tests/test_force_poll_endpoint.py — no need to double-test it here.
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_first_import_enqueues_returns_202(client, session_factory):
    """D-16: POST /api/import returns 202 + {enqueued: [...]} after enqueueing
    live rows for every active card. Phase 1's synchronous body is gone."""
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

    r = await client.post("/api/import")
    assert r.status_code == 202, r.text
    body = r.json()
    assert "enqueued" in body
    assert isinstance(body["enqueued"], list)
    assert len(body["enqueued"]) == 1
    assert body["enqueued"][0]["account_id"] == 1
    assert isinstance(body["enqueued"][0]["run_id"], int)
    assert body["enqueued"][0]["run_id"] > 0


@pytest.mark.asyncio
async def test_import_enqueues_rows_visible_in_db(client, session_factory):
    """D-16: the response's enqueued run_ids correspond to real pending live
    import_runs rows — the scheduler tick will consume them on the next 10s
    slot."""
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

    r = await client.post("/api/import")
    assert r.status_code == 202

    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT account_id, run_kind, status FROM import_runs "
                    "WHERE run_kind='live'"
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].account_id == 1
    assert rows[0].run_kind == "live"
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_import_with_no_cards_returns_empty_enqueued(client):
    """D-16: empty accounts table → 202 + {enqueued: []}, NOT a 409 (Phase 1
    behavior gone). Discovery happens inside the runner's tick, not the route."""
    # Conftest TRUNCATE wipes accounts before each client test.
    r = await client.post("/api/import")
    assert r.status_code == 202
    body = r.json()
    assert body["enqueued"] == []
