"""ImportRunRepo unit tests.

Each test maps to a behavior in must_haves.truths from plan 02-01. Uses
testcontainers Postgres via session_factory (Archetype B from PATTERNS.md).

Per-test isolation: the autouse fixture truncates import_runs and
mono.card accounts so claim_next_pending's global queue starts empty for
every test. The conftest `client` fixture only truncates between HTTP-route
tests; tests using session_factory directly need their own reset.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from finance_bro.db.import_run_repo import ImportRunRepo


@pytest_asyncio.fixture(autouse=True)
async def _truncate_runs(session_factory):
    """Reset import_runs (child) + accounts (parent) before each test in this
    file so claim_next_pending sees an empty queue. CASCADE handles any
    transactions FKs."""
    async with session_factory() as s:
        await s.execute(
            text(
                "TRUNCATE TABLE transactions, import_runs, accounts "
                "RESTART IDENTITY CASCADE"
            )
        )
        await s.commit()
    yield


async def _seed_account(session_factory, source_account_id: str = "acc-irr-1") -> int:
    """Create a minimal mono.card row and return its id."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts "
                "(source_kind, source_account_id, currency, raw_payload, mono_type) "
                "VALUES ('mono.card', :sa, 'UAH', '{}'::jsonb, 'black')"
            ),
            {"sa": source_account_id},
        )
        acc_id = (
            await s.execute(
                text("SELECT id FROM accounts WHERE source_account_id = :sa"),
                {"sa": source_account_id},
            )
        ).scalar_one()
        await s.commit()
    return int(acc_id)


@pytest.mark.asyncio
async def test_enqueue_backfill_creates_twelve_pending_rows(session_factory):
    acc_id = await _seed_account(session_factory, "acc-bf-12")
    base = datetime(2026, 5, 1, tzinfo=UTC)
    chunks = [
        (base - timedelta(days=30 * (i + 1)), base - timedelta(days=30 * i))
        for i in range(12)
    ]

    async with session_factory() as s:
        repo = ImportRunRepo(s)
        ids = await repo.enqueue_backfill(acc_id, chunks)
        await s.commit()

    assert len(ids) == 12

    async with session_factory() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM import_runs "
                    "WHERE account_id = :id AND run_kind='backfill' "
                    "  AND status='pending'"
                ),
                {"id": acc_id},
            )
        ).scalar_one()
    assert count == 12


@pytest.mark.asyncio
async def test_enqueue_live_creates_one_pending_row(session_factory):
    acc_id = await _seed_account(session_factory, "acc-live-1")
    frm = datetime(2026, 5, 1, tzinfo=UTC)
    to = datetime(2026, 5, 10, tzinfo=UTC)

    async with session_factory() as s:
        repo = ImportRunRepo(s)
        new_id = await repo.enqueue_live(acc_id, frm, to)
        await s.commit()

    assert isinstance(new_id, int) and new_id > 0

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT run_kind, status FROM import_runs WHERE id = :id"
                ),
                {"id": new_id},
            )
        ).first()
    assert row is not None
    assert row[0] == "live"
    assert row[1] == "pending"


@pytest.mark.asyncio
async def test_claim_next_pending_returns_oldest_and_marks_in_flight(session_factory):
    acc_id = await _seed_account(session_factory, "acc-claim-1")
    # Insert three pending rows with explicit created_at staggering so
    # ORDER BY created_at ASC is deterministic.
    async with session_factory() as s:
        for i in range(3):
            await s.execute(
                text(
                    "INSERT INTO import_runs "
                    "(account_id, run_kind, window_from, window_to, status, "
                    " created_at) "
                    "VALUES (:a, 'live', now() - interval '1 hour', now(), "
                    "        'pending', now() - make_interval(secs => :s))"
                ),
                {"a": acc_id, "s": 60 * (3 - i)},
            )
        await s.commit()

    async with session_factory() as s:
        oldest_id = (
            await s.execute(
                text(
                    "SELECT id FROM import_runs WHERE account_id = :a "
                    "ORDER BY created_at ASC LIMIT 1"
                ),
                {"a": acc_id},
            )
        ).scalar_one()

    async with session_factory() as s:
        repo = ImportRunRepo(s)
        claimed = await repo.claim_next_pending()
        await s.commit()
    assert claimed is not None
    assert claimed.id == oldest_id

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, attempts, started_at "
                    "FROM import_runs WHERE id = :id"
                ),
                {"id": claimed.id},
            )
        ).first()
    assert row is not None
    assert row[0] == "in_flight"
    assert row[1] == 1
    assert row[2] is not None


