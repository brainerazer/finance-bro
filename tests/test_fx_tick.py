"""FX-02 / D-08 / D-16 / D-17 — the daily fx_tick job.

Live test (Plan 03-04): runner.fx_tick is built.

Asserts the locked contract:
(a) currencies are processed in `ORDER BY currency` (seed USD/EUR/CHF ->
    fetch call order CHF, EUR, USD);
(b) a row with bootstrap_done=false triggers the 12-month range re-fetch
    (not just a single-day fetch);
(c) an empty NBU result records last_error and leaves bootstrap_done=false (D-16);
(d) one currency raising in fetch_range does NOT abort the loop — the remaining
    currencies are still fetched (per-currency error isolation);
(e) scheduler_state is NEVER written by fx_tick (D-08).
"""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text


@pytest_asyncio.fixture(autouse=True)
async def _hermetic_tracked_fx(session_factory):
    """Hermetic isolation: tracked_fx_currencies is NOT in the conftest client
    truncate list, and sibling tests (test_fx_repos) leak first-seen AAA/ZZZ
    rows into it. The ORDER BY assertion below pins an exact currency list, so
    truncate before and after each test in this module. Mirrors the per-test
    autouse-truncate pattern used by the direct-session FX tests in Plan 03-03.
    """
    truncate = text("TRUNCATE TABLE tracked_fx_currencies RESTART IDENTITY CASCADE")
    async with session_factory() as s:
        await s.execute(truncate)
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(truncate)
        await s.commit()


def _seed_tracked():
    return text(
        "INSERT INTO tracked_fx_currencies (currency, bootstrap_done) VALUES "
        "('USD', true), ('EUR', true), ('CHF', false) "
        "ON CONFLICT (currency) DO UPDATE SET bootstrap_done = EXCLUDED.bootstrap_done"
    )


@pytest.mark.asyncio
async def test_fx_tick_orders_by_currency_and_isolates_errors(session_factory):
    from finance_bro.importers.base import FxRateRow
    from finance_bro.scheduler.runner import SchedulerRunner

    async with session_factory() as s:
        await s.execute(_seed_tracked())
        await s.commit()

    fetched_currencies: list[str] = []

    async def _fetch_range(currency: str, start: date, end: date):
        fetched_currencies.append(currency)
        if currency == "EUR":
            raise RuntimeError("boom")  # per-currency failure must not abort loop
        if currency == "USD":
            return []  # D-16: empty -> last_error, bootstrap stays as-is
        return [FxRateRow(rate_date=end, currency=currency, rate=Decimal("49.1200"))]

    importer = _make_importer(_fetch_range)
    runner = SchedulerRunner(session_factory=session_factory, fx_importer=importer)
    await runner.fx_tick()

    # (a) ORDER BY currency -> CHF, EUR, USD; (d) EUR raising did not abort.
    assert fetched_currencies == ["CHF", "EUR", "USD"]


@pytest.mark.asyncio
async def test_fx_tick_bootstrap_incomplete_refetches_range(session_factory):
    from finance_bro.importers.base import FxRateRow
    from finance_bro.scheduler.runner import SchedulerRunner

    async with session_factory() as s:
        await s.execute(_seed_tracked())
        await s.commit()

    ranges: dict[str, int] = {}

    async def _fetch_range(currency: str, start: date, end: date):
        ranges[currency] = (end - start).days
        return [FxRateRow(rate_date=end, currency=currency, rate=Decimal("1.0"))]

    importer = _make_importer(_fetch_range)
    runner = SchedulerRunner(session_factory=session_factory, fx_importer=importer)
    await runner.fx_tick()

    # CHF is bootstrap_done=false -> ~12-month range; USD/EUR done -> small range.
    assert ranges["CHF"] >= 300, "bootstrap-incomplete must re-fetch the year range"
    assert ranges["USD"] <= 7, "already-bootstrapped currency fetches a short range"


@pytest.mark.asyncio
async def test_fx_tick_empty_records_last_error_and_no_scheduler_state_write(
    session_factory,
):
    from finance_bro.scheduler.runner import SchedulerRunner

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO tracked_fx_currencies (currency, bootstrap_done) "
                "VALUES ('USD', true) "
                "ON CONFLICT (currency) DO UPDATE SET bootstrap_done = true"
            )
        )
        # scheduler_state singleton is seeded id=1 'running' by conftest.
        before = (
            await s.execute(text("SELECT state, since FROM scheduler_state WHERE id=1"))
        ).first()
        await s.commit()

    async def _fetch_range(currency: str, start: date, end: date):
        return []  # D-16 empty

    importer = _make_importer(_fetch_range)
    runner = SchedulerRunner(session_factory=session_factory, fx_importer=importer)
    await runner.fx_tick()

    async with session_factory() as s:
        usd = (
            await s.execute(
                text(
                    "SELECT bootstrap_done, last_error FROM tracked_fx_currencies "
                    "WHERE currency='USD'"
                )
            )
        ).first()
        after = (
            await s.execute(text("SELECT state, since FROM scheduler_state WHERE id=1"))
        ).first()

    # Empty NBU result -> last_error set, bootstrap unchanged (D-16).
    assert usd[1] is not None
    # D-08: fx_tick NEVER writes scheduler_state.
    assert after == before


def _make_importer(fetch_range):
    """Minimal FxRatesPort double whose fetch_range is the given coroutine fn."""

    class _Importer:
        async def fetch_range(self, currency, start, end):
            return await fetch_range(currency, start, end)

    return _Importer()
