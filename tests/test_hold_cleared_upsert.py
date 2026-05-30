"""Hold-aware upsert tests — ING-05 + SC#3 + D-10 frozen-fields invariant.

These tests exercise `TransactionRepo.insert_many` directly (no HTTP, no Mono,
no scheduler) with synthesized `CanonicalTransaction` instances. The third test
goes through the JSON fixtures from 02-01 (statement_with_hold.json +
statement_cleared_followup.json) to anchor the integration test that 02-03
will run via the runner — same code path, same assertion shape.

The CENTRAL test is `test_cleared_updates_in_place`: after a hold→cleared
transition, exactly three columns mutate (`hold`, `amount_minor`, `raw_payload`)
and every other column — including six manual-edit columns from Phases 4-6
(is_user_locked, category_id, category_source, description, mcc, attributed_day)
plus structural columns (currency, time, account_id, source_tx_id, created_at) —
is FROZEN BY OMISSION. Failure of any frozen-field assertion means the SET
clause leaked a column it must not have, which silently breaks Phase 1's
Pitfall-10 promise (no manual-edit overwrites by the importer).

Per-test autouse `_truncate_tx` fixture keeps these tests independent from
each other (they all touch source_tx_id="HOLD-FIXTURE-ID-1") and from
unrelated tests in the same pytest session. Mirror of the per-test isolation
pattern from `tests/test_import_run_repo.py` (02-01 SUMMARY).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import text

from finance_bro.db.transaction_repo import TransactionRepo
from finance_bro.importers.base import CanonicalTransaction
from finance_bro.importers.currency_map import numeric_to_alpha

FIXTURES = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tx(session_factory):
    """Truncate transactions/accounts before every test. CASCADE handles FKs.
    `import_runs`/`scheduler_state` left alone — these tests don't touch them
    and the singleton invariant must survive."""
    async with session_factory() as s:
        await s.execute(text("TRUNCATE TABLE transactions, accounts RESTART IDENTITY CASCADE"))
        await s.commit()
    yield


async def _seed_account(session_factory, source_account_id: str) -> int:
    """Insert a single mono.card account row via raw SQL; return its id.
    Mirrors the seed pattern from test_partial_unique_index.py."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', :sid, 'UAH', '{}'::jsonb)"
            ),
            {"sid": source_account_id},
        )
        acc_id = (
            await s.execute(
                text("SELECT id FROM accounts WHERE source_account_id = :sid"),
                {"sid": source_account_id},
            )
        ).scalar_one()
        await s.commit()
    return cast(int, acc_id)


def _ct_from_mono_item(item: dict[str, Any], source_account_id: str) -> CanonicalTransaction:
    """Convert a Mono /personal/statement item dict to a CanonicalTransaction.
    Mirrors the importer mapping in MonobankImporter.fetch_statement, but inline
    here so the test exercises the upsert path without the HTTP layer.
    """
    return CanonicalTransaction(
        source_tx_id=item["id"],
        source_account_id=source_account_id,
        occurred_at=datetime.fromtimestamp(item["time"], tz=UTC),
        amount_minor=int(item["amount"]),
        currency=numeric_to_alpha(item["currencyCode"]),
        raw=item,
        hold=item["hold"],
        description=item.get("description"),
        mcc=item.get("mcc"),
    )


@pytest.mark.asyncio
async def test_hold_inserted_with_flag(session_factory):
    """ING-05: a hold:true CanonicalTransaction is inserted with hold=true."""
    account_id = await _seed_account(session_factory, "hold-a1")
    held = CanonicalTransaction(
        source_tx_id="HOLD-FIXTURE-ID-1",
        source_account_id="hold-a1",
        occurred_at=datetime.fromtimestamp(1746856800, tz=UTC),
        amount_minor=-12345,
        currency="UAH",
        raw={"id": "HOLD-FIXTURE-ID-1", "hold": True, "amount": -12345},
        hold=True,
        description="Restaurant (pending)",
        mcc=5812,
    )

    async with session_factory() as s, s.begin():
        inserted, updated = await TransactionRepo(s).insert_many(account_id, [held])

    assert (inserted, updated) == (1, 0)

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT hold, amount_minor, description, mcc "
                    "FROM transactions WHERE source_tx_id = 'HOLD-FIXTURE-ID-1'"
                )
            )
        ).one()
    assert row.hold is True
    assert row.amount_minor == -12345
    # First-INSERT path: importer-supplied description/mcc populated normally.
    assert row.description == "Restaurant (pending)"
    assert row.mcc == 5812


