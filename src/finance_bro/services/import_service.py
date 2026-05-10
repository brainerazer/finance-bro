"""ImportService — orchestrates the Mono import slice for Phase 1.

Phase 1 polls one card (D-04). The first call discovers accounts via
`/personal/client-info` (D-03 lazy validation, D-06 one-shot), persists every
account Mono returns (cards + jars + FOPs — D-05), then polls the lowest-id
`mono.card` for a 31-day statement window (Pitfall 5: Mono caps at 31d+1h).
Subsequent calls skip discovery — accounts come from the DB.

Idempotency comes from the partial unique index on `(account_id, source_tx_id)
WHERE NOT is_deleted`; `TransactionRepo.insert_many` uses ON CONFLICT DO
NOTHING and returns the exact inserted count (SC#3 — second POST is a no-op).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_bro.db.account_repo import AccountRepo
from finance_bro.db.transaction_repo import TransactionRepo
from finance_bro.importers.monobank import MonobankImporter

MONO_STATEMENT_WINDOW_DAYS = 31  # Pitfall 5


@dataclass(frozen=True)
class ImportResult:
    polled_account_id: str
    statement_count: int
    inserted: int
    skipped_duplicates: int


class NoCardAccountFound(RuntimeError):
    """Raised when client-info returned no usable account or the DB has no
    mono.card row to poll. Phase 1 polls only cards (D-04)."""


class ImportService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        importer: MonobankImporter,
    ) -> None:
        self._session_factory = session_factory
        self._importer = importer

    async def run_one_card(self, now: datetime | None = None) -> ImportResult:
        now = now or datetime.now(UTC)
        since = now - timedelta(days=MONO_STATEMENT_WINDOW_DAYS)

        # Step 1: ensure accounts table is populated (lazy, D-03 + D-06)
        async with self._session_factory() as session, session.begin():
            accounts = await AccountRepo(session).list_all()
            have_accounts = bool(accounts)

        if not have_accounts:
            discovered = await self._importer.discover_accounts()
            if not discovered:
                raise NoCardAccountFound("Mono /client-info returned no accounts")
            async with self._session_factory() as session, session.begin():
                await AccountRepo(session).upsert_many(discovered)

        # Step 2: pick the first card (D-04)
        async with self._session_factory() as session:
            card = await AccountRepo(session).get_first_card()
        if card is None:
            raise NoCardAccountFound(
                "No mono.card account found after discovery. Phase 1 polls only cards (D-04)."
            )

        # Step 3: fetch statement (gate enforced inside importer)
        items = [
            t
            async for t in self._importer.fetch_statement(
                card.source_account_id,
                since,
                now,
            )
        ]

        # Step 4: idempotent insert
        async with self._session_factory() as session, session.begin():
            inserted = await TransactionRepo(session).insert_many(card.id, items)

        return ImportResult(
            polled_account_id=card.source_account_id,
            statement_count=len(items),
            inserted=inserted,
            skipped_duplicates=len(items) - inserted,
        )