@pytest.mark.asyncio
async def test_claim_next_pending_returns_none_when_empty(session_factory):
    async with session_factory() as s:
        repo = ImportRunRepo(s)
        result = await repo.claim_next_pending()
        await s.commit()
    assert result is None


@pytest.mark.asyncio
async def test_mark_done_records_counts_and_completed_at(session_factory):
    acc_id = await _seed_account(session_factory, "acc-done-1")
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO import_runs "
                "(account_id, run_kind, window_from, window_to, status) "
                "VALUES (:a, 'live', now() - interval '1 hour', now(), 'pending')"
            ),
            {"a": acc_id},
        )
        await s.commit()

    async with session_factory() as s:
        repo = ImportRunRepo(s)
        claimed = await repo.claim_next_pending()
        assert claimed is not None
        await repo.mark_done(claimed.id, statement_count=7, inserted=5, updated=2)
        await s.commit()

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, statement_count, inserted, completed_at, "
                    "       last_error "
                    "FROM import_runs WHERE id = :id"
                ),
                {"id": claimed.id},
            )
        ).first()
    assert row is not None
    assert row[0] == "done"
    assert row[1] == 7
    assert row[2] == 5
    assert row[3] is not None
    assert row[4] is None


@pytest.mark.asyncio
async def test_mark_error_sets_status_and_last_error(session_factory):
    acc_id = await _seed_account(session_factory, "acc-err-1")
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO import_runs "
                "(account_id, run_kind, window_from, window_to, status) "
                "VALUES (:a, 'live', now() - interval '1 hour', now(), 'pending')"
            ),
            {"a": acc_id},
        )
        await s.commit()

    async with session_factory() as s:
        repo = ImportRunRepo(s)
        claimed = await repo.claim_next_pending()
        assert claimed is not None
        await repo.mark_error(claimed.id, error="HTTP 500: upstream blew up")
        await s.commit()

    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, last_error, completed_at "
                    "FROM import_runs WHERE id = :id"
                ),
                {"id": claimed.id},
            )
        ).first()
    assert row is not None
    assert row[0] == "error"
    assert row[1] == "HTTP 500: upstream blew up"
    assert row[2] is not None


@pytest.mark.asyncio
async def test_recover_in_flight_resets_stale_rows(session_factory):
    acc_id = await _seed_account(session_factory, "acc-rec-1")
    async with session_factory() as s:
        # Stale: started 6 minutes ago, threshold is 5 min → must reset.
        await s.execute(
            text(
                "INSERT INTO import_runs "
                "(account_id, run_kind, window_from, window_to, status, "
                " started_at) "
                "VALUES (:a, 'live', now() - interval '1 hour', now(), "
                "        'in_flight', now() - interval '6 minutes')"
            ),
            {"a": acc_id},
        )
        # Fresh: started 1 minute ago → must NOT be touched.
        await s.execute(
            text(
                "INSERT INTO import_runs "
                "(account_id, run_kind, window_from, window_to, status, "
                " started_at) "
                "VALUES (:a, 'live', now() - interval '1 hour', now(), "
                "        'in_flight', now() - interval '1 minute')"
            ),
            {"a": acc_id},
        )
        await s.commit()

    async with session_factory() as s:
        repo = ImportRunRepo(s)
        reset_count = await repo.recover_in_flight(threshold_seconds=300)
        await s.commit()
    assert reset_count == 1

    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT status, started_at FROM import_runs "
                    "WHERE account_id = :a ORDER BY id ASC"
                ),
                {"a": acc_id},
            )
        ).all()
    assert rows[0][0] == "pending"
    assert rows[0][1] is None
    assert rows[1][0] == "in_flight"
    assert rows[1][1] is not None


@pytest.mark.asyncio
async def test_count_pending_or_in_flight_backfill_returns_count(session_factory):
    acc_id = await _seed_account(session_factory, "acc-count-1")
    async with session_factory() as s:
        # 2 pending backfill, 1 in_flight backfill, 1 done backfill, 1 pending live.
        for status in ("pending", "pending", "in_flight", "done"):
            await s.execute(
                text(
                    "INSERT INTO import_runs "
                    "(account_id, run_kind, window_from, window_to, status) "
                    "VALUES (:a, 'backfill', now() - interval '30 days', now(), :st)"
                ),
                {"a": acc_id, "st": status},
            )
        await s.execute(
            text(
                "INSERT INTO import_runs "
                "(account_id, run_kind, window_from, window_to, status) "
                "VALUES (:a, 'live', now() - interval '1 hour', now(), 'pending')"
            ),
            {"a": acc_id},
        )
        await s.commit()

    async with session_factory() as s:
        repo = ImportRunRepo(s)
        count = await repo.count_pending_or_in_flight_backfill(acc_id)
    assert count == 3
