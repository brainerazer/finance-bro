"""CAT-01 / D-02 / D-09 / D-11 (Plan 04-03) — repo+engine seam at import time.

Integration test (real Postgres, migration-seeded rules). Proves the repo's two
new write-path methods cooperate with the PURE engine reused verbatim (D-11):

  * `fetch_for_categorize(account_id, touched_source_tx_ids)` returns RowView
    objects for ONLY the non-locked touched rows — a locked row is filtered in
    SQL (`NOT is_user_locked`) and never appears.
  * running `engine.categorize_rows(rows, compile_rules(list_active_ordered()))`
    over those rows yields (id, category_id) pairs: the ATB-grocery debit → the
    Groceries category id; the no-match row → None.
  * `apply_categories(updates)` writes `category_id` + `category_source='rule'`
    for exactly those rows; a `(id, None)` update writes NULL (D-02, evaluated
    but matched nothing — never silently bucketed); the locked row is untouched.

The `rules`/`categories` tables are NOT truncated between tests (only
transactions/accounts are), so a private high source_account_id / a per-test
account keeps this file isolated; the seeded taxonomy stays intact.
"""

import uuid

import pytest
from sqlalchemy import text

from finance_bro.categorizer import categorize_rows
from finance_bro.categorizer.engine import compile_rules
from finance_bro.db.rule_repo import RuleRepo
from finance_bro.db.transaction_repo import TransactionRepo


async def _make_account(session_factory) -> int:
    src = f"acct-{uuid.uuid4().hex[:8]}"
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', :src, 'UAH', '{}'::jsonb)"
            ),
            {"src": src},
        )
        acc_id = (
            await s.execute(
                text("SELECT id FROM accounts WHERE source_account_id = :src"),
                {"src": src},
            )
        ).scalar_one()
    return acc_id


async def _groceries_category_id(session_factory) -> int:
    async with session_factory() as s:
        return (
            await s.execute(text("SELECT id FROM categories WHERE name = 'Groceries'"))
        ).scalar_one()


@pytest.mark.asyncio
async def test_fetch_for_categorize_excludes_locked_rows(session_factory):
    acc_id = await _make_account(session_factory)
    # Three touched rows: one grocery debit, one no-match, one locked grocery debit.
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO transactions "
                "  (account_id, source_tx_id, amount_minor, currency, time, raw_payload, "
                "   hold, mcc, description, is_user_locked, attributed_day) "
                "VALUES "
                "  (:a, 'g-debit',  -1500, 'UAH', now(), '{}'::jsonb, false, 5411, 'ATB', false, "
                "   (now() AT TIME ZONE 'Europe/Kyiv')::date), "
                "  (:a, 'no-match', -2000, 'UAH', now(), '{}'::jsonb, false, 4111, 'Bolt', false, "
                "   (now() AT TIME ZONE 'Europe/Kyiv')::date), "
                "  (:a, 'locked',   -3000, 'UAH', now(), '{}'::jsonb, false, 5411, 'Silpo', true, "
                "   (now() AT TIME ZONE 'Europe/Kyiv')::date)"
            ),
            {"a": acc_id},
        )

    touched = ["g-debit", "no-match", "locked"]
    async with session_factory() as s:
        rows = await TransactionRepo(s).fetch_for_categorize(acc_id, touched)

    # The locked row is filtered in SQL — it is absent entirely (D-09).
    descriptions = {r.description for r in rows}
    assert "Silpo" not in descriptions
    assert {"ATB", "Bolt"} == descriptions
    assert all(not r.is_user_locked for r in rows)


@pytest.mark.asyncio
async def test_engine_round_trip_applies_categories_and_nulls(session_factory):
    acc_id = await _make_account(session_factory)
    groceries = await _groceries_category_id(session_factory)
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO transactions "
                "  (account_id, source_tx_id, amount_minor, currency, time, raw_payload, "
                "   hold, mcc, description, is_user_locked, attributed_day) "
                "VALUES "
                "  (:a, 'g-debit',  -1500, 'UAH', now(), '{}'::jsonb, false, 5411, 'ATB', false, "
                "   (now() AT TIME ZONE 'Europe/Kyiv')::date), "
                "  (:a, 'no-match', -2000, 'UAH', now(), '{}'::jsonb, false, 4111, 'Bolt', false, "
                "   (now() AT TIME ZONE 'Europe/Kyiv')::date), "
                "  (:a, 'locked',   -3000, 'UAH', now(), '{}'::jsonb, false, 5411, 'Silpo', true, "
                "   (now() AT TIME ZONE 'Europe/Kyiv')::date)"
            ),
            {"a": acc_id},
        )

    touched = ["g-debit", "no-match", "locked"]
    async with session_factory() as s:
        repo = TransactionRepo(s)
        rules = compile_rules(await RuleRepo(s).list_active_ordered())
        rows = await repo.fetch_for_categorize(acc_id, touched)
        updates = categorize_rows(rows, rules)
        async with s.begin():
            await repo.apply_categories(updates)

    async with session_factory() as s:
        result = (
            await s.execute(
                text(
                    "SELECT source_tx_id, category_id, category_source "
                    "FROM transactions WHERE account_id = :a ORDER BY source_tx_id"
                ),
                {"a": acc_id},
            )
        ).all()
    by_id = {r.source_tx_id: r for r in result}

    # ATB grocery debit -> Groceries category, stamped 'rule'.
    assert by_id["g-debit"].category_id == groceries
    assert by_id["g-debit"].category_source == "rule"
    # no-match row -> NULL category, but STILL stamped 'rule' (D-02 evaluated).
    assert by_id["no-match"].category_id is None
    assert by_id["no-match"].category_source == "rule"
    # locked row was never fetched, never written -> untouched (NULL / NULL).
    assert by_id["locked"].category_id is None
    assert by_id["locked"].category_source is None
