---
phase: 02-reliable-sync
reviewed: 2026-05-10T00:00:00Z
depth: standard
files_reviewed: 39
files_reviewed_list:
  - alembic/versions/0002_phase2_sync.py
  - pyproject.toml
  - src/finance_bro/api/deps.py
  - src/finance_bro/api/routes_backfill.py
  - src/finance_bro/api/routes_import.py
  - src/finance_bro/api/routes_status.py
  - src/finance_bro/api/schemas.py
  - src/finance_bro/db/account_repo.py
  - src/finance_bro/db/import_run_repo.py
  - src/finance_bro/db/models.py
  - src/finance_bro/db/scheduler_state_repo.py
  - src/finance_bro/db/transaction_repo.py
  - src/finance_bro/importers/base.py
  - src/finance_bro/importers/monobank.py
  - src/finance_bro/main.py
  - src/finance_bro/scheduler/__init__.py
  - src/finance_bro/scheduler/errors.py
  - src/finance_bro/scheduler/runner.py
  - src/finance_bro/scheduler/window.py
  - src/finance_bro/services/import_service.py
  - tests/conftest.py
  - tests/fixtures/client_info_multi_card.json
  - tests/fixtures/statement_cleared_followup.json
  - tests/fixtures/statement_empty.json
  - tests/fixtures/statement_with_hold.json
  - tests/test_401_stops_scheduler.py
  - tests/test_429_does_not_stop.py
  - tests/test_backfill_enqueue.py
  - tests/test_backfill_resumability.py
  - tests/test_backfill_window_math.py
  - tests/test_force_poll_endpoint.py
  - tests/test_hold_cleared_upsert.py
  - tests/test_idempotency.py
  - tests/test_import_route.py
  - tests/test_import_run_repo.py
  - tests/test_import_status_shape.py
  - tests/test_migrations.py
  - tests/test_no_auth.py
  - tests/test_scheduler_round_robin.py
  - tests/test_scheduler_state_repo.py
  - tests/test_transactions_route.py
findings:
  critical: 2
  blocker: 2
  warning: 9
  info: 4
  total: 15
status: fixed
fix_status:
  BL-01: fixed
  BL-02: fixed
  CR-01: fixed
  CR-02: fixed
  WR-01: fixed
  WR-02: fixed
  WR-03: fixed
  WR-04: fixed
  WR-05: fixed
  WR-06: fixed
  WR-07: fixed
  WR-08: fixed
  WR-09: fixed
  IN-01: deferred (info-tier, out of scope)
  IN-02: deferred (info-tier, out of scope)
  IN-03: deferred (info-tier, out of scope)
  IN-04: deferred (info-tier, out of scope)
fixed_at: 2026-05-10T00:00:00Z
test_result_after_fix: 93/93 passing (80 baseline + 13 new regression tests)
---

# Phase 2: Code Review Report

**Reviewed:** 2026-05-10
**Depth:** standard
**Files Reviewed:** 39 (including 1 unused fixture)
**Status:** issues_found

## Summary

The Phase 2 reliable-sync implementation correctly preserves the central D-10 invariant (`TransactionRepo.insert_many` mutates only `hold`/`amount_minor`/`raw_payload` on conflict — verified by reading the SQL exactly), preserves the rate-limit single-owner design (`RateLimitGate` is the only place holding the 65s budget; the scheduler does not duplicate it), and the migration round-trips cleanly. Typed importer exceptions (`MonoAuthError` / `MonoRateLimitError` / `MonoTransientError`) cleanly route 401-vs-429 in `runner.tick`.

However, the review found two **BLOCKER** defects:

1. A live-poll forced through `POST /api/import` is enqueued with a fresh `created_at` and gets queued **after** any pending backfill rows for that card (claim is `ORDER BY created_at ASC`). On a card with 12 backfill rows pending, a manual force-poll waits behind ~12 minutes of backfill polling — surprising and likely user-confusing. Worse, `enqueue_live_for_all_active_cards` ignores backfill state entirely, meaning duplicate live rows can pile up while a live tick is in flight.
2. Stale `in_flight` rows from a same-process tick crash will trigger the `_pick_next_active_card` `min(..., default=datetime.min)` branch and cause the runner to enqueue **another** live row for the same already-in-flight account, since `recover_in_flight` only runs at lifespan startup, not per-tick.

