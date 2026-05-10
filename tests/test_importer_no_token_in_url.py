import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def stub_gate():
    g = AsyncMock()
    g.acquire = AsyncMock(return_value=None)
    return g


@pytest.mark.asyncio
async def test_token_only_in_header_for_client_info(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    payload = json.loads((FIXTURES / "client_info_minimal.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        route = mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=payload)
        )
        imp = MonobankImporter(TOKEN, stub_gate)
        await imp.discover_accounts()
        await imp.aclose()
    req = route.calls[0].request
    url_str = str(req.url)
    assert TOKEN not in url_str, f"Token must never appear in URL; saw {url_str}"
    assert req.headers.get("X-Token") == TOKEN


@pytest.mark.asyncio
async def test_token_only_in_header_for_statement(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    payload = json.loads((FIXTURES / "statement_two_items.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        route = mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        imp = MonobankImporter(TOKEN, stub_gate)
        txs = [
            t
            async for t in imp.fetch_statement(
                "acc-x",
                datetime(2026, 5, 9, tzinfo=UTC),
                datetime(2026, 5, 10, tzinfo=UTC),
            )
        ]
        await imp.aclose()
    assert len(txs) == 2
    req = route.calls[0].request
    url_str = str(req.url)
    assert TOKEN not in url_str
    assert "acc-x" in url_str  # the account ID DOES appear in URL — that's fine
    assert req.headers.get("X-Token") == TOKEN
