"""SchedulerStateRepo — single owner of writes against the scheduler_state singleton.

The id=1 row is seeded by migration 0002 and protected by a CHECK constraint
(D-15 + RESEARCH.md Pattern 5). Reads come from process-cached state in the
runner; writes happen on 401-detection only.
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class SchedulerStateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def read(self) -> tuple[str, str | None, datetime] | None:
        row = (
            await self._s.execute(
                text("SELECT state, last_error, since FROM scheduler_state WHERE id = 1")
            )
        ).first()
        return (row[0], row[1], row[2]) if row else None

    async def write(self, state: str, last_error: str | None) -> None:
        await self._s.execute(
            text(
                "UPDATE scheduler_state "
                "SET state = :state, last_error = :err, since = now() "
                "WHERE id = 1"
            ),
            {"state": state, "err": last_error},
        )
