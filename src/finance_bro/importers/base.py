"""Importer port: protocol + canonical dataclasses.

Every concrete importer (Mono today; PrivatBank/Wise/etc. later) implements
ImporterProtocol. The canonical dataclasses are the only shape downstream
services (storage, reconciliation, categorization) ever see — source-specific
quirks are confined to the adapter that produced the raw payload.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class CanonicalAccount:
    source_account_id: str
    source_kind: str
    currency: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str
    source_account_id: str
    occurred_at: datetime
    amount_minor: int
    currency: str
    raw: dict[str, Any]


class ImporterProtocol(Protocol):
    source_kind: str

    async def discover_accounts(self) -> list[CanonicalAccount]: ...

    def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]: ...
