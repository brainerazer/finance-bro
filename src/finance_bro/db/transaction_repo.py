"""Transaction repository — single owner of writes/reads against `transactions`.

`insert_many` uses `INSERT ... ON CONFLICT (account_id, source_tx_id) WHERE NOT
is_deleted DO NOTHING` against the partial unique index `uq_transactions_account_source_tx`
declared in migration 0001 (ING-04). The `RETURNING id` clause lets us count
exactly how many rows were inserted (rows skipped by the conflict are not
returned), which the import service surfaces as `skipped_duplicates =
statement_count - inserted` (SC#3).
"""

from sqlalchemy import select, text
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
    ) -> int:
        """Insert canonical transactions idempotently. Returns the count of rows
        actually inserted; skipped duplicates are not counted (the partial
        unique index from migration 0001 guarantees the (account_id,
        source_tx_id) WHERE NOT is_deleted invariant)."""
        if not items:
            return 0
        rows = [
            {
                "account_id": account_id,
                "source_tx_id": t.source_tx_id,
                "amount_minor": t.amount_minor,
                "currency": t.currency,
                "time": t.occurred_at,
                "raw_payload": t.raw,
            }
            for t in items
        ]
        stmt = (
            insert(Transaction)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["account_id", "source_tx_id"],
                index_where=text("NOT is_deleted"),
            )
            .returning(Transaction.id)
        )
        result = await self._s.execute(stmt)
        returned = result.scalars().all()
        return len(returned)

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