Two additional **CRITICAL** findings concern (a) lifespan resource leak when `recover_in_flight` raises (the httpx client never gets `aclose`d) and (b) `enqueue_backfill` silently no-ops when given a non-pollable `account_id` instead of returning an error to the caller.

Warnings concentrate around stale documentation (`deps.py` says the `RateLimitGate` is "shared per-process" but is actually re-instantiated per request — true safety comes from DB-side serialization, not Python identity), API ergonomics (`mark_done(updated=...)` accepts and silently discards `updated`), and test brittleness (`client._transport.app.state.runner` reaches into httpx internals).

## Blocker Issues

### BL-01: `enqueue_live_for_all_active_cards` ignores active backfill, allowing duplicate live rows during ongoing backfill

**File:** `src/finance_bro/scheduler/runner.py:116-131`
**Issue:** `_pick_next_active_card` correctly filters out cards with `count_pending_or_in_flight_backfill > 0` per D-06. However, the manual force-poll path in `enqueue_live_for_all_active_cards` does NOT apply this filter. A user clicking "Import now" while a 12-row backfill is in progress queues an additional live `import_runs` row for that card. Two consequences:

1. **User-surprising scheduling:** The new live row sits behind ~12 backfill rows in the `claim_next_pending` queue (ORDER BY `created_at` ASC); the user expects the live poll to happen next, but it's delayed by ~12 × 65s ≈ 13 minutes.
2. **Duplicate live rows accumulate:** Repeatedly clicking the button (the user has no feedback that one is already queued) creates an unbounded number of pending live rows for the same card. There is no dedup or "single pending live row per card" invariant. The status surface shows `last_status='pending'` indefinitely.

The button is described in `routes_import.py` as "an async hint, not a synchronous fetch" — but with no dedup, the hint becomes spam.

**Fix:**
```python
# In SchedulerRunner.enqueue_live_for_all_active_cards
async def enqueue_live_for_all_active_cards(self) -> list[tuple[int, int]]:
    now = datetime.now(UTC)
    window_from = now - LIVE_POLL_LOOKBACK
    out: list[tuple[int, int]] = []
    async with self._session_factory() as session, session.begin():
        accounts = await AccountRepo(session).list_pollable_cards()
        repo = ImportRunRepo(session)
        for acc in accounts:
            # D-06: skip cards whose backfill is still draining.
            if await repo.count_pending_or_in_flight_backfill(acc.id) > 0:
                continue
            # Skip if there is already a pending live row for this card —
            # a force-poll request when one is already queued is a no-op.
            existing = await repo.count_pending_live(acc.id)  # NEW helper
            if existing > 0:
                continue
            run_id = await repo.enqueue_live(acc.id, window_from, now)
            out.append((acc.id, run_id))
    _log.info("scheduler.live.enqueue", account_count=len(accounts), runs=len(out))
    return out
```
Add the corresponding repo helper:
```python
# In ImportRunRepo
async def count_pending_live(self, account_id: int) -> int:
    row = (
        await self._s.execute(
            text(
                "SELECT count(*) FROM import_runs "
                "WHERE account_id=:id AND run_kind='live' AND status='pending'"
            ),
            {"id": account_id},
        )
    ).first()
    return int(row[0]) if row else 0
```

### BL-02: `_pick_next_active_card` enqueues duplicate live row when a stale `in_flight` exists in same process

**File:** `src/finance_bro/scheduler/runner.py:153-182`
**Issue:** The runner's `_pick_next_active_card` uses `last_live_per_account()` which returns the most recent live row regardless of status (DISTINCT ON with `nulls_last`). For a card whose ONLY live row is `in_flight` (stale because a previous tick crashed mid-fetch — the broad `except Exception` mostly catches this, but `_mark_error` itself can fail, leaving the row stuck), `last_live[c.id]` exists but `completed_at is None`. The fallback `min()` key then evaluates to `datetime.min.replace(tzinfo=UTC)` and **this card "wins"** the rotation. The runner then calls `enqueue_live(card.id, ...)` and creates a duplicate live row for the same already-in-flight account.

`recover_in_flight` only runs at lifespan startup with a 5-minute threshold, so within a single process lifetime, stale in_flight rows accumulate, each triggering this enqueue path on subsequent ticks. Five-minute window can produce ~30 ticks worth of duplicate enqueues per card.

