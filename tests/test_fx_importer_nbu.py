"""FX-02 — NbuFxImporter.fetch_range against the NBU exchange_site endpoint.

RED scaffold for a later 03 plan (NbuFxImporter not yet built). The import
happens inside each test body and the tests are xfail(strict=False) so
collection stays clean while the target does not yet exist.

Locked behavior contract (Pitfall 1 + D-16):
- `exchangedate` (dd.mm.yyyy) parses via %d.%m.%Y to a date.
- `rate` is a Decimal, NEVER a float (parse_float=Decimal discipline).
- An empty NBU array yields [] (weekend-only / unknown ccy -> "no rates", D-16).
- The AsyncClient MUST be closed via aclose() — pyproject filterwarnings=["error"]
  escalates an unclosed client to a hard failure.
"""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
@pytest.mark.xfail(reason="NbuFxImporter impl lands in a later 03 plan", strict=False)
async def test_fetch_range_parses_rows_as_decimal():
    from finance_bro.importers.nbu import NbuFxImporter

    payload = json.loads((FIXTURES / "nbu_usd_range.json").read_text())
    with respx.mock(base_url="https://bank.gov.ua") as mock:
        mock.get(url__regex=r"/NBU_Exchange/exchange_site.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        imp = NbuFxImporter()
        rows = await imp.fetch_range("USD", date(2026, 5, 8), date(2026, 5, 11))
        await imp.aclose()  # MANDATORY — filterwarnings=["error"]

    # The Friday row parses dd.mm.yyyy -> date.
    by_day = {r.rate_date: r for r in rows}
    friday = by_day[date(2026, 5, 8)]
    assert friday.currency == "USD"
    # rate is a Decimal — never a float (Pitfall 1).
    assert isinstance(friday.rate, Decimal)
    assert not isinstance(friday.rate, float)
    assert friday.rate == Decimal("43.8033")


@pytest.mark.asyncio
@pytest.mark.xfail(reason="NbuFxImporter impl lands in a later 03 plan", strict=False)
async def test_fetch_range_empty_yields_empty_list():
    from finance_bro.importers.nbu import NbuFxImporter

    payload = json.loads((FIXTURES / "nbu_empty.json").read_text())
    assert payload == []
    with respx.mock(base_url="https://bank.gov.ua") as mock:
        mock.get(url__regex=r"/NBU_Exchange/exchange_site.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        imp = NbuFxImporter()
        rows = await imp.fetch_range("USD", date(2026, 5, 8), date(2026, 5, 11))
        await imp.aclose()

    assert rows == []  # D-16: no rates -> empty, caller leaves bootstrap_done=false
