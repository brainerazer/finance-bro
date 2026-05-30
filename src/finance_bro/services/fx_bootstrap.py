"""FxBootstrapService — idempotent, lazy 12-month NBU backfill (D-03/D-07).

On first boot (and re-run by the daily ``fx_tick`` when a currency is still
incomplete) this service backfills ~12 months of NBU rates for every tracked
currency so the rollup read path resolves a rate for any in-window transaction.

Lifecycle per currency (``maybe_bootstrap_fx``):
1. Count the rates already present in the last 365 days. If we already hold
   ``>= BOOTSTRAP_THRESHOLD`` rows the currency is considered backfilled — no
   fetch (idempotent / cheap re-entry).
2. Otherwise fetch the ~12-month range from NBU OUTSIDE any open DB session
   (a slow NBU response must never hold a transaction open).
3. On an empty result or a fetch failure, record ``last_error`` and leave
   ``bootstrap_done`` false (D-16) — the next tick retries. The failure surface
   is logs + ``tracked_fx_currencies.last_error`` ONLY; it NEVER touches
   ``scheduler_state`` (D-08 — that row is the Mono poll cursor).
4. On success, upsert the rows (ON CONFLICT DO NOTHING — D-03 idempotent),
   flip ``bootstrap_done`` true, and clear ``last_error``.

``maybe_bootstrap_fx_all_tracked`` iterates the tracked currencies SEQUENTIALLY
(not gathered — D-07/D-17) so one slow/failing currency cannot stampede NBU or
abort the others.
"""

from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_bro.db.fx_rate_repo import FxRateRepo
from finance_bro.db.tracked_fx_currency_repo import TrackedFxCurrencyRepo
from finance_bro.importers.base import FxRatesPort

# A full year of NBU publications is ~250 business days. Holding at least this
# many rows in the trailing 365 days means the 12-month window is backfilled.
BOOTSTRAP_THRESHOLD = 250
# The lazy backfill window. 366 days covers a full year incl. the leap-day edge.
BOOTSTRAP_WINDOW = timedelta(days=366)
FRESHNESS_WINDOW = timedelta(days=365)

_log = structlog.get_logger()


class FxBootstrapService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        importer: FxRatesPort,
    ) -> None:
        self._session_factory = session_factory
        self._importer = importer

    def _today(self) -> date:
        return datetime.now(UTC).date()

    async def maybe_bootstrap_fx(self, currency: str) -> None:
        """Idempotent lazy 12-month backfill for one currency (D-03)."""
        today = self._today()

        # 1. Freshness check (session block). Already backfilled -> no-op.
        async with self._session_factory() as session, session.begin():
            count = await FxRateRepo(session).count_in_window(currency, today - FRESHNESS_WINDOW)
        if count >= BOOTSTRAP_THRESHOLD:
            _log.info("fx.bootstrap.skip", currency=currency, count=count)
            return

        # 2. HTTP fetch OUTSIDE any session (a slow NBU call must not hold a tx).
        try:
            rows = await self._importer.fetch_range(currency, today - BOOTSTRAP_WINDOW, today)
        except Exception as exc:  # any NBU failure is logs+last_error only (D-08)
            async with self._session_factory() as session, session.begin():
                await TrackedFxCurrencyRepo(session).mark_attempted(currency, str(exc))
            _log.warning("fx.bootstrap.fetch_failed", currency=currency, error=str(exc))
            return

        if not rows:
            # D-16: empty result -> record last_error, leave bootstrap_done false.
            async with self._session_factory() as session, session.begin():
                await TrackedFxCurrencyRepo(session).mark_attempted(currency, "no rates published")
            _log.info("fx.bootstrap.empty", currency=currency)
            return

        # 3. Persist + flip the bootstrap flag + clear last_error (session block).
        async with self._session_factory() as session, session.begin():
            inserted = await FxRateRepo(session).upsert_many(rows)
            tracked = TrackedFxCurrencyRepo(session)
            await tracked.set_bootstrap_done(currency)
            await tracked.mark_attempted(currency, None)
        _log.info("fx.bootstrap.done", currency=currency, rows=inserted)

    async def maybe_bootstrap_fx_all_tracked(self) -> None:
        """Backfill every tracked currency SEQUENTIALLY (D-07/D-17).

        One currency's exception is logged and does NOT abort the others.
        Never touches scheduler_state (D-08).
        """
        async with self._session_factory() as session, session.begin():
            currencies = await TrackedFxCurrencyRepo(session).list_currencies()
        for currency in currencies:
            try:
                await self.maybe_bootstrap_fx(currency)
            except Exception:  # per-currency isolation: log + continue
                _log.exception("fx.bootstrap.currency_failed", currency=currency)
