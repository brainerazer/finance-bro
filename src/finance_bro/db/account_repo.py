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

    async def list_pollable_cards(self) -> list[Account]:
        """Active polling set per D-01 + D-02: mono.card with type ∈ {black, platinum,
        white}, ordered by id ASC for deterministic round-robin (D-02). Fail-closed:
        any other mono_type (eAid, future iron/yellow/etc.) is excluded."""
        rows = (
            await self._s.execute(
                select(Account)
                .where(Account.source_kind == "mono.card")
                .where(Account.mono_type.in_(["black", "platinum", "white"]))
                .order_by(Account.id.asc())
            )
        ).scalars().all()
        return list(rows)

    async def upsert_many(self, items: list[CanonicalAccount]) -> int:
        if not items:
            return 0
        rows = [
            {
                "source_kind": a.source_kind,
                "source_account_id": a.source_account_id,
                "currency": a.currency,
                "raw_payload": a.raw,
                # NEW (02-01 T3): bridge between accounts.mono_type column and the
                # CanonicalAccount.mono_type field that 02-03 T1 will add. Use
                # getattr so this code stays compatible with pre-02-03 callers
                # (CanonicalAccount has no mono_type yet); migration 0002 already
                # backfilled existing rows from raw_payload->>'type'.
                "mono_type": getattr(a, "mono_type", None),
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