**Fix:** Filter out cards whose most recent live run is `in_flight` (or a pending live row exists), or run `recover_in_flight` periodically. Minimal patch:

```python
# In _pick_next_active_card, after fetching last_live:
async with self._session_factory() as session, session.begin():
    cards = await AccountRepo(session).list_pollable_cards()
    if not cards:
        return None
    ir_repo = ImportRunRepo(session)
    last_live = await ir_repo.last_live_per_account()
    eligible: list[Account] = []
    for c in cards:
        if await ir_repo.count_pending_or_in_flight_backfill(c.id) > 0:
            continue
        # NEW: skip cards with an in-flight or pending live run — they're
        # already queued / running, do not enqueue another.
        last = last_live.get(c.id)
        if last is not None and last.status in ("in_flight", "pending"):
            continue
        eligible.append(c)
    ...
```
Better still, run `recover_in_flight` at the top of every tick (cheap UPDATE; no-op when nothing is stale).

## Critical Issues

### CR-01: Lifespan startup leaks `httpx.AsyncClient` if `recover_in_flight` or `read_state` raises

**File:** `src/finance_bro/main.py:62-89`
**Issue:** The lifespan constructs `MonobankImporter` (which opens `httpx.AsyncClient`) BEFORE entering the `try` block. If `await runner.recover_in_flight()` or `await runner.read_state()` raises (DB connection failure, migration desync, transient pool error), the `try`/`finally` is never entered, `runner.aclose()` never runs, and the `httpx.AsyncClient` is leaked. Under `pyproject.toml`'s `filterwarnings = ["error"]`, the resulting `RuntimeWarning: coroutine was never awaited` / unclosed-client warning is escalated to an exception and the lifespan failure becomes opaque.

**Fix:**
```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    init_engine()

    session_factory = get_session_factory()
    gate = RateLimitGate(session_factory)
    importer = MonobankImporter(settings.mono_token, gate)
    scheduler = AsyncIOScheduler()
    runner = SchedulerRunner(session_factory=session_factory, importer=importer)

    try:
        await runner.recover_in_flight()
        state, _last_err = await runner.read_state()
        disable_scheduler = os.environ.get("APP_DISABLE_SCHEDULER") == "1"
        if state == "running" and not disable_scheduler:
            scheduler.add_job(
                runner.tick,
                IntervalTrigger(seconds=10),
                id="finance-bro-tick",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=30,
            )
            scheduler.start()
        app.state.scheduler = scheduler
        app.state.runner = runner
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await runner.aclose()
```

### CR-02: `enqueue_backfill(account_id=X)` silently returns `[]` when X does not exist or is not a pollable card

**File:** `src/finance_bro/scheduler/runner.py:93-114` and `src/finance_bro/api/routes_backfill.py:31-42`
**Issue:** When the user POSTs `/api/backfill` with an `account_id` that (a) doesn't exist, (b) is a `mono.fop`/`mono.jar`, or (c) is a `mono.card` filtered out by the eAid allowlist, the runner's filter step `accounts = [a for a in accounts if a.id == account_id]` produces an empty list and the response is `{"run_ids": []}` with HTTP 202. The user has no signal that their request was rejected, and operator debugging is harder (clicked button → silent zero result).

A user-supplied `account_id=99999` is indistinguishable from "everything is filtered out for legitimate reasons". From the operator's standpoint, this is a swallowed input-validation error.

**Fix:** Return 404 (or 400) when an explicit `account_id` doesn't resolve to a pollable card:
```python
# In SchedulerRunner.enqueue_backfill
async def enqueue_backfill(
    self,
    account_id: int | None = None,
    months: int = 12,
) -> list[int]:
    now = datetime.now(UTC)
    chunks = list(backfill_chunks(now, months=months))
    ids_out: list[int] = []
    async with self._session_factory() as session, session.begin():
        accounts = await AccountRepo(session).list_pollable_cards()
        if account_id is not None:
            accounts = [a for a in accounts if a.id == account_id]
            if not accounts:
                raise ValueError(
                    f"account_id={account_id} not found or not pollable "
                    f"(must be mono.card with mono_type ∈ black/platinum/white)"
                )
        ...
```
Then in the route, translate to 404:
```python
@router.post("/api/backfill", ...)
async def trigger_backfill(...):
    try:
        run_ids = await runner.enqueue_backfill(...)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    ...
```

