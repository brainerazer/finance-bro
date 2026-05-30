"""CAT-03 / D-15 (Plan 04-02) — CategoryRepo CRUD + reference_counts.

Live regression against a real Postgres container. The migration 0004 seeds a
~15-category taxonomy + 11 MCC rules; these tests create their own categories
(with unique names) and assert the round-trip plus the reference_counts
pre-check that backs the D-15 delete guard. No f-string SQL anywhere — the two
reference counts use parameterized text() (T-4-sqli).
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from finance_bro.db.category_repo import CategoryRepo


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(autouse=True)
async def _restore_taxonomy(session_factory):
    """The `categories`/`rules` tables are seeded by migration 0004 and are NOT
    truncated between tests. Snapshot the seeded baseline (max ids) before the
    test, then delete any rows this test created afterward so absolute-count
    assertions elsewhere (test_migration_0004) stay valid. Rules first (FK)."""
    async with session_factory() as s:
        max_cat = (await s.execute(text("SELECT max(id) FROM categories"))).scalar_one()
        max_rule = (await s.execute(text("SELECT max(id) FROM rules"))).scalar_one()
    yield
    async with session_factory() as s, s.begin():
        await s.execute(text("DELETE FROM rules WHERE id > :m"), {"m": max_rule or 0})
        await s.execute(text("DELETE FROM categories WHERE id > :m"), {"m": max_cat or 0})


@pytest.mark.asyncio
async def test_create_get_list_update_round_trip(session_factory):
    name = _uniq("Coffee")
    async with session_factory() as s, s.begin():
        repo = CategoryRepo(s)
        created = await repo.create(name=name, color="#abc123")
    cid = created.id
    assert cid is not None
    assert created.name == name
    assert created.color == "#abc123"

    # get + list (fresh session — proves persistence, not identity-map cache)
    async with session_factory() as s:
        repo = CategoryRepo(s)
        got = await repo.get(cid)
        assert got is not None
        assert got.name == name
        listed = await repo.list_all()
    # list is id-ordered and includes the seeded taxonomy + our new row
    ids = [c.id for c in listed]
    assert ids == sorted(ids)
    assert cid in ids

    # update name + color
    async with session_factory() as s, s.begin():
        repo = CategoryRepo(s)
        await repo.update(cid, name=_uniq("Espresso"), color="#000000")
    async with session_factory() as s:
        repo = CategoryRepo(s)
        got = await repo.get(cid)
        assert got is not None
        assert got.name.startswith("Espresso-")
        assert got.color == "#000000"


@pytest.mark.asyncio
async def test_reference_counts_zero_then_nonzero(session_factory):
    """reference_counts(cid) returns (rules_referencing, transactions_referencing):
    (0,0) for a fresh category, then nonzero once a rule references it."""
    from finance_bro.db.rule_repo import RuleRepo

    async with session_factory() as s, s.begin():
        cat = await CategoryRepo(s).create(name=_uniq("Refs"), color=None)
    cid = cat.id

    async with session_factory() as s:
        repo = CategoryRepo(s)
        rules_n, tx_n = await repo.reference_counts(cid)
    assert (rules_n, tx_n) == (0, 0)

    # Reference the category from a rule → rules count becomes 1.
    predicate_json = {"all": [{"op": "in_int", "field": "mcc", "values": [9999]}]}
    async with session_factory() as s, s.begin():
        await RuleRepo(s).create(
            priority=9100,
            category_id=cid,
            predicate_json=predicate_json,
            description="ref test",
        )
    try:
        async with session_factory() as s:
            rules_n, tx_n = await CategoryRepo(s).reference_counts(cid)
        assert rules_n == 1
        assert tx_n == 0
    finally:
        # Clean up the rule (rules table is NOT truncated between tests).
        async with session_factory() as s, s.begin():
            from sqlalchemy import text

            await s.execute(text("DELETE FROM rules WHERE priority = :p"), {"p": 9100})


@pytest.mark.asyncio
async def test_delete_removes_unreferenced_category(session_factory):
    async with session_factory() as s, s.begin():
        cat = await CategoryRepo(s).create(name=_uniq("Temp"), color=None)
    cid = cat.id
    async with session_factory() as s, s.begin():
        await CategoryRepo(s).delete(cid)
    async with session_factory() as s:
        assert await CategoryRepo(s).get(cid) is None
