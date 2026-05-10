"""SchedulerStateRepo unit tests.

The id=1 row is seeded by migration 0002 (and re-seeded by conftest after
TRUNCATE). The CHECK constraint on `id = 1` enforces the singleton invariant
at the DB level; the repo has no INSERT path.

Per-test isolation: autouse fixture resets the singleton row so every test
starts from a fresh ('running', NULL) state regardless of prior writes.
"""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from finance_bro.db.scheduler_state_repo import SchedulerStateRepo


@pytest_asyncio.fixture(autouse=True)
async def _reset_scheduler_state(session_factory):
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE scheduler_state "
                "SET state='running', last_error=NULL, since=now() "
                "WHERE id = 1"
            )
        )
        # If a prior test in the session somehow wiped it, re-seed.
        count = (
            await s.execute(text("SELECT count(*) FROM scheduler_state"))
        ).scalar_one()
        if count == 0:
            await s.execute(
                text("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')")
            )
        await s.commit()
    yield


@pytest.mark.asyncio
async def test_read_returns_seeded_singleton(session_factory):
    async with session_factory() as s:
        repo = SchedulerStateRepo(s)
        result = await repo.read()
    assert result is not None
    state, last_error, since = result
    assert state == "running"
    assert last_error is None
    assert since is not None


@pytest.mark.asyncio
async def test_write_updates_state_and_last_error_and_since(session_factory):
    async with session_factory() as s:
        before = (
            await s.execute(text("SELECT since FROM scheduler_state WHERE id = 1"))
        ).scalar_one()

    async with session_factory() as s:
        repo = SchedulerStateRepo(s)
        await repo.write("auth_failed", "token revoked (401)")
        await s.commit()

    async with session_factory() as s:
        repo = SchedulerStateRepo(s)
        result = await repo.read()
    assert result is not None
    state, last_error, since = result
    assert state == "auth_failed"
    assert last_error == "token revoked (401)"
    # `since` must have advanced — the write() call updates it via now().
    assert since >= before


@pytest.mark.asyncio
async def test_write_does_not_create_second_row(session_factory):
    async with session_factory() as s:
        repo = SchedulerStateRepo(s)
        await repo.write("running", None)
        await repo.write("stopped", "manual stop")
        await repo.write("running", None)
        await s.commit()

    async with session_factory() as s:
        count = (
            await s.execute(text("SELECT count(*) FROM scheduler_state"))
        ).scalar_one()
    assert count == 1

    # CHECK constraint id = 1 must reject any attempt to insert a second row.
    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            await s.execute(
                text(
                    "INSERT INTO scheduler_state (id, state) VALUES (2, 'running')"
                )
            )
            await s.commit()
