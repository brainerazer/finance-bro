"""CAT-05 — run-rules-over-history preview→commit with the staleness-token handshake.

Integration test (real Postgres, migration-seeded rules). Proves the
`RulesHistoryService` sweeps the PURE Plan 01 engine over ALL non-locked rows
(D-14), returns the full diff + a sha256 staleness token (D-12/D-13), and that
`commit` recomputes-and-compares the token — applying the diff on match and
raising `StaleRunError` on mismatch (the route maps it to HTTP 409), never
touching a locked row (CAT-04/D-09).

The service-level cases (preview shape, commit-on-match, stale→raise, lock
invariant) live here; the route-level cases (200 + body shape, 200 commit,
409 on stale) are added in Task 2 via the `client` fixture.

`rules`/`categories` are NOT truncated between tests (only transactions/accounts
are), so the seeded taxonomy stays intact; an autouse fixture wipes
accounts+transactions so this file never leaks into sibling session_factory
tests (mirrors test_categorize_on_import.py).
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from finance_bro.db.engine import get_session_factory
from finance_bro.services.rules_history import RulesHistoryService, StaleRunError


@pytest_asyncio.fixture(autouse=True)
async def _isolate_accounts(session_factory):
    """Wipe accounts+transactions before AND after each test so this file's rows
    never leak into sibling session_factory tests. Seeded categories/rules
    survive (not truncated)."""
    truncate = text("TRUNCATE TABLE transactions, accounts RESTART IDENTITY CASCADE")
    async with session_factory() as s, s.begin():
        await s.execute(truncate)
    yield
    async with session_factory() as s, s.begin():
        await s.execute(truncate)


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


async def _category_id(session_factory, name: str) -> int:
    async with session_factory() as s:
        return (
            await s.execute(
                text("SELECT id FROM categories WHERE name = :n"), {"n": name}
            )
        ).scalar_one()


async def _insert_tx(
    session_factory,
    acc_id: int,
    *,
    source_tx_id: str,
    amount_minor: int,
    mcc: int,
    description: str,
    category_id: int | None = None,
    category_source: str | None = None,
    is_user_locked: bool = False,
) -> None:
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO transactions "
                "  (account_id, source_tx_id, amount_minor, currency, time, raw_payload, "
                "   hold, mcc, description, category_id, category_source, is_user_locked, "
                "   attributed_day) "
                "VALUES "
                "  (:a, :stx, :amt, 'UAH', now(), '{}'::jsonb, false, :mcc, :desc, :cid, "
                "   :csrc, :locked, (now() AT TIME ZONE 'Europe/Kyiv')::date)"
            ),
            {
                "a": acc_id,
                "stx": source_tx_id,
                "amt": amount_minor,
                "mcc": mcc,
                "desc": description,
                "cid": category_id,
                "csrc": category_source,
                "locked": is_user_locked,
            },
        )


async def _tx_state(session_factory, acc_id: int) -> dict[str, tuple]:
    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT source_tx_id, category_id, category_source, is_user_locked "
                    "FROM transactions WHERE account_id = :a"
                ),
                {"a": acc_id},
            )
        ).all()
    return {
        r.source_tx_id: (r.category_id, r.category_source, r.is_user_locked)
        for r in rows
    }


async def _id_map(session_factory, acc_id: int) -> dict[str, int]:
    async with session_factory() as s:
        return {
            r.source_tx_id: r.id
            for r in (
                await s.execute(
                    text("SELECT id, source_tx_id FROM transactions WHERE account_id = :a"),
                    {"a": acc_id},
                )
            ).all()
        }


async def _seed_mixed_account(session_factory) -> tuple[int, int, int]:
    """A 4-row account: (a) NULL-category ATB grocery debit, (b) a rule-categorized
    UNLOCKED row sitting in the WRONG category (Transport) that a grocery rule would
    move to Groceries, (c) a no-match row, (d) a LOCKED manual row. Returns
    (account_id, groceries_id, transport_id)."""
    acc_id = await _make_account(session_factory)
    groceries = await _category_id(session_factory, "Groceries")
    transport = await _category_id(session_factory, "Transport")
    # (a) untouched grocery debit, no category yet -> should become Groceries.
    await _insert_tx(
        session_factory, acc_id,
        source_tx_id="a-null-grocery", amount_minor=-1500, mcc=5411, description="ATB",
    )
    # (b) rule-categorized but in the WRONG bucket (Transport) -> grocery rule moves
    #     it to Groceries (an OVERWRITE: old non-None -> new differs).
    await _insert_tx(
        session_factory, acc_id,
        source_tx_id="b-wrong-rule", amount_minor=-1700, mcc=5411, description="Silpo",
        category_id=transport, category_source="rule",
    )
    # (c) no-match row -> stays/becomes NULL.
    await _insert_tx(
        session_factory, acc_id,
        source_tx_id="c-no-match", amount_minor=-2000, mcc=9999, description="Bolt",
    )
    # (d) LOCKED manual grocery debit -> never touched by the sweep.
    await _insert_tx(
        session_factory, acc_id,
        source_tx_id="d-locked", amount_minor=-3000, mcc=5411, description="Manual",
        category_id=transport, category_source="manual", is_user_locked=True,
    )
    return acc_id, groceries, transport


@pytest.mark.asyncio
async def test_preview_shape_and_counts(session_factory):
    acc_id, groceries, transport = await _seed_mixed_account(session_factory)
    svc = RulesHistoryService(get_session_factory())

    preview = await svc.preview(acc_id)

    by_tx = {c.transaction_id: c for c in preview.changes}
    changed_ids = set(by_tx.keys())
    idmap = await _id_map(session_factory, acc_id)

    assert idmap["a-null-grocery"] in changed_ids
    assert idmap["b-wrong-rule"] in changed_ids
    assert idmap["c-no-match"] not in changed_ids  # NULL -> NULL, no change
    assert idmap["d-locked"] not in changed_ids  # locked, never in changes

    assert preview.changed_count == 2
    # overwritten = rows whose OLD category was non-None and differs = only (b).
    assert preview.overwritten_count == 1
    # one locked row excluded.
    assert preview.skipped_locked_count == 1
    assert preview.token  # non-empty

    a_change = by_tx[idmap["a-null-grocery"]]
    assert a_change.old_category_id is None
    assert a_change.new_category_id == groceries
    b_change = by_tx[idmap["b-wrong-rule"]]
    assert b_change.old_category_id == transport
    assert b_change.new_category_id == groceries


@pytest.mark.asyncio
async def test_commit_on_matching_token_applies(session_factory):
    acc_id, groceries, transport = await _seed_mixed_account(session_factory)
    svc = RulesHistoryService(get_session_factory())

    preview = await svc.preview(acc_id)
    result = await svc.commit(acc_id, preview.token)
    assert result["applied"] == preview.changed_count

    state = await _tx_state(session_factory, acc_id)
    # (a) grocery debit -> Groceries, stamped 'rule'.
    assert state["a-null-grocery"][0] == groceries
    assert state["a-null-grocery"][1] == "rule"
    # (b) overwritten Transport -> Groceries, stamped 'rule'.
    assert state["b-wrong-rule"][0] == groceries
    assert state["b-wrong-rule"][1] == "rule"
    # (c) no-match stays NULL (not in diff -> untouched).
    assert state["c-no-match"][0] is None
    # (d) locked row UNCHANGED — Transport / manual / locked.
    assert state["d-locked"] == (transport, "manual", True)


@pytest.mark.asyncio
async def test_commit_with_stale_token_raises_and_changes_nothing(session_factory):
    acc_id, groceries, transport = await _seed_mixed_account(session_factory)
    svc = RulesHistoryService(get_session_factory())

    preview = await svc.preview(acc_id)
    before = await _tx_state(session_factory, acc_id)

    # Mutate state so the recomputed token differs: lock + manually recategorize
    # the previously-unlocked (b) row.
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "UPDATE transactions SET is_user_locked = true, "
                "category_source = 'manual' WHERE account_id = :a "
                "AND source_tx_id = 'b-wrong-rule'"
            ),
            {"a": acc_id},
        )

    with pytest.raises(StaleRunError):
        await svc.commit(acc_id, preview.token)

    # The failed commit changed nothing beyond the manual mutation above.
    after = await _tx_state(session_factory, acc_id)
    assert after["a-null-grocery"] == before["a-null-grocery"]  # still untouched
    assert after["c-no-match"] == before["c-no-match"]
    assert after["d-locked"] == before["d-locked"]


@pytest.mark.asyncio
async def test_locked_row_never_in_changes_and_unchanged_after_commit(session_factory):
    acc_id, groceries, transport = await _seed_mixed_account(session_factory)
    svc = RulesHistoryService(get_session_factory())

    preview = await svc.preview(acc_id)
    idmap = await _id_map(session_factory, acc_id)
    locked_id = idmap["d-locked"]
    assert locked_id not in {c.transaction_id for c in preview.changes}

    await svc.commit(acc_id, preview.token)
    state = await _tx_state(session_factory, acc_id)
    assert state["d-locked"] == (transport, "manual", True)
