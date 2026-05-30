"""FX-04 — card transactions in a non-account currency use account-currency ×
NBU, NEVER triangulation via operationAmount (Pitfall 2 / D-11).

RED scaffold for a later 03 plan.

An account is EUR; a transaction's raw_payload.currencyCode is 840 (USD merchant
operation). The rollup must label fx_source == "mono_card" and compute
uah_amount_minor = EUR amount_minor × NBU EUR/UAH rate — i.e. it converts the
ACCOUNT currency (EUR) by the NBU EUR rate, not the USD operation amount.
"""
from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
@pytest.mark.xfail(reason="mono_card rollup labelling lands in a later 03 plan", strict=False)
async def test_card_foreign_op_uses_account_currency_nbu(session_factory):
    from finance_bro.db.transaction_repo import TransactionRepo

    eur_rate = Decimal("47.2500")
    amount_minor = -5000  # -50.00 EUR
    expected_uah_minor = int(
        ((Decimal(amount_minor) / 100) * eur_rate).quantize(
            Decimal("0.01"), ROUND_HALF_EVEN
        )
        * 100
    )

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'card-eur', 'EUR', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(
                text("SELECT id FROM accounts WHERE source_account_id='card-eur'")
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO fx_rates (rate_date, currency, rate) "
                "VALUES ('2026-05-08', 'EUR', 47.2500)"
            )
        )
        # currencyCode 840 (USD) != account currency EUR -> mono_card label,
        # but the math uses the EUR account amount × NBU EUR rate.
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, "
                " raw_payload, attributed_day) "
                "VALUES (:a, 'card-1', :amt, 'EUR', "
                "'2026-05-08T12:00:00+00:00'::timestamptz, "
                "'{\"currencyCode\": 840}'::jsonb, '2026-05-08')"
            ),
            {"a": acc_id, "amt": amount_minor},
        )
        await s.commit()

        repo = TransactionRepo(s)
        rows = await repo.list_for_account(acc_id)

    assert len(rows) == 1
    row = rows[0]
    assert row["fx_source"] == "mono_card"
    assert row["uah_amount_minor"] == expected_uah_minor
