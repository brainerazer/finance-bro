"""FxRateRepo: idempotent persistence + freshness count for NBU rates.

`upsert_many` keys on the (rate_date, currency) PK with ON CONFLICT DO NOTHING
so re-fetching an overlapping date range (bootstrap re-run, daily tick overlap)
never duplicates or errors (D-03). `count_in_window` feeds the freshness
threshold the bootstrap path uses ("~250 rows in the last 365 days" -> already
backfilled). Rates are Decimal end-to-end -- no float touches a rate (Pitfall 1).
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.importers.base import FxRateRow

from .models import FxRate


class FxRateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert_many(self, rows: list[FxRateRow]) -> int:
        if not rows:
            return 0
        values = [{"rate_date": r.rate_date, "currency": r.currency, "rate": r.rate} for r in rows]
        stmt = insert(FxRate).values(values)
        stmt = stmt.on_conflict_do_nothing(index_elements=["rate_date", "currency"])
        result = await self._s.execute(stmt)
        return result.rowcount

    async def count_in_window(self, currency: str, since_date: date) -> int:
        result = await self._s.execute(
            text("SELECT count(*) FROM fx_rates WHERE currency = :ccy AND rate_date >= :since"),
            {"ccy": currency, "since": since_date},
        )
        row = result.first()
        return int(row[0]) if row else 0
