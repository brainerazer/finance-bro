"""POST /api/import — D-16 reshape (dedicated coverage).

Phase 1's synchronous body shape (statement_count / inserted /
skipped_duplicates / polled_account_id) is GONE. The route now enqueues
import_runs rows and returns 202 + {enqueued: [{account_id, run_id}]}.
The scheduler tick consumes the rows on the next 10s slot.

The runner's enqueue path reads `list_pollable_cards()` (D-01 allowlist —
black/platinum/white only) and inserts via `enqueue_live`. No discovery is
triggered by this path; if accounts is empty, the response is `{enqueued:
[]}` rather than a 409.
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_returns_202_enqueued(client, session_factory):
    """D-16: POST /api/import returns 202 with {enqueued: [{account_id,
    run_id}]} and persists pending live import_runs rows."""
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
        await s.commit()

    r = await client.post("/api/import")
    assert r.status_code == 202, r.text
    body = r.json()
    assert "enqueued" in body
    assert len(body["enqueued"]) == 2
    aids = {row["account_id"] for row in body["enqueued"]}
    assert aids == {1, 2}
    for row in body["enqueued"]:
        assert isinstance(row["run_id"], int)
        assert row["run_id"] > 0

    # Side-effect: import_runs has 2 pending live rows.
    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT account_id, run_kind, status FROM import_runs "
                    "WHERE run_kind='live'"
                )
            )
        ).all()
    assert len(rows) == 2
    assert all(r.status == "pending" for r in rows)


@pytest.mark.asyncio
async def test_enqueues_one_row_per_active_card(client, session_factory):
    """D-01 + D-16: only allowlisted cards get enqueued; eAid is skipped."""
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
    r = await client.post("/api/import")
    assert r.status_code == 202
    body = r.json()
    assert len(body["enqueued"]) == 3  # 4 cards minus eAid
    aids = {row["account_id"] for row in body["enqueued"]}
    assert aids == {2, 3, 4}


@pytest.mark.asyncio
async def test_enqueues_zero_when_no_cards(client):
    """D-16: empty accounts table → 202 + {enqueued: []}, NOT a 409 (Phase 1
    behavior gone). Runner's enqueue_live_for_all_active_cards reads
    list_pollable_cards which returns []; no Mono call is made."""
    # No accounts seeded; conftest TRUNCATE already wiped them.
    r = await client.post("/api/import")
    assert r.status_code == 202
    body = r.json()
    assert body["enqueued"] == []
