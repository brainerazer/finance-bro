"""Monobank httpx adapter implementing ImporterProtocol.

Both endpoints (/personal/client-info and /personal/statement) funnel through
the SAME RateLimitGate.acquire() before issuing the HTTP request. The token
rides exclusively in the request header set on the httpx client — URL paths
never contain the token, verified by tests/test_importer_no_token_in_url.py
(Pitfall 7 closed).

Numeric currency codes are mapped to ISO-4217 alpha at the importer boundary
via numeric_to_alpha; nothing downstream sees the numeric form. amount_minor
is always int (no float at this seam — Pitfall 1).

Phase 2 (Plan 02-03): both methods translate `httpx.HTTPStatusError` into
typed exceptions (`MonoAuthError` on 401, `MonoRateLimitError` on 429,
`MonoTransientError` otherwise) so the SchedulerRunner branches on intent
instead of HTTP status strings (RESEARCH.md Pattern 4 + D-15). The
`gate.acquire(self._token)` call MUST remain the FIRST line of each method —
PATTERNS.md Pattern S7 invariant — even though the typed-exception wrap is
new.

Plan 02-03 also populates `CanonicalAccount.mono_type` (cards only) and
`CanonicalTransaction.hold/description/mcc` from each Mono payload so D-01
allowlist filtering and D-10 hold-aware upsert have data to work with.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from finance_bro.scheduler.errors import (
    MonoAuthError,
    MonoRateLimitError,
    MonoTransientError,
)

from .base import CanonicalAccount, CanonicalTransaction
from .currency_map import numeric_to_alpha
from .rate_limit import RateLimitGate

MONO_BASE = "https://api.monobank.ua"
# D-09: the Kyiv calendar day a transaction is attributed to. Derived from the
# SAME `time` field as occurred_at, converted UTC -> Europe/Kyiv (DST-aware).
KYIV = ZoneInfo("Europe/Kyiv")


def _retry_after_seconds(resp: httpx.Response) -> int | None:
    """Parse the Retry-After header as integer seconds. Mono sometimes omits it
    (Plan 02-03 must_haves.truths line: '429 (with Retry-After if present)')."""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None


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
        await self._gate.acquire(self._token)  # Pattern S7: gate FIRST.
        resp = await self._client.get("/personal/client-info")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise MonoAuthError("Mono token rejected (401)") from e
            if status == 429:
                raise MonoRateLimitError(_retry_after_seconds(e.response)) from e
            raise MonoTransientError(f"Mono {status}") from e
        data = resp.json()
        out: list[CanonicalAccount] = []
        for acc in data.get("accounts", []):
            kind = "mono.fop" if acc.get("type") == "fop" else "mono.card"
            mono_type = acc.get("type") if kind == "mono.card" else None
            out.append(
                CanonicalAccount(
                    source_account_id=acc["id"],
                    source_kind=kind,
                    currency=numeric_to_alpha(acc["currencyCode"]),
                    raw=acc,
                    mono_type=mono_type,
                )
            )
        for jar in data.get("jars", []):
            out.append(
                CanonicalAccount(
                    source_account_id=jar["id"],
                    source_kind="mono.jar",
                    currency=numeric_to_alpha(jar["currencyCode"]),
                    raw=jar,
                    mono_type=None,
                )
            )
        return out

    async def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]:
        await self._gate.acquire(self._token)  # Pattern S7: gate FIRST.
        from_ts = int(since.timestamp())
        to_ts = int(until.timestamp())
        resp = await self._client.get(f"/personal/statement/{source_account_id}/{from_ts}/{to_ts}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                raise MonoAuthError("Mono token rejected (401)") from e
            if status == 429:
                raise MonoRateLimitError(_retry_after_seconds(e.response)) from e
            raise MonoTransientError(f"Mono {status}") from e
        for item in resp.json():
            occurred_at = datetime.fromtimestamp(item["time"], tz=UTC)
            yield CanonicalTransaction(
                source_tx_id=item["id"],
                source_account_id=source_account_id,
                occurred_at=occurred_at,
                amount_minor=int(item["amount"]),
                currency=numeric_to_alpha(item["currencyCode"]),
                raw=item,
                hold=item.get("hold", False),
                description=item.get("description"),
                mcc=item.get("mcc"),
                # D-09: Kyiv calendar day, frozen on first write (absent from the
                # upsert SET clause). Derived from the same `time` as occurred_at.
                attributed_day=occurred_at.astimezone(KYIV).date(),
            )
