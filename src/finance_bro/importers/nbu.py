"""NBU FX adapter implementing FxRatesPort.

Fetches official NBU rates for one currency over a date range in a single call
to the `exchange_site` endpoint (D-01, D-02). NBU has no token: there is NO
X-Token header and NO RateLimitGate — unlike the Mono importer. The base URL is
a hardcoded constant (no user input in the URL — SSRF mitigation T-03-06).

Rates are parsed with `json.loads(resp.text, parse_float=Decimal)` so a float
NEVER touches a rate value (Pitfall 1 / T-03-04). The empty NBU array (weekend-
only window or unknown currency) yields `[]` — "no rates", not an error (D-16),
so the caller leaves `bootstrap_done` false and retries later.

The httpx client is owned by this importer and MUST be closed via `aclose()`;
the test suite runs under `filterwarnings = ["error"]`, which escalates an
unclosed client to a hard failure.
"""

import json
from datetime import date, datetime
from decimal import Decimal

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .base import FxRateRow

NBU_BASE = "https://bank.gov.ua/NBU_Exchange/exchange_site"


class NbuFxImporter:
    source_kind = "nbu"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=NBU_BASE,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=30),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        reraise=True,
    )
    async def fetch_range(self, currency: str, start: date, end: date) -> list[FxRateRow]:
        resp = await self._client.get(
            "",
            params={
                "start": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "valcode": currency,
                "json": "",
            },
        )
        resp.raise_for_status()
        # NEVER resp.json() — parse_float=Decimal keeps every rate exact (Pitfall 1).
        raw = json.loads(resp.text, parse_float=Decimal)
        if raw == []:
            return []  # D-16: no rates -> empty list, caller leaves bootstrap_done=false
        rows: list[FxRateRow] = []
        for r in raw:
            # Defensive: NBU normally reports units==1; if not, rate_per_unit is the
            # per-1-unit value (Assumptions Log A3).
            units = r.get("units", 1)
            rate_value = r["rate_per_unit"] if units != 1 else r["rate"]
            rows.append(
                FxRateRow(
                    rate_date=datetime.strptime(r["exchangedate"], "%d.%m.%Y").date(),
                    currency=r["cc"],
                    rate=Decimal(str(rate_value)),
                )
            )
        return rows
