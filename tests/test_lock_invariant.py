"""CAT-04 headline test — the lock invariant on the IMPORT path (Plan 04-03).

A user-locked, manually-categorized transaction MUST survive a re-import that
re-touches its `(account_id, source_tx_id)` completely untouched: its
`category_id`, `category_source='manual'`, and `is_user_locked=true` are all
unchanged after the import-step categorizer runs. Meanwhile a freshly-touched
NON-locked row that matches the ATB grocery seed rule comes back
`category_source='rule'` with the Groceries category id — proving the import
tick both auto-categorizes AND refuses to clobber a manual lock.

This is the import-step half of the CAT-04 invariant. The history-sweep half is
added by Plan 04 to a sibling area; the two halves coexist by construction
(this file asserts only the import path).

Driven through `ImportService.run_one_card` with a stub importer, so the test
exercises the real Step-4 categorize wiring (insert_many -> fetch_for_categorize
-> categorize_rows -> apply_categories) end to end, not a hand-rolled call.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from finance_bro.importers.base import CanonicalTransaction
from finance_bro.services.import_service import ImportService


@pytest_asyncio.fixture(autouse=True)
async def _isolate_accounts(session_factory):
    """`run_one_card` polls the lowest-id mono.card (D-04); other
    session_factory tests leak accounts/transactions (only the `client` fixture
    truncates). Wipe accounts+transactions before AND after so the card this
    test seeds is the one that gets polled. Seeded categories/rules survive."""
    truncate = text("TRUNCATE TABLE transactions, accounts RESTART IDENTITY CASCADE")
    async with session_factory() as s, s.begin():
        await s.execute(truncate)
    yield
    async with session_factory() as s, s.begin():
        await s.execute(truncate)


class _StubImporter:
    """Minimal ImporterProtocol stand-in: never discovers (the test seeds the
    card directly so discovery is skipped) and yields a fixed statement."""

    source_kind = "mono.card"

    def __init__(self, items: list[CanonicalTransaction]) -> None:
        self._items = items

    async def discover_accounts(self):  # pragma: no cover - not reached
        return []

    async def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]:
        for it in self._items:
            yield it


async def _seed_card(session_factory) -> tuple[int, str]:
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
    return acc_id, src


async def _category_id(session_factory, name: str) -> int:
    async with session_factory() as s:
        return (
            await s.execute(text("SELECT id FROM categories WHERE name = :n"), {"n": name})
        ).scalar_one()


@pytest.mark.asyncio
async def test_reimport_leaves_locked_manual_row_untouched_and_categorizes_new(
    session_factory,
):
    acc_id, src = await _seed_card(session_factory)
    groceries = await _category_id(session_factory, "Groceries")
    # A DELIBERATELY WRONG manual category for the locked row, so a rule firing
    # would visibly change it — the test only passes if the lock holds.
    entertainment = await _category_id(session_factory, "Entertainment")

    # Pre-existing locked, manually-categorized grocery debit (a rule WOULD match
    # mcc 5411 -> Groceries, but the row is locked to Entertainment by hand).
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO transactions "
                "  (account_id, source_tx_id, amount_minor, currency, time, raw_payload, "
                "   hold, mcc, description, category_id, category_source, is_user_locked, "
                "   attributed_day) "
                "VALUES "
                "  (:a, 'locked-tx', -5000, 'UAH', now(), '{}'::jsonb, false, 5411, 'ATB', "
                "   :cid, 'manual', true, (now() AT TIME ZONE 'Europe/Kyiv')::date)"
            ),
            {"a": acc_id, "cid": entertainment},
        )

    now = datetime.now(UTC)
    # The re-import statement re-touches the locked row AND brings a fresh
    # non-locked grocery debit that should be auto-categorized.
    items = [
        CanonicalTransaction(
            source_tx_id="locked-tx",
            source_account_id=src,
            occurred_at=now,
            amount_minor=-5000,
            currency="UAH",
            raw={},
            hold=False,
            mcc=5411,
            description="ATB",
        ),
        CanonicalTransaction(
            source_tx_id="fresh-tx",
            source_account_id=src,
            occurred_at=now,
            amount_minor=-1200,
            currency="UAH",
            raw={},
            hold=False,
            mcc=5411,
            description="ATB",
        ),
    ]

    service = ImportService(session_factory, _StubImporter(items))
    await service.run_one_card(now=now)

    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT source_tx_id, category_id, category_source, is_user_locked "
                    "FROM transactions WHERE account_id = :a ORDER BY source_tx_id"
                ),
                {"a": acc_id},
            )
        ).all()
    by_id = {r.source_tx_id: r for r in rows}

    # CAT-04 headline: the locked manual row is byte-for-byte unchanged.
    locked = by_id["locked-tx"]
    assert locked.is_user_locked is True
    assert locked.category_source == "manual"
    assert locked.category_id == entertainment  # NOT re-bucketed to Groceries

    # The fresh non-locked grocery debit was auto-categorized by the rule engine.
    fresh = by_id["fresh-tx"]
    assert fresh.is_user_locked is False
    assert fresh.category_source == "rule"
    assert fresh.category_id == groceries
