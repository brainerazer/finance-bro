"""Importer port: protocol + canonical dataclasses.

Every concrete importer (Mono today; PrivatBank/Wise/etc. later) implements
ImporterProtocol. The canonical dataclasses are the only shape downstream
services (storage, reconciliation, categorization) ever see — source-specific
quirks are confined to the adapter that produced the raw payload.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class CanonicalAccount:
    source_account_id: str
    source_kind: str
    currency: str
    raw: dict[str, Any]
    # NEW (02-03 T1): mono_type extracted from acc.get("type") for cards;
    # None for jars/FOPs. Drives D-01 fail-closed allowlist filter
    # (AccountRepo.list_pollable_cards) and the 02-04 status surface.
    mono_type: str | None = None


@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str
    source_account_id: str
    occurred_at: datetime
    amount_minor: int
    currency: str
    raw: dict[str, Any]
    # Optional fields (defaulted) populated by the importer on first INSERT only.
    # On UPDATE (hold→cleared), the upsert clause does NOT mutate these — D-10
    # frozen-by-omission invariant keeps Phase 4-6 manual edits intact. Plan 02-03
    # wires Mono payloads -> these fields; Plan 02-02 only plumbs them through.
    hold: bool = False
    description: str | None = None
    mcc: int | None = None
    # Kyiv calendar day the transaction is attributed to (D-09). The importer
    # derives it from `occurred_at` at the source boundary (Plan 03-03). When the
    # importer leaves it None, `TransactionRepo.insert_many` derives it from
    # `occurred_at` as a safety net so the NOT NULL column is always populated.
    # Frozen-on-first-write: absent from the upsert SET clause.
    attributed_day: date | None = None


class ImporterProtocol(Protocol):
    source_kind: str

    async def discover_accounts(self) -> list[CanonicalAccount]: ...

    def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]: ...


@dataclass(frozen=True)
class FxRateRow:
    """One (date, currency) NBU rate. `rate` is always a Decimal — never a
    float (Pitfall 1). Produced by FxRatesPort.fetch_range, persisted by
    FxRateRepo.upsert_many (D-02)."""

    rate_date: date
    currency: str
    rate: Decimal


class FxRatesPort(Protocol):
    """The FX-source port (D-02). NbuFxImporter is the only implementation in
    v1; a future ECB/other source slots in behind the same contract without
    touching the rollup or bootstrap callers."""

    async def fetch_range(self, currency: str, start: date, end: date) -> list[FxRateRow]: ...
