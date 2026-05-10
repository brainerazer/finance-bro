import json
import logging as stdlog
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"


def _client_info():
    return json.loads((FIXTURES / "client_info_minimal.json").read_text())


def _statement():
    return json.loads((FIXTURES / "statement_two_items.json").read_text())


@pytest.mark.asyncio
async def test_first_import_discovers_and_inserts(client):
    with (
        respx.mock(base_url="https://api.monobank.ua") as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=_client_info())
        )
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=_statement())
        )
        r = await client.post("/api/import")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["statement_count"] == 2
    assert body["inserted"] == 2
    assert body["skipped_duplicates"] == 0
    assert body["polled_account_id"] == "card-id-1"


@pytest.mark.asyncio
async def test_all_accounts_persisted(client):
    with (
        respx.mock(base_url="https://api.monobank.ua") as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=_client_info())
        )
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=_statement())
        )
        await client.post("/api/import")
    r = await client.get("/api/accounts")
    assert r.status_code == 200
    kinds = sorted(a["source_kind"] for a in r.json())
    assert kinds == ["mono.card", "mono.jar"]


@pytest.mark.asyncio
async def test_raw_payload_verbatim(client):
    statement = _statement()
    with (
        respx.mock(base_url="https://api.monobank.ua") as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=_client_info())
        )
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=statement)
        )
        await client.post("/api/import")
    r = await client.get("/api/transactions")
    rows = r.json()
    assert len(rows) == 2
    rows_by_id = {row["source_tx_id"]: row for row in rows}
    for original in statement:
        row = rows_by_id[original["id"]]
        assert row["raw_payload"] == original


@pytest.mark.asyncio
async def test_no_token_in_info_logs_full_cycle(client, caplog):
    caplog.set_level(stdlog.INFO)
    token = os.environ["MONO_TOKEN"]
    assert len(token) >= 30, "Test token must be ≥30 chars to exercise the redaction regex"
    with (
        respx.mock(base_url="https://api.monobank.ua") as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=_client_info())
        )
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=_statement())
        )
        await client.post("/api/import")
        await client.get("/api/transactions")
    combined = caplog.text
    assert token not in combined
    assert "X-Token" not in combined
    # Statement amounts must not leak at INFO
    for amount in (-8500, 8500, 5000000):
        assert str(amount) not in combined
