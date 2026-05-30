"""ImportService — orchestrates the Mono import slice for Phase 1.

Phase 1 polls one card (D-04). The first call discovers accounts via
`/personal/client-info` (D-03 lazy validation, D-06 one-shot), persists every
account Mono returns (cards + jars + FOPs — D-05), then polls the lowest-id
`mono.card` for a 31-day statement window (Pitfall 5: Mono caps at 31d+1h).
Subsequent calls skip discovery — accounts come from the DB.

Idempotency comes from the partial unique index on `(account_id, source_tx_id)
WHERE NOT is_deleted`; from Phase 2 (Plan 02-02), `TransactionRepo.insert_many`
uses ON CONFLICT DO UPDATE and returns `(inserted, updated_in_place)`; this
service folds them into the Phase 1 `ImportResultOut.inserted` field
(`inserted_total = inserted + updated`) so the route shape stays unchanged
until Plan 02-04 reshapes it (D-16). SC#3 — second POST is still a user-visible
no-op (one row per Mono id), even though the SQL underneath is now an UPDATE.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_bro.categorizer import categorize_rows, compile_rules
from finance_bro.categorizer.engine import RuleRowLike
from finance_bro.db.account_repo import AccountRepo
from finance_bro.db.rule_repo import RuleRepo
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

        # Step 4: idempotent upsert (Phase 2: ON CONFLICT DO UPDATE; D-10 mutates
        # only hold/amount_minor/raw_payload — all other columns frozen by omission).
        async with self._session_factory() as session, session.begin():
            tx_repo = TransactionRepo(session)
            inserted, updated = await tx_repo.insert_many(card.id, items)
            # Step 4b: auto-categorize the touched rows (D-10/D-11). Reuse the
            # PURE engine verbatim — the same call the Plan 04 history sweep makes.
            # Locked rows are excluded twice over: fetch_for_categorize filters
            # `NOT is_user_locked` in SQL (D-09) AND the engine returns SKIP. The
            # frozen-by-omission upsert above already guarantees a re-import can
            # never clobber category columns, so this step is purely additive and
            # leaves a manual lock intact (CAT-04). Touched ids come from the items
            # just upserted (Open Question 2: import categorizes only touched rows).
            # ORM `Rule` rows satisfy RuleRowLike at runtime (Mapped[int] -> int,
            # Mapped[dict] -> dict on instance access); cast at this boundary.
            rules = compile_rules(
                cast("list[RuleRowLike]", await RuleRepo(session).list_active_ordered())
            )
            rows = await tx_repo.fetch_for_categorize(
                card.id, touched_source_tx_ids=[t.source_tx_id for t in items]
            )
            updates = categorize_rows(rows, rules)
            await tx_repo.apply_categories(updates)
        # Phase 1 ImportResultOut shape preserved (Plan 02-04 reshapes the route).
        # `inserted_total` accounts for both first-insert rows and hold→cleared updates;
        # Phase 1's "second-import is a no-op" semantics still hold for the user (one row
        # per Mono id), but the second call now reports inserted_total=N (all updated)
        # rather than inserted=0 (all skipped). The user-visible row count is unchanged.
        inserted_total = inserted + updated

        return ImportResult(
            polled_account_id=card.source_account_id,
            statement_count=len(items),
            inserted=inserted_total,
            skipped_duplicates=len(items) - inserted_total,
        )
