"""FX-03 — the LATERAL rollup carries the most-recent rate forward (D-13/D-14).

RED scaffold for a later 03 plan (transaction_repo LATERAL rollup not yet built).

Seed fx_rates with ONLY the Friday 2026-05-08 USD row; a USD transaction whose
attributed_day is Sunday 2026-05-10 (no rate published) must resolve to the
Friday rate via `rate_date <= attributed_day ORDER BY rate_date DESC LIMIT 1`,
and be flagged fx_stale because the rate date precedes the transaction day.
"""

import pytest
import pytest_asyncio
from sqlalchemy import text


@pytest_asyncio.fixture(autouse=True)
async def _truncate_fx(session_factory):
    """Hermetic isolation — wipe fx_rates/transactions/accounts before the test
    so a sibling FX test's seeded rate cannot perturb the carry-forward lookup
    (fx_rates is NOT in the conftest truncate list)."""
    async with session_factory() as s:
        await s.execute(
            text("TRUNCATE TABLE fx_rates, transactions, accounts RESTART IDENTITY CASCADE")
        )
        await s.commit()
    yield


@pytest.mark.asyncio
async def test_sunday_tx_uses_friday_rate_and_is_stale(session_factory):
    from finance_bro.db.transaction_repo import TransactionRepo

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'rollup-usd', 'USD', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(text("SELECT id FROM accounts WHERE source_account_id='rollup-usd'"))
        ).scalar_one()
        # Only the Friday rate exists — Sunday is carried forward.
        await s.execute(
            text(
                "INSERT INTO fx_rates (rate_date, currency, rate) "
                "VALUES ('2026-05-08', 'USD', 43.8033)"
            )
        )
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, "
                " raw_payload, attributed_day) "
                "VALUES (:a, 'rj-1', -10000, 'USD', "
                "'2026-05-10T12:00:00+00:00'::timestamptz, "
                "'{\"currencyCode\": 840}'::jsonb, '2026-05-10')"
            ),
            {"a": acc_id},
        )
        await s.commit()

        repo = TransactionRepo(s)
        rows = await repo.list_for_account(acc_id)

    assert len(rows) == 1
    row = rows[0]
    # The carried-forward rate is the Friday 2026-05-08 row.
    assert row["fx_rate_date"].isoformat() == "2026-05-08"
    # Rate date precedes attributed_day -> stale (D-13).
    assert row["fx_stale"] is True
