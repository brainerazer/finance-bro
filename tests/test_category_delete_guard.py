"""D-15 (Plan 04-02) — category delete guard + router smoke over the HTTP layer.

Exercises the `client` fixture end-to-end (both routers mounted):
  * DELETE /api/categories/{cid} on a referenced category → 409 with counts;
  * DELETE on an unreferenced category → 204;
  * POST /api/rules with a malformed predicate → 422 (Pydantic boundary, V5);
  * POST /api/rules with the canonical ATB predicate → 201;
  * basic CRUD smoke for both routers (create + list).

The `categories`/`rules` tables are NOT truncated by the client fixture, so each
test uses unique category names + a private priority band (>= 9000) and cleans up
created rules so it never collides with the 11 seeded MCC rules or siblings.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

# Private priority band so we never collide with the 11 seeded MCC rules.
_PRIO = 9400


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(autouse=True)
async def _restore_taxonomy(session_factory):
    """The `categories`/`rules` tables are seeded by migration 0004 and are NOT
    truncated between tests. Snapshot the seeded baseline (max ids), then delete
    any categories/rules this test created via the routers so absolute-count
    assertions in test_migration_0004 stay valid. Rules first (FK)."""
    async with session_factory() as s:
        max_cat = (await s.execute(text("SELECT max(id) FROM categories"))).scalar_one()
        max_rule = (await s.execute(text("SELECT max(id) FROM rules"))).scalar_one()
    yield
    async with session_factory() as s, s.begin():
        await s.execute(text("DELETE FROM rules WHERE id > :m"), {"m": max_rule or 0})
        await s.execute(text("DELETE FROM categories WHERE id > :m"), {"m": max_cat or 0})


async def _delete_rules_in_band(session_factory) -> None:
    async with session_factory() as s, s.begin():
        await s.execute(
            text("DELETE FROM rules WHERE priority >= :lo AND priority < :hi"),
            {"lo": _PRIO, "hi": _PRIO + 100},
        )


# The canonical ATB predicate (from the plan) — must be accepted at the boundary.
_ATB_PREDICATE = {
    "all": [
        {"op": "in_int", "field": "mcc", "values": [5411, 5499]},
        {"op": "amount_sign", "sign": "debit"},
        {"op": "icontains", "field": "description", "value": "ATB"},
    ]
}


@pytest.mark.asyncio
async def test_category_and_rule_crud_smoke(client, session_factory):
    """Create a category + a rule via the routers, then list both."""
    try:
        # create category
        r = await client.post("/api/categories", json={"name": _uniq("Smoke"), "color": "#123456"})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]

        # create rule referencing it (canonical ATB predicate)
        r = await client.post(
            "/api/rules",
            json={
                "priority": _PRIO + 1,
                "category_id": cid,
                "predicate": _ATB_PREDICATE,
                "description": "ATB groceries",
            },
        )
        assert r.status_code == 201, r.text
        rule = r.json()
        assert rule["category_id"] == cid
        assert rule["predicate"]["all"][0]["op"] == "in_int"

        # list both — priority-ordered rules, id-ordered categories
        cats = await client.get("/api/categories")
        assert cats.status_code == 200
        assert any(c["id"] == cid for c in cats.json())

        rules = await client.get("/api/rules")
        assert rules.status_code == 200
        assert any(rr["id"] == rule["id"] for rr in rules.json())
    finally:
        await _delete_rules_in_band(session_factory)


@pytest.mark.asyncio
async def test_delete_referenced_category_returns_409_with_counts(client, session_factory):
    """D-15: deleting a category referenced by a rule → 409 (NOT 200/204) with a
    detail carrying integer `rules` + `transactions` counts."""
    try:
        r = await client.post("/api/categories", json={"name": _uniq("Refd")})
        cid = r.json()["id"]

        r = await client.post(
            "/api/rules",
            json={
                "priority": _PRIO + 2,
                "category_id": cid,
                "predicate": {"all": [{"op": "in_int", "field": "mcc", "values": [1]}]},
                "description": "ref",
            },
        )
        assert r.status_code == 201, r.text

        r = await client.delete(f"/api/categories/{cid}")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["rules"] == 1
        assert detail["transactions"] == 0
        assert isinstance(detail["rules"], int)
        assert isinstance(detail["transactions"], int)
    finally:
        await _delete_rules_in_band(session_factory)


@pytest.mark.asyncio
async def test_delete_unreferenced_category_succeeds(client):
    r = await client.post("/api/categories", json={"name": _uniq("Free")})
    cid = r.json()["id"]
    r = await client.delete(f"/api/categories/{cid}")
    assert r.status_code == 204, r.text
    # gone now
    cats = await client.get("/api/categories")
    assert not any(c["id"] == cid for c in cats.json())


@pytest.mark.asyncio
async def test_malformed_predicate_rejected_at_boundary(client, session_factory):
    """V5 / T-4-validate: an unknown `op` is rejected by Pydantic → 422 before the
    interpreter ever runs."""
    try:
        r = await client.post("/api/categories", json={"name": _uniq("Mal")})
        cid = r.json()["id"]

        r = await client.post(
            "/api/rules",
            json={
                "priority": _PRIO + 3,
                "category_id": cid,
                # unknown op — discriminated union has no member for it
                "predicate": {
                    "all": [{"op": "regex_match", "field": "description", "value": ".*"}]
                },
                "description": "bad",
            },
        )
        assert r.status_code == 422, r.text
    finally:
        await _delete_rules_in_band(session_factory)


@pytest.mark.asyncio
async def test_canonical_atb_predicate_accepted(client, session_factory):
    try:
        r = await client.post("/api/categories", json={"name": _uniq("Atb")})
        cid = r.json()["id"]
        r = await client.post(
            "/api/rules",
            json={
                "priority": _PRIO + 4,
                "category_id": cid,
                "predicate": _ATB_PREDICATE,
                "description": "ATB",
            },
        )
        assert r.status_code == 201, r.text
    finally:
        await _delete_rules_in_band(session_factory)
