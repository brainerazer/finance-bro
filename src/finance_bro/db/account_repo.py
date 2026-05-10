"""Account repository — single owner of writes/reads against the `accounts` table.

Used by ImportService (lazy discovery), GET /api/accounts, and GET
/api/transactions (which picks the first card to scope the read). The
`upsert_many` path uses the `uq_accounts_source` constraint declared in
migration 0001 so re-running discovery is idempotent (D-06).
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.db.models import Account
from finance_bro.importers.base import CanonicalAccount


class AccountRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_all(self) -> list[Account]:
        rows = (await self._s.execute(select(Account).order_by(Account.id.asc()))).scalars().all()
        return list(rows)

    async def get_first_card(self) -> Account | None:
        return (
            await self._s.execute(
                select(Account)
                .where(Account.source_kind == "mono.card")
                .order_by(Account.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def upsert_many(self, items: list[CanonicalAccount]) -> int:
        if not items:
            return 0
        rows = [
            {
                "source_kind": a.source_kind,
                "source_account_id": a.source_account_id,
                "currency": a.currency,
                "raw_payload": a.raw,
            }
            for a in items
        ]
        stmt = (
            insert(Account)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_accounts_source")
            .returning(Account.id)
        )
        result = await self._s.execute(stmt)
        returned = result.scalars().all()
        return len(returned)
