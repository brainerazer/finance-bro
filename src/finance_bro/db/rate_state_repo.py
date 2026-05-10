"""Repository owning all writes to mono_rate_state.

RateLimitGate is the only consumer; this layer keeps SQL out of the importers
package. Writes go through SELECT ... FOR UPDATE to serialize concurrent
acquirers (Pattern 1 from RESEARCH.md).
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class RateStateRepo:
    """Single owner of writes to mono_rate_state. RateLimitGate uses this."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def ensure_row(self, token_hash: str, sentinel: datetime) -> None:
        """Insert a sentinel row if absent. Required so SELECT ... FOR UPDATE
        has a row to lock — without it, two concurrent first-time acquirers
        would both observe an empty result and both proceed without serializing
        (#concurrent_serialize). The sentinel timestamp is far enough in the
        past that the immediate FOR UPDATE read sees a non-blocking last."""
        await self._s.execute(
            text(
                "INSERT INTO mono_rate_state (token_hash, last_acquired_at) "
                "VALUES (:h, :ts) "
                "ON CONFLICT (token_hash) DO NOTHING"
            ),
            {"h": token_hash, "ts": sentinel},
        )

    async def select_for_update(self, token_hash: str) -> datetime | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT last_acquired_at FROM mono_rate_state WHERE token_hash = :h FOR UPDATE"
                ),
                {"h": token_hash},
            )
        ).first()
        return row[0] if row else None

    async def upsert(self, token_hash: str, ts: datetime) -> None:
        await self._s.execute(
            text(
                "INSERT INTO mono_rate_state (token_hash, last_acquired_at) "
                "VALUES (:h, :ts) "
                "ON CONFLICT (token_hash) DO UPDATE "
                "SET last_acquired_at = EXCLUDED.last_acquired_at"
            ),
            {"h": token_hash, "ts": ts},
        )
