import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

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
