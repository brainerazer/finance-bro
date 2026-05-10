import subprocess

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_amount_minor_is_bigint_signed(engine, session_factory):
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name='transactions' AND column_name='amount_minor'"
                )
            )
        ).first()
    assert row is not None and row[0] == "bigint"
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'm', 'UAH', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(
                text("SELECT id FROM accounts WHERE source_account_id='m'")
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, raw_payload) "
                "VALUES (:a, 'neg', -9999999999, 'UAH', now(), '{}'::jsonb)"
            ),
            {"a": acc_id},
        )
        value = (
            await s.execute(
                text("SELECT amount_minor FROM transactions WHERE source_tx_id='neg'")
            )
        ).scalar_one()
        assert value == -9999999999 and isinstance(value, int)


@pytest.mark.asyncio
async def test_currency_is_char3(engine):
    async with engine.connect() as conn:
        for table in ("accounts", "transactions"):
            row = (
                await conn.execute(
                    text(
                        "SELECT data_type, character_maximum_length "
                        "FROM information_schema.columns "
                        "WHERE table_name=:t AND column_name='currency'"
                    ),
                    {"t": table},
                )
            ).first()
            assert row is not None
            assert row[0] in ("character", "char")
            assert row[1] == 3


def test_no_float_in_pipeline():
    # Ban float() in DB layer (importers/ comes in Plan 02 with same rule).
    result = subprocess.run(
        ["grep", "-rEn", r"\bfloat\(", "src/finance_bro/db", "src/finance_bro/core"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, f"float() usage found:\n{result.stdout}"
