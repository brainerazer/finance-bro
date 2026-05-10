"""ImportRunRepo unit tests.

Wave 0 scaffolding — bodies are filled in Task 3 of plan 02-01 once the repo
module exists. Each test name maps to a behavior in must_haves.truths.
"""

import pytest
from sqlalchemy import text  # noqa: F401  (used once Task 3 fills the bodies)


@pytest.mark.asyncio
async def test_enqueue_backfill_creates_twelve_pending_rows(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_enqueue_live_creates_one_pending_row(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_claim_next_pending_returns_oldest_and_marks_in_flight(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_claim_next_pending_returns_none_when_empty(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_mark_done_records_counts_and_completed_at(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_mark_error_sets_status_and_last_error(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_recover_in_flight_resets_stale_rows(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_count_pending_or_in_flight_backfill_returns_count(session_factory):
    pytest.fail("TODO Task 3")
