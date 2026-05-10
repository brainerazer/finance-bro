"""SchedulerStateRepo unit tests.

Wave 0 scaffolding — bodies are filled in Task 3 of plan 02-01 once the repo
module exists. Each test maps to a behavior in must_haves.truths (singleton
read/write + UPDATE-only invariant).
"""

import pytest
from sqlalchemy import text  # noqa: F401  (used once Task 3 fills the bodies)


@pytest.mark.asyncio
async def test_read_returns_seeded_singleton(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_write_updates_state_and_last_error_and_since(session_factory):
    pytest.fail("TODO Task 3")


@pytest.mark.asyncio
async def test_write_does_not_create_second_row(session_factory):
    pytest.fail("TODO Task 3")
