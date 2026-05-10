import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.mark.asyncio
async def test_index_ddl_has_where_clause(engine):
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_transactions_account_source_tx'"
                )
            )
        ).first()
    assert row is not None, "Partial unique index missing"
    assert "WHERE" in row[0].upper() and "is_deleted" in row[0].lower()


@pytest.mark.asyncio
async def test_active_duplicate_rejected(session_factory):
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'a1', 'UAH', '{}'::jsonb) RETURNING id"
            )
        )
        acc_id = (
            await s.execute(text("SELECT id FROM accounts WHERE source_account_id='a1'"))
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, raw_payload) "
                "VALUES (:a, 'tx1', -100, 'UAH', now(), '{}'::jsonb)"
            ),
            {"a": acc_id},
        )
        await s.commit()
    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            await s.execute(
                text(
                    "INSERT INTO transactions "
                    "(account_id, source_tx_id, amount_minor, currency, time, raw_payload) "
                    "VALUES (:a, 'tx1', -100, 'UAH', now(), '{}'::jsonb)"
                ),
                {"a": acc_id},
            )
            await s.commit()


@pytest.mark.asyncio
async def test_soft_deleted_can_reinsert(session_factory):
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'a2', 'UAH', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(text("SELECT id FROM accounts WHERE source_account_id='a2'"))
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, raw_payload) "
                "VALUES (:a, 'tx-soft', -100, 'UAH', now(), '{}'::jsonb)"
            ),
            {"a": acc_id},
        )
        await s.execute(
            text(
                "UPDATE transactions SET is_deleted=true "
                "WHERE account_id=:a AND source_tx_id='tx-soft'"
            ),
            {"a": acc_id},
        )
        # Re-insert SAME (account_id, source_tx_id) — must succeed.
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, raw_payload) "
                "VALUES (:a, 'tx-soft', -100, 'UAH', now(), '{}'::jsonb)"
            ),
            {"a": acc_id},
        )
        await s.commit()
