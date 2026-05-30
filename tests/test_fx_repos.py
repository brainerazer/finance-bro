"""FX-02 (Plan 03-02) — FxRateRepo + TrackedFxCurrencyRepo live regression tests.

These are the live PASS coverage for the two repos this plan owns. The Wave-0
scaffolds named in the plan's verify block (test_fx_bootstrap_lazy /
test_fx_stale_fallback) actually depend on the Plan 04 bootstrap service and the
Plan 03 LATERAL rollup respectively, so they stay xfail until those plans land.
This file exercises ONLY the 03-02 artifacts against a real Postgres container.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from finance_bro.db.fx_rate_repo import FxRateRepo
from finance_bro.db.tracked_fx_currency_repo import TrackedFxCurrencyRepo
from finance_bro.importers.base import FxRateRow


@pytest.mark.asyncio
async def test_upsert_many_is_idempotent_and_counts(session_factory):
    rows = [
        FxRateRow(rate_date=date(2026, 5, 8), currency="USD", rate=Decimal("43.8033")),
        FxRateRow(rate_date=date(2026, 5, 11), currency="USD", rate=Decimal("43.9120")),
    ]
    async with session_factory() as s, s.begin():
        repo = FxRateRepo(s)
        await repo.upsert_many(rows)
    # Re-inserting the SAME (rate_date, currency) rows must not error or duplicate
    # (ON CONFLICT DO NOTHING — D-03).
    async with session_factory() as s, s.begin():
        repo = FxRateRepo(s)
        await repo.upsert_many(rows)

    async with session_factory() as s:
        total = (
            await s.execute(text("SELECT count(*) FROM fx_rates WHERE currency = 'USD'"))
        ).scalar_one()
        repo = FxRateRepo(s)
        in_window = await repo.count_in_window("USD", date(2026, 5, 1))
        before_window = await repo.count_in_window("USD", date(2026, 6, 1))
    assert total == 2  # no duplicates despite double upsert
    assert in_window == 2
    assert before_window == 0


@pytest.mark.asyncio
async def test_upsert_many_empty_is_noop(session_factory):
    async with session_factory() as s, s.begin():
        repo = FxRateRepo(s)
        affected = await repo.upsert_many([])
    assert affected == 0


@pytest.mark.asyncio
async def test_tracked_currency_lifecycle(session_factory):
    # first-seen upsert is idempotent (D-15); seeded USD/EUR + first-seen ZZZ/AAA.
    async with session_factory() as s, s.begin():
        repo = TrackedFxCurrencyRepo(s)
        await repo.upsert_currency("ZZZ")
        await repo.upsert_currency("AAA")
        await repo.upsert_currency("ZZZ")  # repeat — no error, no duplicate

    async with session_factory() as s:
        repo = TrackedFxCurrencyRepo(s)
        listed = await repo.list_currencies()
    # D-17: ascending order; migration seeds USD/EUR, we added AAA/ZZZ.
    assert listed == sorted(listed)
    assert "AAA" in listed and "ZZZ" in listed
    assert listed.index("AAA") < listed.index("ZZZ")

    # set_bootstrap_done flips the flag.
    async with session_factory() as s, s.begin():
        repo = TrackedFxCurrencyRepo(s)
        await repo.set_bootstrap_done("ZZZ")
    async with session_factory() as s:
        repo = TrackedFxCurrencyRepo(s)
        zzz = await repo.get("ZZZ")
        assert zzz is not None
        assert zzz.bootstrap_done is True

    # mark_attempted records last_error, then clears it on success (D-08/D-16).
    async with session_factory() as s, s.begin():
        repo = TrackedFxCurrencyRepo(s)
        await repo.mark_attempted("ZZZ", "no rates published")
    async with session_factory() as s:
        repo = TrackedFxCurrencyRepo(s)
        zzz = await repo.get("ZZZ")
        assert zzz is not None
        assert zzz.last_error == "no rates published"
        assert zzz.last_attempted_at is not None

    async with session_factory() as s, s.begin():
        repo = TrackedFxCurrencyRepo(s)
        await repo.mark_attempted("ZZZ", None)
    async with session_factory() as s:
        repo = TrackedFxCurrencyRepo(s)
        zzz = await repo.get("ZZZ")
        assert zzz is not None
        assert zzz.last_error is None