## Warnings

### WR-01: `mark_done(updated=...)` accepts a parameter it discards via `del`

**File:** `src/finance_bro/db/import_run_repo.py:99-121`
**Issue:** The method signature accepts `updated: int`, but the body executes `del updated  # Intentionally unused — see docstring.`. The runner passes `updated=updated` thinking it gets persisted; it doesn't. The docstring explains the design (D-17 says no extra audit columns), but the API surface is misleading. A future contributor reading the call site `mark_done(run.id, statement_count=len(items), inserted=inserted, updated=updated)` will reasonably assume `updated` is recorded.

**Fix:** Drop the parameter; let the runner log `updated_in_place` separately (which it already does):
```python
async def mark_done(
    self,
    run_id: int,
    statement_count: int,
    inserted: int,  # Pass inserted+updated if you want "rows touched" semantics.
) -> None:
    await self._s.execute(
        text(
            "UPDATE import_runs SET status='done', completed_at=now(), "
            "    statement_count=:c, inserted=:i, last_error=NULL "
            "WHERE id=:id"
        ),
        {"c": statement_count, "i": inserted, "id": run_id},
    )
```
And in `runner.tick`:
```python
await ImportRunRepo(session).mark_done(
    run.id, statement_count=len(items), inserted=inserted + updated,
)
```

### WR-02: `deps.py` docstring claims a shared `RateLimitGate` per process; in fact a new one is constructed per request

**File:** `src/finance_bro/api/deps.py:1-8, 30-31`
**Issue:** The module docstring states "sharing one persistent RateLimitGate instance per process so the 1-req/60s contract is honored even across concurrent /api/import calls (Pitfall 9)." But `get_rate_gate` is `def get_rate_gate() -> RateLimitGate: return RateLimitGate(get_session_factory())` — a fresh instance per dependency invocation. The 1-req/60s contract is honored not by Python identity but by the DB-side `SELECT … FOR UPDATE` in `RateLimitGate.acquire`. This works correctly, but the docstring is wrong and any future "optimization" that relies on the claim ("the gate is shared, so we can cache its in-memory state") will break the contract.

**Fix:** Update the docstring to reflect reality:
```python
"""...
`get_rate_gate` returns a fresh `RateLimitGate` per invocation; safe because
the gate's enforcement state lives in `mono_rate_state` and is serialized via
`SELECT ... FOR UPDATE` (Pitfall 1 / Pattern 1). Concurrent /api/* callers
race for the row lock, not for an in-memory counter.
"""
```
Or, better, return a process-scoped singleton from `app.state` to make the docstring true.

### WR-03: `recover_in_flight` runs only once at startup; tick-time crashes accumulate stale rows

**File:** `src/finance_bro/scheduler/runner.py:62-71` and `src/finance_bro/main.py:65-66`
**Issue:** Crashes inside `tick()` are caught by the broad `except Exception` and converted to `_mark_error`, which is correct. But `_mark_error` itself does a DB write; if THAT fails (transient connection loss between `claim_next_pending` and `mark_error`), the row stays `in_flight` until the next process restart. The 5-minute threshold is meaningless in a long-lived process.

This is the root cause that makes BL-02 actually possible.

**Fix:** Run `recover_in_flight` at the top of every tick. It's a single UPDATE with a WHERE clause; cost is negligible when nothing is stale:
```python
async def tick(self) -> None:
    if self._cached_state[0] != "running":
        return
    # Sweep stale in_flight rows every tick (cheap no-op when none exist).
    await self.recover_in_flight()
    ...
```

### WR-04: `enqueue_backfill` does not de-duplicate against existing pending backfill rows

**File:** `src/finance_bro/scheduler/runner.py:93-114`
**Issue:** Two consecutive `POST /api/backfill` calls with the same `account_id` and `months=12` create 24 backfill `import_runs` rows. The user clicking the button twice (perhaps because the first response was slow) doubles the rate-limit budget consumption for that card. In a 12-month backfill window, a duplicate run wastes ~12 × 65s ≈ 13 minutes of the global 60s/req budget — visible to the user as "the rest of the system is paused for 13 extra minutes" since round-robin shares a single token.

