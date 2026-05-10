"""ImportRunRepo — claim/enqueue/audit for the scheduler.

Single tick consumer per D-03 (APScheduler max_instances=1) means no skip-locked
or advisory-lock preamble is needed (RESEARCH.md Pattern 2). The (status, created_at) btree
index keeps `claim_next_pending` O(log n) on the import_runs queue.

Method roles:
- claim_next_pending: atomic "next pending → in_flight" transition with attempts++.
- enqueue_backfill: bulk-insert N pending rows for a fresh account's 12-month chunks.
- enqueue_live: insert one pending row for the next live tick.
- mark_done / mark_error: terminal-state writes + statement counts (D-08).
- recover_in_flight: sweep stale in_flight rows back to pending (RESEARCH.md Pattern 7).
- count_pending_or_in_flight_backfill: D-06 — runner skips live polling while backfill is in progress.
- last_live_per_account: DISTINCT ON for status surface + round-robin "oldest last-poll wins".
"""

from datetime import datetime

from sqlalchemy import insert as sa_insert
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.db.models import ImportRun


class ImportRunRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def claim_next_pending(self) -> ImportRun | None:
        """Atomically transition the oldest pending row to in_flight.

        UPDATE ... WHERE id = (SELECT ... LIMIT 1) — no row-level lock preamble
        needed because max_instances=1 means there is exactly one tick consumer
        at a time.
        """
        row = (
            await self._s.execute(
                text(
                    "UPDATE import_runs "
                    "SET status='in_flight', started_at=now(), attempts=attempts+1 "
                    "WHERE id = ("
                    "  SELECT id FROM import_runs "
                    "  WHERE status='pending' "
                    "  ORDER BY created_at ASC LIMIT 1"
                    ") "
                    "RETURNING *"
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        # Hydrate ORM instance from RETURNING * mapping.
        return ImportRun(**dict(row))

    async def enqueue_backfill(
        self,
        account_id: int,
        chunks: list[tuple[datetime, datetime]],
    ) -> list[int]:
        """Bulk-insert backfill chunks; return inserted ids in creation order."""
        if not chunks:
            return []
        rows = [
            {
                "account_id": account_id,
                "run_kind": "backfill",
                "window_from": frm,
                "window_to": to,
                "status": "pending",
            }
            for frm, to in chunks
        ]
        stmt = sa_insert(ImportRun).values(rows).returning(ImportRun.id)
        result = await self._s.execute(stmt)
        return list(result.scalars().all())

    async def enqueue_live(
        self,
        account_id: int,
        window_from: datetime,
        window_to: datetime,
    ) -> int:
        """Insert one pending live row; return its id."""
        stmt = (
            sa_insert(ImportRun)
            .values(
                account_id=account_id,
                run_kind="live",
                window_from=window_from,
                window_to=window_to,
                status="pending",
            )
            .returning(ImportRun.id)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one()

    async def mark_done(
        self,
        run_id: int,
        statement_count: int,
        inserted: int,
    ) -> None:
        """Mark a run done. Persists `inserted` (matches D-08 column name).

        D-17 explicitly forbids extra audit columns (no separate `updated`
        column on import_runs in v1). The runner decides what `inserted`
        means: pass `inserted+updated` for "rows touched" semantics, or
        pass strict insertions only. The "updated_in_place" count is logged
        via structlog at the runner instead of stored.

        WR-01: this method previously accepted an `updated` parameter that
        it silently discarded via `del updated` — misleading API surface.
        Removed; the runner now sums explicitly at the call site.
        """
        await self._s.execute(
            text(
                "UPDATE import_runs "
                "SET status='done', completed_at=now(), "
                "    statement_count=:c, inserted=:i, last_error=NULL "
                "WHERE id=:id"
            ),
            {"c": statement_count, "i": inserted, "id": run_id},
        )

    async def mark_error(self, run_id: int, error: str) -> None:
        await self._s.execute(
            text(
                "UPDATE import_runs "
                "SET status='error', completed_at=now(), last_error=:err "
                "WHERE id=:id"
            ),
            {"err": error, "id": run_id},
        )

    async def recover_in_flight(self, threshold_seconds: int = 300) -> int:
        """Reset in_flight rows older than threshold back to pending.

        RESEARCH.md Pattern 7. Now called per-tick by the runner (WR-03), so
        a crash mid-tick (or a _mark_error failure) doesn't leave a row stuck
        in_flight indefinitely. Cheap UPDATE; no-op when nothing is stale.
        """
        # WR-09: drop redundant list(...) wrap — `result.scalars().all()`
        # already returns a list; `len()` of it is fine. Using rowcount on
        # async drivers is unreliable across versions, so we keep the
        # RETURNING materialization.
        result = await self._s.execute(
            text(
                "UPDATE import_runs "
                "SET status='pending', started_at=NULL "
                "WHERE status='in_flight' "
                "  AND started_at < now() - make_interval(secs => :s) "
                "RETURNING id"
            ),
            {"s": threshold_seconds},
        )
        return len(result.scalars().all())

    async def count_pending_or_in_flight_backfill(self, account_id: int) -> int:
        """D-06: runner skips live polling for an account whose backfill is still
        running."""
        row = (
            await self._s.execute(
                text(
                    "SELECT count(*) FROM import_runs "
                    "WHERE account_id=:id "
                    "  AND run_kind='backfill' "
                    "  AND status IN ('pending','in_flight')"
                ),
                {"id": account_id},
            )
        ).first()
        return int(row[0]) if row else 0

    async def count_pending_or_in_flight_live(self, account_id: int) -> int:
        """BL-01: dedup guard. A force-poll request when a live row is already
        queued (or running) is a no-op — repeated /api/import clicks must not
        accumulate unbounded duplicate live rows for the same card."""
        row = (
            await self._s.execute(
                text(
                    "SELECT count(*) FROM import_runs "
                    "WHERE account_id=:id "
                    "  AND run_kind='live' "
                    "  AND status IN ('pending','in_flight')"
                ),
                {"id": account_id},
            )
        ).first()
        return int(row[0]) if row else 0

    async def last_live_per_account(self) -> dict[int, ImportRun]:
        """Most recent live run per account, keyed by account_id.

        Used by SchedulerRunner.pick_next_active_card (oldest last-poll wins) AND
        by 02-04's status surface (single query, two paths).
        """
        rows = (
            await self._s.execute(
                select(ImportRun)
                .where(ImportRun.run_kind == "live")
                .order_by(
                    ImportRun.account_id.asc(),
                    ImportRun.completed_at.desc().nulls_last(),
                )
                .distinct(ImportRun.account_id)
            )
        ).scalars().all()
        return {r.account_id: r for r in rows}
