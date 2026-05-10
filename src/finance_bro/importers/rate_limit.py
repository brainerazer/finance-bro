"""Persistent token-bucket rate-limit gate for the Mono API.

The gate is owned by MonobankImporter (single instance per token), persists
last_acquired_at in Postgres so a container restart cannot violate the 1-req-
per-60s limit (Pitfall 1), and uses SELECT ... FOR UPDATE to serialize
concurrent acquirers (Pattern 1).

Note on the timestamp written to disk: when a caller has to wait, the row is
updated to the *next allowed* slot (claim_ts = wait_until), not "now". That is
what makes concurrent acquirers serialize correctly: caller B reads caller A's
forward-dated claim under FOR UPDATE, computes a further-forward claim of its
own, and ends up sleeping past A's slot.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_bro.db.rate_state_repo import RateStateRepo

MONO_RATE_LIMIT_SECONDS = 65  # 60s API limit + 5s slack for clock drift


class RateLimitGate:
    """Postgres-backed token-bucket gate. Single instance per token. Persists
    last_acquired_at in mono_rate_state so a container restart cannot violate
    Mono 1 req / 60s rule (Pitfall 1). Uses SELECT FOR UPDATE so concurrent
    acquirers serialize (Pattern 1)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def acquire(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        # Sentinel is far enough in the past that a freshly inserted row never
        # blocks the first acquirer. Using a fixed epoch (not "now") so a race
        # between sentinel insert and SELECT FOR UPDATE always sees the same
        # initial state.
        sentinel = datetime(1970, 1, 1, tzinfo=UTC)
        now = datetime.now(UTC)
        wait_until: datetime | None = None

        async with self._session_factory() as session, session.begin():
            repo = RateStateRepo(session)
            # Ensure the row exists so SELECT ... FOR UPDATE has something
            # to lock. Without this, concurrent first-time acquirers would
            # both observe an empty result and both proceed without
            # serializing.
            await repo.ensure_row(token_hash, sentinel)
            last = await repo.select_for_update(token_hash)
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                next_allowed = last + timedelta(seconds=MONO_RATE_LIMIT_SECONDS)
                if next_allowed > now:
                    wait_until = next_allowed
            claim_ts = wait_until or now
            await repo.upsert(token_hash, claim_ts)

        if wait_until is not None:
            remaining = (wait_until - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