@pytest.mark.asyncio
async def test_cleared_updates_in_place(session_factory):
    """CENTRAL D-10 invariant: hold→cleared mutates EXACTLY hold/amount_minor/raw_payload;
    every other column — including six Phase 4-6 manual-edit columns — is frozen.
    Failure of any frozen-field assertion means the SET clause leaked a column."""
    account_id = await _seed_account(session_factory, "hold-a2")

    # 1. Insert the hold:true row (importer-supplied description/mcc).
    held = CanonicalTransaction(
        source_tx_id="HOLD-FIXTURE-ID-1",
        source_account_id="hold-a2",
        occurred_at=datetime.fromtimestamp(1746856800, tz=UTC),
        amount_minor=-12345,
        currency="UAH",
        raw={"id": "HOLD-FIXTURE-ID-1", "hold": True, "amount": -12345, "k": "v1"},
        hold=True,
        description="from importer original",
        mcc=5812,
    )
    async with session_factory() as s, s.begin():
        inserted, updated = await TransactionRepo(s).insert_many(account_id, [held])
    assert (inserted, updated) == (1, 0)

    # 2. Simulate a Phase 4/5/6 manual edit: user assigns a category, locks the row,
    #    overrides description/mcc, and pins the attributed_day. These six columns
    #    plus structural ones must survive a subsequent importer write — D-10.
    #    `category_id` references a REAL seeded category — migration 0004 added the
    #    `fk_transactions_category` FK (D-03), so a fictitious id no longer inserts.
    async with session_factory() as s:
        category_id = (
            await s.execute(text("SELECT id FROM categories WHERE name = 'Groceries'"))
        ).scalar_one()
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "UPDATE transactions SET "
                "  is_user_locked = true, "
                "  category_id = :cid, "
                "  category_source = 'manual', "
                "  description = 'user note', "
                "  mcc = 5411, "
                "  attributed_day = '2026-05-01' "
                "WHERE account_id = :a AND source_tx_id = 'HOLD-FIXTURE-ID-1'"
            ),
            {"a": account_id, "cid": category_id},
        )
    # Capture frozen structural fields before the upsert so we can prove they
    # don't move (created_at and time both pre-upsert).
    async with session_factory() as s:
        pre = (
            await s.execute(
                text(
                    "SELECT id, account_id, source_tx_id, currency, time, created_at "
                    "FROM transactions WHERE account_id = :a "
                    "  AND source_tx_id = 'HOLD-FIXTURE-ID-1'"
                ),
                {"a": account_id},
            )
        ).one()

    # 3. The cleared follow-up. Importer wants to change description/mcc — D-10
    #    forbids it because they're absent from set_={...}. Different raw_payload
    #    too, because that one IS in set_={...} and SHOULD overwrite.
    cleared = CanonicalTransaction(
        source_tx_id="HOLD-FIXTURE-ID-1",
        source_account_id="hold-a2",
        # Importer might pass a different occurred_at, but it must NOT mutate
        # the existing row's `time` column (D-10).
        occurred_at=datetime.fromtimestamp(1746860000, tz=UTC),
        amount_minor=-12500,
        currency="UAH",
        raw={"id": "HOLD-FIXTURE-ID-1", "hold": False, "amount": -12500, "k": "v2"},
        hold=False,
        description="from importer cleared",
        mcc=9999,
    )
    async with session_factory() as s, s.begin():
        inserted, updated = await TransactionRepo(s).insert_many(account_id, [cleared])
    assert (inserted, updated) == (0, 1)

    # 4. Read the full row back.
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT id, account_id, source_tx_id, amount_minor, currency, "
                    "       time, raw_payload, hold, category_id, category_source, "
                    "       is_user_locked, mcc, description, attributed_day, "
                    "       created_at, is_deleted "
                    "FROM transactions WHERE account_id = :a "
                    "  AND source_tx_id = 'HOLD-FIXTURE-ID-1'"
                ),
                {"a": account_id},
            )
        ).one()

    # MUTATED — exactly these three (D-10).
    assert row.hold is False, "hold must flip to False on cleared upsert"
    assert row.amount_minor == -12500, "amount_minor must reflect cleared payload"
    assert row.raw_payload == {
        "id": "HOLD-FIXTURE-ID-1",
        "hold": False,
        "amount": -12500,
        "k": "v2",
    }, "raw_payload must overwrite verbatim"

    # FROZEN — manual-edit columns (Phases 4-6).
    assert row.is_user_locked is True, "is_user_locked must NOT be cleared by importer"
    assert row.category_id == category_id, "category_id must NOT be reset by importer"
    assert row.category_source == "manual", "category_source must NOT be reset by importer"
    assert row.description == "user note", (
        "description must remain user's edit; importer's 'from importer cleared' "
        "must NOT overwrite (D-10 — description absent from set_={...})"
    )
    assert row.mcc == 5411, (
        "mcc must remain user's edit; importer's 9999 must NOT overwrite "
        "(D-10 — mcc absent from set_={...})"
    )
    assert str(row.attributed_day) == "2026-05-01", "attributed_day must NOT be reset by importer"

    # FROZEN — structural columns (identity + audit).
    assert row.id == pre.id, "primary key must not move"
    assert row.account_id == pre.account_id
    assert row.source_tx_id == pre.source_tx_id
    assert row.currency == pre.currency, "currency frozen by omission"
    assert row.time == pre.time, (
        "time must NOT mutate even when importer passes a different occurred_at"
    )
    assert row.created_at == pre.created_at, "created_at frozen — audit invariant"
    assert row.is_deleted is False, "is_deleted frozen by omission"

    # Single-row invariant — no duplicate created.
    async with session_factory() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM transactions WHERE account_id = :a "
                    "  AND source_tx_id = 'HOLD-FIXTURE-ID-1'"
                ),
                {"a": account_id},
            )
        ).scalar_one()
    assert count == 1, "Upsert must produce exactly one row, not a duplicate"


