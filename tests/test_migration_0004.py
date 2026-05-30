"""Migration 0004 (Plan 04-01) — integration coverage.

Asserts that migration 0004 (categories + rules tables, the
`transactions.category_id` FK, and the seeded taxonomy + MCC rules) applies
cleanly against a real Postgres container and that the FK + seed counts are
present. Also verifies a clean downgrade-then-upgrade round-trip (the whole
0001->0004 chain is dropped to base and re-applied) so the down path is real.
"""

import asyncio

import pytest
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import run_alembic

# Keep these in sync with alembic/versions/0004_categorized_spending.py.
_EXPECTED_CATEGORY_COUNT = 15
_EXPECTED_RULE_COUNT = 11


@pytest.mark.asyncio
async def test_0004_seeds_and_fk_present(pg_url, engine):
    # The session-scoped pg_url fixture already ran `alembic upgrade head`, so
    # 0004 is applied. Assert the seed counts and the FK constraint.
    async with engine.connect() as conn:
        category_count = (await conn.execute(text("SELECT count(*) FROM categories"))).scalar_one()
        rule_count = (await conn.execute(text("SELECT count(*) FROM rules"))).scalar_one()
        fk_row = (
            await conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conname = 'fk_transactions_category'"
                )
            )
        ).first()

    assert category_count == _EXPECTED_CATEGORY_COUNT
    assert rule_count == _EXPECTED_RULE_COUNT
    assert fk_row is not None, "fk_transactions_category missing on transactions.category_id"
    # confdeltype 'r' == ON DELETE RESTRICT (D-03/D-15).
    assert fk_row[0] == "r", f"expected RESTRICT delete rule, got {fk_row[0]!r}"


@pytest.mark.asyncio
async def test_0004_seed_rules_reference_categories_and_have_unique_priority(pg_url, engine):
    async with engine.connect() as conn:
        # Every seeded rule resolves to a real category (subselect seed worked).
        orphan_rules = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM rules r "
                    "LEFT JOIN categories c ON c.id = r.category_id "
                    "WHERE c.id IS NULL"
                )
            )
        ).scalar_one()
        # priority is UNIQUE — distinct count equals row count (Pitfall 6).
        distinct_priorities = (
            await conn.execute(text("SELECT count(DISTINCT priority) FROM rules"))
        ).scalar_one()
        total_rules = (await conn.execute(text("SELECT count(*) FROM rules"))).scalar_one()
        # Seeded predicates are the closed-op AST shape (flat AND-only — D-06).
        sample_predicate = (
            await conn.execute(text("SELECT predicate FROM rules ORDER BY priority ASC LIMIT 1"))
        ).scalar_one()

    assert orphan_rules == 0
    assert distinct_priorities == total_rules
    assert "all" in sample_predicate
    ops = {cond["op"] for cond in sample_predicate["all"]}
    assert "in_int" in ops and "amount_sign" in ops


@pytest.mark.asyncio
async def test_0004_round_trip_to_base_and_back(pg_url, engine):
    """Drop the whole chain to base and re-apply head — proves 0004's downgrade
    is real and the upgrade is idempotent across a clean rebuild."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    cfg.set_main_option("script_location", "alembic")
    await asyncio.to_thread(run_alembic, cfg, "base", downgrade=True)
    await asyncio.to_thread(run_alembic, cfg, "head")

    async with engine.connect() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname='public' ORDER BY tablename"
                    )
                )
            )
            .scalars()
            .all()
        )
        fk_row = (
            await conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = 'fk_transactions_category'")
            )
        ).first()
        category_count = (await conn.execute(text("SELECT count(*) FROM categories"))).scalar_one()

    assert "categories" in tables
    assert "rules" in tables
    assert fk_row is not None, "FK absent after round-trip"
    assert category_count == _EXPECTED_CATEGORY_COUNT
