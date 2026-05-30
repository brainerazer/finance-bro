"""D-12 / D-13 — a transaction with NO matching fx_rates row still appears in
the list, with null FX fields and fx_stale=True.

RED scaffold for a later 03 plan (LATERAL rollup read path not yet built).

The LEFT JOIN LATERAL must NOT drop the row when no rate exists; instead the
rollup yields uah_amount_minor=None, fx_rate=None, fx_stale=True.
"""
import pytest
from sqlalchemy import text


@pytest.mark.asyncio
@pytest.mark.xfail(reason="stale-fallback rollup lands in a later 03 plan", strict=False)
async def test_no_rate_yields_null_fx_but_row_present(session_factory):
    from finance_bro.db.transaction_repo import TransactionRepo

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'stale-usd', 'USD', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(
                text("SELECT id FROM accounts WHERE source_account_id='stale-usd'")
            )
        ).scalar_one()
        # No fx_rates rows at all for USD.
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, "
                " raw_payload, attributed_day) "
                "VALUES (:a, 'st-1', -2500, 'USD', "
                "'2026-05-08T12:00:00+00:00'::timestamptz, "
                "'{\"currencyCode\": 840}'::jsonb, '2026-05-08')"
            ),
            {"a": acc_id},
        )
        await s.commit()

        repo = TransactionRepo(s)
        rows = await repo.list_for_account(acc_id)

    # Row STILL present even with no rate (LEFT JOIN, not INNER).
    assert len(rows) == 1
    row = rows[0]
    assert row["uah_amount_minor"] is None
    assert row["fx_rate"] is None
    assert row["fx_stale"] is True