@pytest.mark.asyncio
async def test_e2e_hold_then_cleared(session_factory):
    """SC#3 end-to-end: load the JSON fixtures from 02-01 and round-trip them
    through the upsert path. This is the unit-level anchor for 02-03's runner
    integration test (same fixtures, same code path, +HTTP/scheduler layer)."""
    account_id = await _seed_account(session_factory, "hold-a3")

    held_payload = json.loads((FIXTURES / "statement_with_hold.json").read_text())[0]
    cleared_payload = json.loads((FIXTURES / "statement_cleared_followup.json").read_text())[0]

    held_ct = _ct_from_mono_item(held_payload, source_account_id="hold-a3")
    cleared_ct = _ct_from_mono_item(cleared_payload, source_account_id="hold-a3")

    # Sanity: the fixtures must share the Mono id so the upsert hits ON CONFLICT.
    assert held_ct.source_tx_id == cleared_ct.source_tx_id == "HOLD-FIXTURE-ID-1"
    assert held_ct.hold is True
    assert cleared_ct.hold is False
    assert held_ct.amount_minor != cleared_ct.amount_minor

    async with session_factory() as s, s.begin():
        inserted, updated = await TransactionRepo(s).insert_many(account_id, [held_ct])
    assert (inserted, updated) == (1, 0)

    async with session_factory() as s, s.begin():
        inserted, updated = await TransactionRepo(s).insert_many(account_id, [cleared_ct])
    assert (inserted, updated) == (0, 1)

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT hold, amount_minor, raw_payload "
                    "FROM transactions WHERE account_id = :a "
                    "  AND source_tx_id = 'HOLD-FIXTURE-ID-1'"
                ),
                {"a": account_id},
            )
        ).one()
        count = (
            await s.execute(
                text("SELECT count(*) FROM transactions WHERE account_id = :a"),
                {"a": account_id},
            )
        ).scalar_one()
    assert count == 1, "Round trip must keep exactly one row"
    assert row.hold is False, "hold must be cleared after follow-up"
    assert row.amount_minor == cleared_payload["amount"], (
        "amount_minor must reflect the cleared payload"
    )
    assert row.raw_payload == cleared_payload, "raw_payload must equal the cleared fixture verbatim"
