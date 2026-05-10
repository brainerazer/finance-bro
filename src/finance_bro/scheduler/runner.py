"""SchedulerRunner — owns the tick logic, the recovery sweep, and the lifecycle helpers.

The runner is instantiated once per process from the FastAPI lifespan (D-04).
APScheduler fires `runner.tick()` every 10s with `max_instances=1, coalesce=True`
(D-03). The runner does NOT own rate limiting — `RateLimitGate` (Phase 1) is the
sole 65s budget owner; `gate.acquire()` runs inside `MonobankImporter.fetch_statement`
which the runner calls.

Anti-patterns explicitly avoided (per RESEARCH.md):
  - No secondary thread-blocking sleeps or duplicate timestamp trackers; the
    APScheduler IntervalTrigger is the sole clock source.
  - No SQLAlchemyJobStore — APScheduler MemoryJobStore is correct; persisted
    state lives in `import_runs` and `scheduler_state`, not in the schedule.
  - No row-level lock preamble on `import_runs` claim — single tick consumer.
  - No catching of raw httpx status errors in tick — typed exceptions only.
  - No multiplying timestamps by 1000 (Mono accepts UNIX seconds).
"""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_bro.db.account_repo import AccountRepo
from finance_bro.db.import_run_repo import ImportRunRepo
from finance_bro.db.models import Account
from finance_bro.db.scheduler_state_repo import SchedulerStateRepo
from finance_bro.db.transaction_repo import TransactionRepo
from finance_bro.importers.monobank import MonobankImporter
from finance_bro.scheduler.errors import (
    MonoAuthError,
    MonoRateLimitError,
    MonoTransientError,
)
from finance_bro.scheduler.window import backfill_chunks

# D-16: live-poll lookback window. window_from = now - 1h means we re-fetch the
# last hour every tick — small enough to be cheap, large enough that a missed
# tick doesn't lose transactions.
LIVE_POLL_LOOKBACK = timedelta(hours=1)
# Pattern 7: stale in_flight rows older than 5 min are presumed crashed and
# reset to pending.
RECOVER_THRESHOLD_SECONDS = 300

_log = structlog.get_logger()


class SchedulerRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        importer: MonobankImporter,
    ) -> None:
        self._session_factory = session_factory
        self._importer = importer
        # Cached after the first read_state(); the runner never re-reads inside
        # tick (D-15 + Pattern 5 — sticky bit lives on disk and is read once).
        self._cached_state: tuple[str, str | None] = ("running", None)

    # ---- lifecycle helpers (called by lifespan) ----

    async def recover_in_flight(self) -> int:
        """Sweep stale in_flight rows back to pending (Pattern 7).

        Returns count swept. Called once at lifespan startup.
        """
        async with self._session_factory() as session, session.begin():
            count = await ImportRunRepo(session).recover_in_flight(RECOVER_THRESHOLD_SECONDS)
        if count:
            _log.info("scheduler.recover.in_flight_swept", count=count)
        return count

    async def read_state(self) -> tuple[str, str | None]:
        """Read scheduler_state singleton; cache in process.

        Called at lifespan startup and once per process. Tick consults the cached
        copy only (D-15 + Pattern 5 — sticky bit, no need to re-read).
        """
        async with self._session_factory() as session, session.begin():
            result = await SchedulerStateRepo(session).read()
        if result is None:
            self._cached_state = ("running", None)
        else:
            state, last_err, _since = result
            self._cached_state = (state, last_err)
        return self._cached_state

    async def aclose(self) -> None:
        await self._importer.aclose()

    # ---- enqueue helpers (used by lifespan + Plan 02-04 routes) ----

    async def enqueue_backfill(
        self,
        account_id: int | None = None,
        months: int = 12,
    ) -> list[int]:
        """Enqueue `months` backfill chunks per active card (or just the requested
        account). Returns the list of inserted import_run ids.
        """
        now = datetime.now(UTC)
        chunks = list(backfill_chunks(now, months=months))
        ids_out: list[int] = []
        async with self._session_factory() as session, session.begin():
            accounts = await AccountRepo(session).list_pollable_cards()
            if account_id is not None:
                accounts = [a for a in accounts if a.id == account_id]
            for acc in accounts:
                ids = await ImportRunRepo(session).enqueue_backfill(acc.id, chunks)
                ids_out.extend(ids)
        _log.info(
            "scheduler.backfill.enqueue", account_count=len(accounts), runs=len(ids_out)
        )
        return ids_out

    async def enqueue_live_for_all_active_cards(self) -> list[tuple[int, int]]:
        """D-16: enqueue a live-poll import_run for each active card. Used by
        the reshaped POST /api/import (Plan 02-04). window = now-1h..now.
        Returns list of (account_id, import_run_id) tuples.

        BL-01 guards: skip a card when (a) its backfill is still draining (D-06),
        or (b) a live row for that card is already pending/in_flight. Repeated
        force-poll clicks during a 12-month backfill must NOT pile unbounded
        duplicate live rows behind the backfill queue.
        """
        now = datetime.now(UTC)
        window_from = now - LIVE_POLL_LOOKBACK
        out: list[tuple[int, int]] = []
        async with self._session_factory() as session, session.begin():
            accounts = await AccountRepo(session).list_pollable_cards()
            repo = ImportRunRepo(session)
            for acc in accounts:
                # D-06: skip cards whose backfill is still draining; the live
                # row would otherwise sit behind ~12 backfill rows in
                # claim_next_pending (ORDER BY created_at ASC).
                if await repo.count_pending_or_in_flight_backfill(acc.id) > 0:
                    continue
                # BL-01: dedup. A force-poll request for a card that already
                # has a pending/in_flight live row is a no-op.
                if await repo.count_pending_or_in_flight_live(acc.id) > 0:
                    continue
                run_id = await repo.enqueue_live(acc.id, window_from, now)
                out.append((acc.id, run_id))
        _log.info("scheduler.live.enqueue", account_count=len(accounts), runs=len(out))
        return out

    # ---- discovery (cold-boot) ----

    async def _ensure_accounts_discovered(self) -> None:
        """If accounts table is empty, run discovery. Mirrors the ImportService
        Phase 1 path and is safe to call multiple times due to `uq_accounts_source`.
        Raises MonoAuthError/MonoRateLimitError/MonoTransientError — caller (tick)
        handles them.
        """
        async with self._session_factory() as session, session.begin():
            existing = await AccountRepo(session).list_all()
            if existing:
                return
        discovered = await self._importer.discover_accounts()
        if not discovered:
            return
        async with self._session_factory() as session, session.begin():
            await AccountRepo(session).upsert_many(discovered)

    # ---- pick-next helpers ----

    async def _pick_next_active_card(self) -> Account | None:
        """D-02 + Discretion bullet 5 step 4: cards by oldest last-live completed_at,
        skipping any account whose backfill is in progress (D-06). Falls back to
        id-asc among never-polled cards (last_live is None for them).

        BL-02: also skip cards whose most recent live run is `pending` or
        `in_flight` — they are already queued/running, enqueueing another live
        row would create a duplicate. (Without this filter, an in_flight row
        with completed_at=None collapses to datetime.min via the `or` below
        and "wins" the rotation, triggering a duplicate enqueue every tick.)
        """
        async with self._session_factory() as session, session.begin():
            cards = await AccountRepo(session).list_pollable_cards()
            if not cards:
                return None
            ir_repo = ImportRunRepo(session)
            last_live = await ir_repo.last_live_per_account()
            # Filter out cards with active backfill (D-06) or with a non-terminal
            # live row already queued/running (BL-02).
            eligible: list[Account] = []
            for c in cards:
                if await ir_repo.count_pending_or_in_flight_backfill(c.id) > 0:
                    continue
                last = last_live.get(c.id)
                if last is not None and last.status in ("pending", "in_flight"):
                    continue
                eligible.append(c)
            if not eligible:
                return None
            # Prefer never-polled (last_live is None) by id ASC; otherwise
            # oldest completed_at. After the BL-02 filter above, every
            # eligible card with a last_live entry has a terminal status
            # (`done` or `error`), so completed_at is non-null and the `or`
            # fallback is dead code — kept for defensive typing.
            never_polled = [c for c in eligible if c.id not in last_live]
            if never_polled:
                # `cards` is already ORDER BY id ASC.
                return never_polled[0]
            return min(
                eligible,
                key=lambda c: last_live[c.id].completed_at
                or datetime.min.replace(tzinfo=UTC),
            )

    # ---- tick (the heart) ----

    async def tick(self) -> None:
        """Single tick of the scheduler.

        Body matches RESEARCH.md Code Examples §3:
          1. If state != running -> no-op.
          2. Cold-boot: discover accounts if table is empty.
          3. Claim oldest pending import_run.
          4. If no pending: pick next active card and enqueue a live row, return.
          5. Otherwise: fetch_statement -> insert_many -> mark_done.
          6. Typed errors branch on intent (401 sticks state, 429 transient, etc.).
        """
        if self._cached_state[0] != "running":
            return

        # WR-03: sweep stale in_flight rows every tick (cheap UPDATE, no-op
        # when nothing is stale). The 5-min threshold is meaningless in a
        # long-lived process if recover_in_flight only runs at startup —
        # a tick-time _mark_error failure (DB blip between claim_next_pending
        # and mark_error) would leave the row in_flight indefinitely. Running
        # it per tick is the root-cause fix that also closes BL-02 fully.
        try:
            await self.recover_in_flight()
        except Exception:  # noqa: BLE001
            # Don't let a sweep failure abort the tick; log and continue so
            # the rest of the tick body still has a chance to do useful work.
            _log.exception("scheduler.tick.recover.failed")

        # Cold-boot: ensure discovery has run.
        try:
            await self._ensure_accounts_discovered()
        except MonoAuthError as e:
            await self._set_state_auth_failed(str(e))
            _log.error("scheduler.tick.discovery.auth_failed")
            return
        except (MonoRateLimitError, MonoTransientError) as e:
            _log.warning("scheduler.tick.discovery.transient", error=str(e))
            return  # next tick retries

        # Claim a pending row.
        async with self._session_factory() as session, session.begin():
            run = await ImportRunRepo(session).claim_next_pending()

        if run is None:
            # No pending — enqueue a fresh live row for the next active card.
            card = await self._pick_next_active_card()
            if card is None:
                return
            now = datetime.now(UTC)
            window_from = now - LIVE_POLL_LOOKBACK
            async with self._session_factory() as session, session.begin():
                await ImportRunRepo(session).enqueue_live(card.id, window_from, now)
            return

        # Have a claimed run — fetch + upsert.
        _log.info(
            "scheduler.tick.run.start",
            import_run_id=run.id,
            account_id=run.account_id,
            run_kind=run.run_kind,
            window_from=run.window_from.isoformat(),
            window_to=run.window_to.isoformat(),
        )
        try:
            async with self._session_factory() as session, session.begin():
                account = await session.get(Account, run.account_id)
            if account is None:
                await self._mark_error(run.id, "account row missing")
                return
            items = [
                t
                async for t in self._importer.fetch_statement(
                    account.source_account_id, run.window_from, run.window_to
                )
            ]
            async with self._session_factory() as session, session.begin():
                inserted, updated = await TransactionRepo(session).insert_many(
                    account.id, items
                )
                await ImportRunRepo(session).mark_done(
                    run.id,
                    statement_count=len(items),
                    inserted=inserted,
                    updated=updated,
                )
            _log.info(
                "scheduler.tick.run.done",
                import_run_id=run.id,
                account_id=run.account_id,
                statement_count=len(items),
                inserted=inserted,
                updated_in_place=updated,
            )
        except MonoAuthError as e:
            await self._mark_error(run.id, str(e))
            await self._set_state_auth_failed(str(e))
            _log.error("scheduler.tick.auth_failed", import_run_id=run.id)
        except MonoRateLimitError as e:
            await self._mark_error(
                run.id, f"429 (Retry-After={e.retry_after_seconds})"
            )
            _log.warning(
                "scheduler.tick.mono_429",
                import_run_id=run.id,
                retry_after=e.retry_after_seconds,
            )
        except MonoTransientError as e:
            await self._mark_error(run.id, str(e))
            _log.warning(
                "scheduler.tick.transient", import_run_id=run.id, error=str(e)
            )
        except Exception as e:  # noqa: BLE001
            await self._mark_error(run.id, repr(e))
            _log.exception("scheduler.tick.unexpected", import_run_id=run.id)

    # ---- internal helpers ----

    async def _mark_error(self, run_id: int, error: str) -> None:
        async with self._session_factory() as session, session.begin():
            await ImportRunRepo(session).mark_error(run_id, error)

    async def _set_state_auth_failed(self, error: str) -> None:
        async with self._session_factory() as session, session.begin():
            await SchedulerStateRepo(session).write("auth_failed", error)
        self._cached_state = ("auth_failed", error)
