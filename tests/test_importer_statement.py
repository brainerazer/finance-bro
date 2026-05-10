import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stub_gate():
    g = AsyncMock()
    g.acquire = AsyncMock(return_value=None)
    return g


@pytest.mark.asyncio
async def test_discover_accounts_maps_kinds(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    payload = json.loads((FIXTURES / "client_info_minimal.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        mock.get("/personal/client-info").mock(return_value=httpx.Response(200, json=payload))
        imp = MonobankImporter("test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa", stub_gate)
        accounts = await imp.discover_accounts()
        await imp.aclose()
    kinds = sorted(a.source_kind for a in accounts)
    assert kinds == ["mono.card", "mono.jar"]
    for a in accounts:
        assert a.currency == "UAH"
    stub_gate.acquire.assert_awaited_once()


@pytest.mark.asyncio
async def test_discover_accounts_fop_kind(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    payload = {
        "clientId": "c",
        "name": "n",
        "webHookUrl": "",
        "permissions": "rsf",
        "accounts": [
            {
                "id": "fop-1",
                "type": "fop",
                "currencyCode": 980,
                "balance": 0,
                "creditLimit": 0,
                "sendId": "s",
                "cashbackType": "UAH",
                "maskedPan": [],
                "iban": "",
            }
        ],
        "jars": [],
    }
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        mock.get("/personal/client-info").mock(return_value=httpx.Response(200, json=payload))
        imp = MonobankImporter("test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa", stub_gate)
        accounts = await imp.discover_accounts()
        await imp.aclose()
    assert len(accounts) == 1
    assert accounts[0].source_kind == "mono.fop"


@pytest.mark.asyncio
async def test_fetch_statement_yields_canonical(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    payload = json.loads((FIXTURES / "statement_two_items.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        mock.get(url__regex=r"/personal/statement/acc-1/.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        imp = MonobankImporter("test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa", stub_gate)
        since = datetime(2026, 5, 9, tzinfo=UTC)
        until = datetime(2026, 5, 10, tzinfo=UTC)
        txs = [t async for t in imp.fetch_statement("acc-1", since, until)]
        await imp.aclose()
    assert len(txs) == 2
    assert txs[0].source_tx_id == "tx-1"
    assert txs[0].amount_minor == -8500
    assert txs[0].currency == "UAH"
    assert txs[1].source_tx_id == "tx-2"
    assert txs[1].amount_minor == 5000000


@pytest.mark.asyncio
async def test_raw_payload_verbatim(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    payload = json.loads((FIXTURES / "statement_two_items.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        imp = MonobankImporter("test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa", stub_gate)
        txs = [
            t
            async for t in imp.fetch_statement(
                "acc-1",
                datetime(2026, 5, 9, tzinfo=UTC),
                datetime(2026, 5, 10, tzinfo=UTC),
            )
        ]
        await imp.aclose()
    for ct, original in zip(txs, payload, strict=True):
        assert ct.raw == original


@pytest.mark.asyncio
async def test_amount_minor_is_int_not_float(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    payload = json.loads((FIXTURES / "statement_two_items.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        imp = MonobankImporter("test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa", stub_gate)
        txs = [
            t
            async for t in imp.fetch_statement(
                "a",
                datetime(2026, 5, 9, tzinfo=UTC),
                datetime(2026, 5, 10, tzinfo=UTC),
            )
        ]
        await imp.aclose()
    for ct in txs:
        assert isinstance(ct.amount_minor, int)
        assert not isinstance(ct.amount_minor, bool)
        assert type(ct.amount_minor) is int


@pytest.mark.asyncio
async def test_calls_through_gate(stub_gate):
    from finance_bro.importers.monobank import MonobankImporter

    client_info_payload = json.loads((FIXTURES / "client_info_minimal.json").read_text())
    with respx.mock(base_url="https://api.monobank.ua") as mock:
        ci = mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=client_info_payload)
        )
        imp = MonobankImporter("test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa", stub_gate)
        await imp.discover_accounts()
        await imp.aclose()
    # Gate was called before HTTP fired
    stub_gate.acquire.assert_awaited_once()
    assert ci.called
