"""TrackedFxCurrencyRepo: per-currency FX-bootstrap lifecycle.

Tracks which currencies the FX importer must backfill and their state. The
lifecycle (D-05/D-15/D-16/D-17):
- `upsert_currency` registers a never-before-seen currency on first sight
  (ON CONFLICT DO NOTHING -- idempotent first-seen, D-15).
- `list_currencies` iterates them in a stable ascending order (D-17) so the
  cron tick is deterministic.
- `set_bootstrap_done` flips the flag once a currency is fully backfilled.
- `mark_attempted` records last_attempted_at + last_error ONLY (D-08). The FX
  failure surface NEVER touches scheduler_state -- that row is the Mono poll
  cursor and must stay isolated from FX failures.
"""

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import TrackedFxCurrency


class TrackedFxCurrencyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_currencies(self) -> list[str]:
        result = await self._s.execute(
            select(TrackedFxCurrency.currency).order_by(TrackedFxCurrency.currency)
        )
        return list(result.scalars().all())

    async def get(self, currency: str) -> TrackedFxCurrency | None:
        result = await self._s.execute(
            select(TrackedFxCurrency).where(TrackedFxCurrency.currency == currency)
        )
        return result.scalar_one_or_none()

    async def upsert_currency(self, currency: str) -> None:
        stmt = insert(TrackedFxCurrency).values(currency=currency)
        stmt = stmt.on_conflict_do_nothing(index_elements=["currency"])
        await self._s.execute(stmt)

    async def set_bootstrap_done(self, currency: str) -> None:
        await self._s.execute(
            text("UPDATE tracked_fx_currencies SET bootstrap_done = true WHERE currency = :ccy"),
            {"ccy": currency},
        )

    async def mark_attempted(self, currency: str, last_error: str | None) -> None:
        await self._s.execute(
            text(
                "UPDATE tracked_fx_currencies "
                "SET last_attempted_at = now(), last_error = :err "
                "WHERE currency = :ccy"
            ),
            {"err": last_error, "ccy": currency},
        )
