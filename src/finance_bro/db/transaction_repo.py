"""Transaction repository — single owner of writes/reads against `transactions`.

`insert_many` uses `INSERT ... ON CONFLICT (account_id, source_tx_id) WHERE NOT
is_deleted DO UPDATE SET hold = EXCLUDED.hold, amount_minor = EXCLUDED.amount_minor,
raw_payload = EXCLUDED.raw_payload` against the partial unique index
`uq_transactions_account_source_tx` (migration 0001). On conflict EXACTLY THREE
columns mutate (D-10): every other column — including manual-edit columns from
Phases 4-6 (is_user_locked / category_id / category_source / description / mcc /
attributed_day) — is FROZEN BY OMISSION. The `(xmax = 0)` returning trick
distinguishes inserts from updates so the runner can log both counts.
"""

from sqlalchemy import literal_column, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.db.models import Transaction
from finance_bro.importers.base import CanonicalTransaction


class TransactionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_many(
        self,
        account_id: int,
        items: list[CanonicalTransaction],
    ) -> tuple[int, int]:
        """Upsert canonical transactions idempotently.

        On conflict (i.e., a row with the same (account_id, source_tx_id) WHERE NOT
        is_deleted already exists), the upsert mutates EXACTLY THREE columns:
        `hold`, `amount_minor`, `raw_payload` (D-10). All other columns — currency,
        time, account_id, source_tx_id, created_at, is_user_locked, category_id,
        category_source, is_deleted, description, mcc, attributed_day — are FROZEN
        BY OMISSION. Phase 1's Pitfall-10 promise that the importer never overwrites
        manual edits stays a hard invariant.

        Returns `(inserted, updated_in_place)`. The `xmax = 0` trick: PostgreSQL's
        `xmax` system column is 0 on freshly-inserted rows; ON CONFLICT DO UPDATE
        sets it to the current transaction id. RESEARCH.md Pattern 3 + Pitfall 6.
        """
        if not items:
            return (0, 0)
        rows = [
            {
                "account_id": account_id,
                "source_tx_id": t.source_tx_id,
                "amount_minor": t.amount_minor,
                "currency": t.currency,
                "time": t.occurred_at,
                "raw_payload": t.raw,
                # On first INSERT, the importer is allowed to populate description/mcc
                # (Discretion bullet 8 + PATTERNS.md transformation §2). They become
                # immutable after the row exists because they are absent from the
                # on-conflict update clause below — D-10 frozen-by-omission.
                "description": t.description,
                "mcc": t.mcc,
                "hold": t.hold,
            }
            for t in items
        ]
        stmt = insert(Transaction).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "source_tx_id"],
            index_where=text("NOT is_deleted"),
            set_={
                "hold": stmt.excluded.hold,
                "amount_minor": stmt.excluded.amount_minor,
                "raw_payload": stmt.excluded.raw_payload,
            },
        ).returning(
            Transaction.id,
            literal_column("(xmax = 0)").label("inserted"),
        )
        result = await self._s.execute(stmt)
        rows_back = result.all()
        inserted = sum(1 for r in rows_back if r.inserted)
        updated = len(rows_back) - inserted
        return (inserted, updated)

    async def list_for_account(self, account_id: int) -> list[Transaction]:
        rows = (
            (
                await self._s.execute(
                    select(Transaction)
                    .where(Transaction.account_id == account_id)
                    .where(Transaction.is_deleted.is_(False))
                    .order_by(Transaction.time.desc())
                )
            )
            .scalars()
            .all()
        )
        return list(rows)
