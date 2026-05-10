"""Monobank httpx adapter implementing ImporterProtocol.

Both endpoints (/personal/client-info and /personal/statement) funnel through
the SAME RateLimitGate.acquire() before issuing the HTTP request. The token
rides exclusively in the request header set on the httpx client — URL paths
never contain the token, verified by tests/test_importer_no_token_in_url.py
(Pitfall 7 closed).

Numeric currency codes are mapped to ISO-4217 alpha at the importer boundary
via numeric_to_alpha; nothing downstream sees the numeric form. amount_minor
is always int (no float at this seam — Pitfall 1).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from .base import CanonicalAccount, CanonicalTransaction
from .currency_map import numeric_to_alpha
from .rate_limit import RateLimitGate

MONO_BASE = "https://api.monobank.ua"


class MonobankImporter:
    source_kind = "monobank"

    def __init__(self, token: str, gate: RateLimitGate) -> None:
        self._token = token
        self._gate = gate
        self._client = httpx.AsyncClient(
            base_url=MONO_BASE,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"X-Token": token},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def discover_accounts(self) -> list[CanonicalAccount]:
        await self._gate.acquire(self._token)
        resp = await self._client.get("/personal/client-info")
        resp.raise_for_status()
        data = resp.json()
        out: list[CanonicalAccount] = []
        for acc in data.get("accounts", []):
            kind = "mono.fop" if acc.get("type") == "fop" else "mono.card"
            out.append(
                CanonicalAccount(
                    source_account_id=acc["id"],
                    source_kind=kind,
                    currency=numeric_to_alpha(acc["currencyCode"]),
                    raw=acc,
                )
            )
        for jar in data.get("jars", []):
            out.append(
                CanonicalAccount(
                    source_account_id=jar["id"],
                    source_kind="mono.jar",
                    currency=numeric_to_alpha(jar["currencyCode"]),
                    raw=jar,
                )
            )
        return out

    async def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]:
        await self._gate.acquire(self._token)
        from_ts = int(since.timestamp())
        to_ts = int(until.timestamp())
        resp = await self._client.get(f"/personal/statement/{source_account_id}/{from_ts}/{to_ts}")
        resp.raise_for_status()
        for item in resp.json():
            yield CanonicalTransaction(
                source_tx_id=item["id"],
                source_account_id=source_account_id,
                occurred_at=datetime.fromtimestamp(item["time"], tz=UTC),
                amount_minor=int(item["amount"]),
                currency=numeric_to_alpha(item["currencyCode"]),
                raw=item,
            )
