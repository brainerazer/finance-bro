"""CAT-01 / CAT-02 / Pitfall 6 (Plan 04-02) — RuleRepo CRUD, ordering, reorder.

Live regression against a real Postgres container. The `rules` table is NOT
truncated between tests (the client fixture only wipes transactions/accounts),
so every test uses a private high-priority range (>= 9000) and cleans up after
itself. Asserts:
  * predicate round-trips through JSONB and re-validates as RulePredicate;
  * list_active_ordered() sorts (priority ASC, id ASC) deterministically;
  * reorder() rewrites priorities for an ordered id list with no UNIQUE collision.
"""

import uuid

import pytest
from sqlalchemy import text

from finance_bro.categorizer.predicate import RulePredicate
from finance_bro.db.category_repo import CategoryRepo
from finance_bro.db.rule_repo import RuleRepo

# Private priority band for this file so we never collide with the 11 seeded
# MCC rules (low priorities) or sibling test files.
_BAND_LO = 9200
_BAND_HI = 9300


async def _cleanup(session_factory) -> None:
    async with session_factory() as s, s.begin():
        await s.execute(
            text("DELETE FROM rules WHERE priority >= :lo AND priority < :hi"),
            {"lo": _BAND_LO, "hi": _BAND_HI},
        )


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_predicate_round_trips_through_jsonb(session_factory):
    await _cleanup(session_factory)
    try:
        async with session_factory() as s, s.begin():
            cat = await CategoryRepo(s).create(name=_uniq("Groc"), color=None)
        predicate = RulePredicate.model_validate(
            {
                "all": [
                    {"op": "in_int", "field": "mcc", "values": [5411, 5499]},
                    {"op": "amount_sign", "sign": "debit"},
                ]
            }
        )
        async with session_factory() as s, s.begin():
            rule = await RuleRepo(s).create(
                priority=_BAND_LO + 1,
                category_id=cat.id,
                predicate_json=predicate.model_dump(mode="json"),
                description="round-trip",
            )
        rid = rule.id

        async with session_factory() as s:
            got = await RuleRepo(s).get(rid)
            assert got is not None
            # the repo returns the JSON dict; re-validate it back into the AST
            reparsed = RulePredicate.model_validate(got.predicate)
        assert reparsed == predicate
    finally:
        await _cleanup(session_factory)


@pytest.mark.asyncio
async def test_list_active_ordered_priority_then_id(session_factory):
    await _cleanup(session_factory)
    try:
        async with session_factory() as s, s.begin():
            cat = await CategoryRepo(s).create(name=_uniq("Ord"), color=None)
        pj = {"all": [{"op": "in_int", "field": "mcc", "values": [1]}]}
        # Insert out of order; list must come back priority ASC.
        async with session_factory() as s, s.begin():
            repo = RuleRepo(s)
            await repo.create(priority=_BAND_LO + 30, category_id=cat.id, predicate_json=pj, description="c")
            await repo.create(priority=_BAND_LO + 10, category_id=cat.id, predicate_json=pj, description="a")
            await repo.create(priority=_BAND_LO + 20, category_id=cat.id, predicate_json=pj, description="b")

        async with session_factory() as s:
            ordered = await RuleRepo(s).list_active_ordered()
        band = [r for r in ordered if _BAND_LO <= r.priority < _BAND_HI]
        priorities = [r.priority for r in band]
        assert priorities == sorted(priorities)
        # globally the whole list is sorted by (priority, id)
        keys = [(r.priority, r.id) for r in ordered]
        assert keys == sorted(keys)
    finally:
        await _cleanup(session_factory)


@pytest.mark.asyncio
async def test_reorder_rewrites_priorities_without_unique_collision(session_factory):
    await _cleanup(session_factory)
    try:
        async with session_factory() as s, s.begin():
            cat = await CategoryRepo(s).create(name=_uniq("Reord"), color=None)
        pj = {"all": [{"op": "in_int", "field": "mcc", "values": [1]}]}
        async with session_factory() as s, s.begin():
            repo = RuleRepo(s)
            r1 = await repo.create(priority=_BAND_LO + 11, category_id=cat.id, predicate_json=pj, description="r1")
            r2 = await repo.create(priority=_BAND_LO + 12, category_id=cat.id, predicate_json=pj, description="r2")
            r3 = await repo.create(priority=_BAND_LO + 13, category_id=cat.id, predicate_json=pj, description="r3")
        ids = [r1.id, r2.id, r3.id]

        # Reverse the order — this is the case that naively triggers a UNIQUE
        # collision if priorities are rewritten in place one-by-one.
        reordered = [r3.id, r1.id, r2.id]
        async with session_factory() as s, s.begin():
            await RuleRepo(s).reorder(reordered)

        async with session_factory() as s:
            ordered = await RuleRepo(s).list_active_ordered()
        band_ids = [r.id for r in ordered if r.id in set(ids)]
        # After reorder, the list order must match the requested id sequence.
        assert band_ids == reordered
        # priorities are strictly ascending across the band → no UNIQUE violation
        band_priorities = [r.priority for r in ordered if r.id in set(ids)]
        assert band_priorities == sorted(band_priorities)
        assert len(set(band_priorities)) == 3
    finally:
        await _cleanup(session_factory)


@pytest.mark.asyncio
async def test_update_and_delete(session_factory):
    await _cleanup(session_factory)
    try:
        async with session_factory() as s, s.begin():
            cat = await CategoryRepo(s).create(name=_uniq("UpdDel"), color=None)
        pj = {"all": [{"op": "in_int", "field": "mcc", "values": [1]}]}
        async with session_factory() as s, s.begin():
            rule = await RuleRepo(s).create(
                priority=_BAND_LO + 50, category_id=cat.id, predicate_json=pj, description="orig"
            )
        rid = rule.id
        async with session_factory() as s, s.begin():
            await RuleRepo(s).update(rid, description="changed")
        async with session_factory() as s:
            got = await RuleRepo(s).get(rid)
            assert got is not None
            assert got.description == "changed"
        async with session_factory() as s, s.begin():
            await RuleRepo(s).delete(rid)
        async with session_factory() as s:
            assert await RuleRepo(s).get(rid) is None
    finally:
        await _cleanup(session_factory)