**Fix:** Reject when there are existing pending/in-flight backfill rows for the requested card(s):
```python
async with self._session_factory() as session, session.begin():
    accounts = await AccountRepo(session).list_pollable_cards()
    if account_id is not None:
        accounts = [a for a in accounts if a.id == account_id]
    repo = ImportRunRepo(session)
    for acc in accounts:
        if await repo.count_pending_or_in_flight_backfill(acc.id) > 0:
            continue  # already running, skip
        ids = await repo.enqueue_backfill(acc.id, chunks)
        ids_out.extend(ids)
```
Even better, return a 409 when ALL requested accounts are already backfilling, so the user gets explicit feedback.

### WR-05: `routes_status.STATUS_QUERY` `last_live` CTE includes `pending` and `in_flight` rows, surfacing them as "last poll"

**File:** `src/finance_bro/api/routes_status.py:38-82`
**Issue:** `last_live` selects all rows with `run_kind='live'` regardless of `status`. With `NULLS LAST`, `completed_at` IS NULL rows (pending/in_flight) sort after completed rows, so a card with at least one done run shows the done one — fine. But a card whose ONLY live runs are pending/in_flight (e.g., right after the user hits force-poll, before the tick fires) shows `last_status='pending'` and `last_polled_at=null`. The semantics of "last poll" are then inconsistent — sometimes it's the last completed, sometimes the next-queued.

The frontend will need to special-case this; better to surface explicitly.

**Fix:** Restrict to terminal-state rows for the "last poll" snapshot, and surface `pending`/`in_flight` as a separate "queued" indicator:
```sql
WITH last_live AS (
    SELECT DISTINCT ON (account_id) ...
      FROM import_runs
     WHERE run_kind = 'live'
       AND status IN ('done', 'error')   -- terminal states only
     ORDER BY account_id, completed_at DESC NULLS LAST
),
queued_live AS (
    SELECT account_id, count(*) AS pending_live
      FROM import_runs
     WHERE run_kind = 'live' AND status IN ('pending','in_flight')
     GROUP BY account_id
)
...
```

### WR-06: Index `ix_import_runs_account_kind_completed` does not include `completed_at` despite its name

**File:** `alembic/versions/0002_phase2_sync.py:88-94`
**Issue:** The index is named `ix_import_runs_account_kind_completed` and the comment on line 88 says "Pitfall 5 — index for the status-page DISTINCT ON join." The status query sorts by `(account_id, completed_at DESC NULLS LAST)`, but the index covers only `(account_id, run_kind)`. Postgres can use the index for the WHERE clause but cannot drive the DISTINCT ON sort from it — a separate sort is still required.

This is borderline scope (perf is out for v1) but the misleading name will trip future readers.

