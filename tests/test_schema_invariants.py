import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_is_deleted_default_false(session_factory):
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'sd', 'UAH', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(text("SELECT id FROM accounts WHERE source_account_id='sd'"))
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, raw_payload) "
                "VALUES (:a, 'sd-1', 0, 'UAH', now(), '{}'::jsonb)"
            ),
            {"a": acc_id},
        )
        row = (
            await s.execute(
                text(
                    "SELECT is_deleted, hold, is_user_locked FROM transactions "
                    "WHERE source_tx_id='sd-1'"
                )
            )
        ).first()
        assert row == (False, False, False)


@pytest.mark.asyncio
async def test_raw_payload_jsonb_not_null(engine):
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name='transactions' AND column_name='raw_payload'"
                )
            )
        ).first()
    assert row is not None
    assert row[0] == "jsonb"
    assert row[1] == "NO"


@pytest.mark.asyncio
async def test_forward_looking_columns_present(engine):
    expected = {
        "hold",
        "category_id",
        "category_source",
        "is_user_locked",
        "mcc",
        "description",
        "attributed_day",
    }
    async with engine.connect() as conn:
        cols = set(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='transactions'"
                    )
                )
            )
            .scalars()
            .all()
        )
    missing = expected - cols
    assert not missing, f"Missing forward-looking columns: {missing}"
