import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import text

FIXTURES = Path(__file__).parent / "fixtures"


async def _seed(client):
    with (
        respx.mock(base_url="https://api.monobank.ua") as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(
            return_value=httpx.Response(
                200,
                json=json.loads((FIXTURES / "client_info_minimal.json").read_text()),
            )
        )
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(
                200,
                json=json.loads((FIXTURES / "statement_two_items.json").read_text()),
            )
        )
        await client.post("/api/import")


@pytest.mark.asyncio
async def test_response_shape(client):
    await _seed(client)
    r = await client.get("/api/transactions")
    rows = r.json()
    assert len(rows) == 2
    for row in rows:
        assert isinstance(row["amount_minor"], int)
        assert not isinstance(row["amount_minor"], bool)
        assert isinstance(row["currency"], str) and len(row["currency"]) == 3
        assert isinstance(row["raw_payload"], dict)
        # ISO-8601 string is fine; pydantic emits it that way
        assert "T" in row["time"] or "t" in row["time"]


@pytest.mark.asyncio
async def test_ordering_time_desc(client):
    await _seed(client)
    r = await client.get("/api/transactions")
    rows = r.json()
    assert rows[0]["source_tx_id"] == "tx-1"  # time=1746864000 (newer)
    assert rows[1]["source_tx_id"] == "tx-2"  # time=1746860400 (older)


@pytest.mark.asyncio
async def test_hold_field_in_response(client, session_factory):
    """ING-05 D-12: TransactionOut.hold is present and reflects the DB column.
    Seeds via raw SQL — `client` fixture has already truncated accounts/transactions
    so the route's `get_first_card()` returns the row we insert here."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'acct-A', 'UAH', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(text("SELECT id FROM accounts WHERE source_account_id='acct-A'"))
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO transactions "
                "  (account_id, source_tx_id, amount_minor, currency, time, raw_payload, hold) "
                "VALUES "
                "  (:a, 'tx-cleared', -100, 'UAH', now(), '{}'::jsonb, false), "
                "  (:a, 'tx-held',    -200, 'UAH', now(), '{}'::jsonb, true)"
            ),
            {"a": acc_id},
        )
        await s.commit()

    r = await client.get("/api/transactions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    by_id = {row["source_tx_id"]: row for row in rows}
    assert by_id["tx-cleared"]["hold"] is False
    assert by_id["tx-held"]["hold"] is True
    # Phase 1 fields still present (regression guard for the additive change):
    assert "amount_minor" in by_id["tx-cleared"]
    assert isinstance(by_id["tx-cleared"]["amount_minor"], int)
    assert isinstance(by_id["tx-cleared"]["raw_payload"], dict)