**Fix:** Rename the index to match its actual coverage, OR include `completed_at`:
```python
op.create_index(
    "ix_import_runs_account_kind_completed",
    "import_runs",
    ["account_id", "run_kind", sa.text("completed_at DESC NULLS LAST")],
    postgresql_using="btree",
)
```
(Requires `sa.text()` because expression-indexes with NULLS ordering aren't column-style.)

### WR-07: Tests reach into httpx private API via `client._transport.app.state.runner`

**File:** `tests/test_idempotency.py:46`, `tests/test_transactions_route.py:18`
**Issue:** Both tests access `client._transport` (private attribute prefixed with `_`) to get to `app.state.runner`. httpx maintainers can rename `_transport` in any minor release without it counting as a breaking change. When this happens, both files break with an `AttributeError` that's confusing to debug.

**Fix:** Pass the runner via a fixture instead:
```python
# tests/conftest.py
@pytest_asyncio.fixture
async def runner(client) -> SchedulerRunner:
    """The lifespan-attached SchedulerRunner — same instance the routes use."""
    from finance_bro.main import app
    return app.state.runner
```
Then tests do `async def test_x(client, runner): ...` instead of `client._transport.app.state.runner`.

### WR-08: `tests/fixtures/client_info_multi_card.json` is unreferenced — dead test asset

**File:** `tests/fixtures/client_info_multi_card.json`
**Issue:** `grep -rn "client_info_multi_card" tests` returns no matches. The fixture was committed but never wired into any test. Either (a) the test that was supposed to use it was dropped, in which case the fixture should be deleted, or (b) a planned test was forgotten, in which case the gap should be filled or documented.

**Fix:** Delete the unused file, or add a test that consumes it (the multi-card layout would naturally exercise the round-robin discovery path with a richer payload than `client_info_minimal.json`).

### WR-09: `recover_in_flight` returns count by materializing `result.scalars().all()` only to call `len(list(...))`

**File:** `src/finance_bro/db/import_run_repo.py:139-149`
**Issue:**
```python
result = await self._s.execute(
    text("UPDATE import_runs SET status='pending' ... RETURNING id"),
    ...,
)
return len(list(result.scalars().all()))
```
`result.scalars().all()` already returns a list, so the `list(...)` wrap is redundant. Minor — but also `len()` on the result of `RETURNING id` allocates the full id list just to count. Use `result.rowcount` (Postgres reports it for UPDATE) or `SELECT count(*) FROM ... RETURNING id` patterns.

**Fix:**
```python
result = await self._s.execute(text("..."), {"s": threshold_seconds})
return result.rowcount  # type: ignore[no-any-return]  # may need cast on async cursor
```
Or, if rowcount semantics across async drivers worry you, keep `len(result.scalars().all())` — drop the redundant `list(...)` wrap.

## Info

### IN-01: Backfill window comment says "30d = 2_592_000s; cap = 2_682_000s; headroom = 90_000s = 25h" — actually 90_000s ≈ 25 hours is wrong arithmetic

**File:** `tests/test_backfill_window_math.py:45`
**Issue:** Comment claim: `headroom = 90_000s = 25h`. Math: 90_000s / 3600 = 25 hours. Hmm, that's actually correct (25h). But a 1h headroom (3600s) is what the code documentation states ("MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30  # 1h+ headroom inside the cap"). The headroom is in fact 90_000s = 25h, not "1h+". Either the comment in `window.py` is misleading (it's much more than 1h) or the slack was dialed up intentionally without updating the docstring.

Not a defect, but the inconsistency between the constant docstring ("1h+ headroom") and reality (25h headroom) is confusing.

**Fix:** Update the comment in `src/finance_bro/scheduler/window.py:15`:
```python
MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30  # 25h headroom inside the 31d+1h cap
```

### IN-02: `_pick_next_active_card` uses `min(... default=...)` antipattern via lambda

**File:** `src/finance_bro/scheduler/runner.py:178-182`
**Issue:**
```python
return min(
    eligible,
    key=lambda c: last_live[c.id].completed_at
    or datetime.min.replace(tzinfo=UTC),
)
```
The `lambda` constructs a tz-aware sentinel on every comparison. Pull it out:
```python
_DT_MIN = datetime.min.replace(tzinfo=UTC)
...
return min(
    eligible,
    key=lambda c: last_live[c.id].completed_at or _DT_MIN,
)
```
Also: as a side-effect of BL-02, this `or` collapses `None` to `_DT_MIN`, causing the bug described there.

**Fix:** See BL-02; this is the same code path.

### IN-03: `STATUS_QUERY` describes `last_poll_updated` as v1.5 work but the corresponding TODO has no tracking

**File:** `src/finance_bro/api/routes_status.py:36-37` and `src/finance_bro/api/schemas.py:71-73`
**Issue:** Both files note that `last_poll_updated` is a v1.5 deferral, with a TODO pinned to "add a separate `updated_in_place` column to import_runs in v1.5". No issue/ticket reference. When v1.5 starts, the TODO will be hard to find unless the next phase plan mentions it.

**Fix:** Add a tracker reference (e.g., `# TODO(phase-3 or v1.5): see ROADMAP.md item N`).

### IN-04: `routes_import.py` and `routes_backfill.py` both use `_log = structlog.get_logger()` at module level

**File:** `src/finance_bro/api/routes_import.py:23`, `src/finance_bro/api/routes_backfill.py:23`
**Issue:** Module-level `_log = structlog.get_logger()` is fine, but the leading underscore signals "private" — which it isn't, since it's the module's primary log handle and clones may want to bind context. Convention in the rest of the codebase (`scheduler/runner.py:45`, `routes_status.py:30`) is `_log = structlog.get_logger()`, so this is consistent. No defect; flagging only because the name `log` (without underscore) is more idiomatic for module-level loggers.

**Fix:** Optional rename; consistency wins, leave as-is.

---

_Reviewed: 2026-05-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
