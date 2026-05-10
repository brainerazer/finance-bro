---
phase: 02-reliable-sync
plan: 04
type: execute
wave: 4
depends_on: ["02-01", "02-03"]
files_modified:
  - src/finance_bro/api/schemas.py
  - src/finance_bro/api/routes_status.py
  - src/finance_bro/api/routes_backfill.py
  - src/finance_bro/api/routes_import.py
  - src/finance_bro/main.py
  - tests/test_import_status_shape.py
  - tests/test_force_poll_endpoint.py
  - tests/test_import_route.py
autonomous: true
requirements:
  - ING-08
  - ING-05
  - ING-06
tags: [phase-02, status-surface, force-poll, backfill-route, d-16-reshape]

must_haves:
  truths:
    - "GET /api/import/status returns the full D-14 shape: scheduler{state,since,last_error}, accounts[]{account_id,source_account_id,mono_type,last_polled_at,last_poll_inserted,last_poll_updated,last_status,last_error}, backfill{state,runs_remaining,runs_total,eta_seconds}."
    - "Status response distinguishes scheduler.state='running' (normal), 'auth_failed' (401 sticky banner), 'stopped' (lifespan disabled scheduler)."
    - "Status response surfaces last successful live run per account via last_polled_at + last_poll_inserted."
    - "Status response surfaces 429 distinctly: scheduler.state stays 'running' but the affected account's last_status='rate_limited' or last_status='error' (with 429 in last_error)."
    - "Status response includes ALL mono.card accounts (cards filtered out by allowlist still appear with mono_type so the user can see why they're not polled — Pitfall 10)."
    - "POST /api/import returns 202 Accepted with {enqueued: [{account_id, run_id}, ...]} per D-16; the scheduler tick picks them up on the next 10s slot."
    - "POST /api/backfill returns 202 Accepted with {run_ids: [...]}; default body backfills all active cards 12 months."
    - "Existing tests/test_import_route.py is updated to assert the 202+enqueued shape (Phase 1's synchronous body assertions are GONE)."
  artifacts:
    - path: "src/finance_bro/api/routes_status.py"
      provides: "GET /api/import/status — single read endpoint joining accounts × import_runs × scheduler_state"
      exports: ["router"]
    - path: "src/finance_bro/api/routes_backfill.py"
      provides: "POST /api/backfill — debug/operator endpoint enqueuing 12 chunks per active card (or specific account)"
      exports: ["router"]
    - path: "src/finance_bro/api/routes_import.py"
      provides: "POST /api/import — 202 enqueued shape (D-16) — REPLACED Phase 1 synchronous body"
      contains: "HTTP_202_ACCEPTED"
    - path: "src/finance_bro/api/schemas.py"
      provides: "ImportEnqueuedOut, ImportEnqueueRowOut, ImportStatusOut, SchedulerStatusOut, AccountStatusOut, BackfillStatusOut, BackfillEnqueueIn, BackfillEnqueueOut"
      exports: ["ImportEnqueuedOut", "ImportStatusOut", "BackfillEnqueueIn", "BackfillEnqueueOut"]
  key_links:
    - from: "src/finance_bro/api/routes_status.py"
      to: "STATUS_QUERY (raw SQL CTE from RESEARCH.md Code Examples §4)"
      via: "session.execute(text(STATUS_QUERY)) + SchedulerStateRepo.read()"
      pattern: "DISTINCT ON .account_id."
    - from: "src/finance_bro/api/routes_import.py"
      to: "src/finance_bro/scheduler/runner.py::enqueue_live_for_all_active_cards"
      via: "Depends(get_scheduler_runner) → runner.enqueue_live_for_all_active_cards()"
      pattern: "enqueue_live_for_all_active_cards"
    - from: "src/finance_bro/api/routes_backfill.py"
      to: "src/finance_bro/scheduler/runner.py::enqueue_backfill"
      via: "Depends(get_scheduler_runner) → runner.enqueue_backfill(account_id, months)"
      pattern: "enqueue_backfill"
    - from: "src/finance_bro/main.py"
      to: "routes_status.router and routes_backfill.router"
      via: "app.include_router(routes_status.router); app.include_router(routes_backfill.router)"
      pattern: "include_router.routes_status"
---

<objective>
Surface the engine. After this plan, Bohdan can hit `GET /api/import/status` and see one JSON document that answers "is the scheduler running, when did each card last poll, what was the last error per card, is a backfill in progress, and is my token still good?" The reshaped `POST /api/import` (D-16) and the new `POST /api/backfill` (D-07) round out the API surface so Phase 6's UI has everything it needs.

This plan also performs the D-16 BREAKING change to `routes_import.py` and the corresponding update to `tests/test_import_route.py`. Phase 1's synchronous body shape is gone — the route now enqueues `import_runs` rows and returns `202 Accepted` with the run ids; the actual fetch happens on the next scheduler tick.

Purpose: deliver the rest of ING-08 (status surface), the D-16 reshape, and the debug-endpoint backfill trigger. After this plan, Phase 2 success criteria are all observable through the API.
Output: 3 new src files (`routes_status.py`, `routes_backfill.py`, the schemas additions in `schemas.py`), 1 substantially modified src file (`routes_import.py`), 1 minimally modified src file (`main.py` mounting the new routers), 2 new test files, 1 modified test file, all green under `uv run pytest -x`.
</objective>

<phase_goal>
Bohdan stops clicking import. The app polls Mono on its own at the rate-limit budget, ingests holds correctly (and updates them in place when they clear), can backfill 12 months on first connect, and surfaces "last poll N min ago" plus 401/429 distinctly so silent failures are impossible.
</phase_goal>

<plan_scope>
**Delivers:**

1. `src/finance_bro/api/schemas.py` — add status + enqueue + backfill schemas:
   - `SchedulerStatusOut(state: str, since: datetime, last_error: str | None)`
   - `AccountStatusOut(account_id: int, source_account_id: str, mono_type: str | None, last_polled_at: datetime | None, last_poll_inserted: int | None, last_poll_updated: int, last_poll_statement_count: int | None, last_status: str | None, last_error: str | None, backfill_remaining: int, backfill_total: int)`
   - `BackfillStatusOut(state: str, runs_remaining: int, runs_total: int, eta_seconds: int | None)`
   - `ImportStatusOut(scheduler: SchedulerStatusOut, accounts: list[AccountStatusOut], backfill: BackfillStatusOut)`
   - `ImportEnqueueRowOut(account_id: int, run_id: int)`
   - `ImportEnqueuedOut(enqueued: list[ImportEnqueueRowOut])`
   - `BackfillEnqueueIn(account_id: int | None = None, months: int = 12)`
   - `BackfillEnqueueOut(run_ids: list[int])`
   - **Keep** `ImportResultOut` (Phase 1) — `ImportService.run_one_card` still returns it; it is no longer used by `routes_import.py`. Discretion: not removed because (a) it may be useful for future debug endpoints and (b) removal expands scope unnecessarily. Document the keep.

2. `src/finance_bro/api/routes_status.py` (NEW) per PATTERNS.md lines 386-418.
   - `GET /api/import/status` returning `ImportStatusOut`.
   - Uses RESEARCH.md Code Examples §4 (lines 911-952) STATUS_QUERY CTE verbatim, **with one addition: surface `0 AS last_poll_updated` in the SELECT** so the route hands a fully D-14-shaped row to `AccountStatusOut`.
   - Includes `last_poll_updated` per RESEARCH.md D-14 even though the underlying column is `inserted` (the DB stores inserted+updated together in `import_runs.inserted`; for v1 we surface `last_poll_inserted = inserted+updated` and `last_poll_updated = 0` always — note this in code; v1.5 may add a separate `updated_in_place` column to `import_runs`). **Discretion:** keep it simple — the status surface is informational, not a correctness boundary (Pitfall 2 same call-out). Surface `last_poll_updated` as a `0` constant column in STATUS_QUERY and a typed `int = 0` field on `AccountStatusOut`, with an inline TODO comment noting the v1.5 split.

3. `src/finance_bro/api/routes_backfill.py` (NEW) per PATTERNS.md lines 422-459.
   - `POST /api/backfill` accepting `BackfillEnqueueIn` body (defaults `account_id=None, months=12`).
   - Returns `202 Accepted` with `BackfillEnqueueOut(run_ids=...)`.
   - Calls `runner.enqueue_backfill(account_id, months)` via `Depends(get_scheduler_runner)`.

4. `src/finance_bro/api/routes_import.py` (RESHAPED — D-16 BREAKING):
   - `POST /api/import` returns `ImportEnqueuedOut` with `status_code=status.HTTP_202_ACCEPTED`.
   - Calls `runner.enqueue_live_for_all_active_cards()` via `Depends(get_scheduler_runner)`.
   - **Removes** the Phase 1 body that called `ImportService.run_one_card`. The `ImportService` class itself stays alive (Phase 2 keeps it for `run_one_card`'s lazy-discovery use; if it becomes orphaned by the reshape, leave it for v1.5 cleanup).
   - **Removes** the `NoCardAccountFound` 409 path — Phase 2 enqueues even if zero cards exist (returns `{enqueued: []}`); discovery now lives in the runner's `_ensure_accounts_discovered`.
   - **Preserves** the structlog `import.start` / `import.done` logging idiom; just changes the log keys to match the new shape (`enqueued_count` instead of `inserted`/`skipped_duplicates`).

5. `src/finance_bro/main.py` — mount the two new routers:
   ```python
   app.include_router(routes_status.router)
   app.include_router(routes_backfill.router)
   ```
   Add to imports.

6. `tests/test_import_status_shape.py` (NEW) — covers ING-08 + SC#4 + D-14:
   - `test_status_response_shape` — full D-14 schema validation.
   - `test_last_polled_at_per_account` — seed live runs; assert per-account last_polled_at matches.
   - `test_401_vs_429_distinguished` — seed `scheduler_state.state='auth_failed'` AND an `import_runs` row with `last_error` mentioning 429; assert the response distinguishes them (scheduler.state='auth_failed' for the global state; the per-account `last_error` carries 429).

7. `tests/test_force_poll_endpoint.py` (NEW) — covers D-16:
   - `test_returns_202_enqueued` — POST /api/import; assert 202 status, response body shape `{enqueued: [{account_id, run_id}, ...]}`.
   - `test_enqueues_one_row_per_active_card` — seed 3 active cards + 1 eAid; assert exactly 3 rows enqueued (eAid skipped).
   - `test_enqueues_zero_when_no_cards` — empty accounts table; assert 202 + `{enqueued: []}`.

8. `tests/test_import_route.py` (MODIFIED) — D-16 reshape:
   - Remove all assertions on `body["statement_count"]`, `body["inserted"]`, `body["skipped_duplicates"]`, `body["polled_account_id"]`.
   - Replace with `assert r.status_code == 202`, `assert "enqueued" in body`, `assert isinstance(body["enqueued"], list)`.
   - Optional: drive a tick afterward (instantiate a runner via `app.state.runner`, call `runner.tick()`) to verify the row flows through. This makes the test an end-to-end exercise.
   - The respx mocks for `/personal/client-info` and `/personal/statement/...` likely STAY (the runner's discovery + fetch paths still hit them when the test drives a tick). If the test does NOT drive the tick, the respx mocks for `/personal/statement/...` are unused; remove them or keep them benign.

**Does NOT deliver (in this plan):**
- A frontend status banner — that's Phase 6 (UI-01).
- Pruning of old `import_runs` rows — that's Phase 7's operational closure (Pitfall 5 mitigation is the index, not deletion).
- Any new schema columns (e.g. `import_runs.updated_in_place`) — TODO for v1.5.

**Why this slice is end-to-end testable on its own:** all three new endpoints can be exercised via `client` fixture (respx-mocked importer where needed). The status JSON is read-only; the force-poll + backfill endpoints are write-only producers (the runner consumes them). Tests assert the right SQL state was created.
</plan_scope>

<plan_dependencies>
- **Hard depends on:**
  - `02-01-schema-repos-PLAN.md` (Wave 1) — uses `import_runs`, `scheduler_state`, `accounts.mono_type`. Imports `ImportRunRepo`, `SchedulerStateRepo`.
  - `02-03-scheduler-backfill-PLAN.md` (Wave 3) — calls `runner.enqueue_live_for_all_active_cards()` and `runner.enqueue_backfill()`; the `get_scheduler_runner` dependency was added by 02-03's deps.py change. Without 02-03, the runner doesn't exist on `app.state.runner`.
- **Independent of:** `02-02-hold-aware-upsert-PLAN.md` schema changes (this plan reads `transactions.hold` indirectly only via the existing transactions route which is already extended; this plan does not exercise the hold→cleared upsert directly).
- **Wave 4:** This is the final plan in Phase 2. After landing, all Phase 2 success criteria are verifiable via the API.
</plan_dependencies>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/REQUIREMENTS.md
@.planning/ROADMAP.md
@.planning/phases/02-reliable-sync/02-CONTEXT.md
@.planning/phases/02-reliable-sync/02-RESEARCH.md
@.planning/phases/02-reliable-sync/02-VALIDATION.md
@.planning/phases/02-reliable-sync/02-PATTERNS.md
@.planning/phases/02-reliable-sync/02-01-schema-repos-PLAN.md
@.planning/phases/02-reliable-sync/02-02-hold-aware-upsert-PLAN.md
@.planning/phases/02-reliable-sync/02-03-scheduler-backfill-PLAN.md
@CLAUDE.md
@src/finance_bro/api/routes_import.py
@src/finance_bro/api/routes_accounts.py
@src/finance_bro/api/routes_health.py
@src/finance_bro/api/routes_transactions.py
@src/finance_bro/api/schemas.py
@src/finance_bro/api/deps.py
@src/finance_bro/main.py
@tests/test_import_route.py
@tests/test_transactions_route.py

<interfaces>
<!-- Key types/contracts the executor will consume. Extracted from upstream plans. -->

From Plan 02-03 (`src/finance_bro/scheduler/runner.py`):
```python
class SchedulerRunner:
    async def enqueue_backfill(self, account_id: int | None = None, months: int = 12) -> list[int]: ...
    async def enqueue_live_for_all_active_cards(self) -> list[tuple[int, int]]: ...
    # ... and tick(), recover_in_flight(), read_state(), aclose()
```

From Plan 02-03 (`src/finance_bro/api/deps.py`):
```python
def get_scheduler_runner(request: Request) -> SchedulerRunner: ...
```

From Plan 02-01 (`src/finance_bro/db/scheduler_state_repo.py`):
```python
class SchedulerStateRepo:
    async def read(self) -> tuple[str, str | None, datetime] | None: ...
```

From RESEARCH.md Code Examples §4 (lines 911-952) — verbatim STATUS_QUERY for the status route. Copy the SQL exactly, including the WITH last_live / backfill_pending / backfill_total CTEs.

From PATTERNS.md lines 386-418 — `routes_status.py` shape. Mirror `routes_accounts.py` (read it first to confirm shape).

From PATTERNS.md lines 422-459 — `routes_backfill.py` shape. Returns 202; uses Depends(get_scheduler_runner).

From PATTERNS.md lines 462-475 — `routes_import.py` reshape. Returns 202; uses Depends(get_scheduler_runner).
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Schemas + status route + force-poll reshape (POST /api/import D-16)</name>
  <files>
    src/finance_bro/api/schemas.py,
    src/finance_bro/api/routes_status.py,
    src/finance_bro/api/routes_import.py,
    src/finance_bro/main.py,
    tests/test_import_route.py
  </files>
  <action>
**1) Extend `src/finance_bro/api/schemas.py`** — additive only.

Add to imports if missing: `from datetime import datetime` (already present).

Append the new schemas AFTER the existing `ImportResultOut`:

```python
# ----- Phase 2 (D-14) — Status surface -----

class SchedulerStatusOut(BaseModel):
    state: str = Field(description="One of: running, auth_failed, stopped")
    since: datetime
    last_error: str | None = None


class AccountStatusOut(BaseModel):
    account_id: int
    source_account_id: str
    mono_type: str | None = None
    last_polled_at: datetime | None = None
    last_poll_inserted: int | None = None
    last_poll_updated: int = 0  # v1: always 0; DB stores inserted+updated combined in import_runs.inserted (deferred v1.5 split — D-14)
    last_poll_statement_count: int | None = None
    last_status: str | None = None
    last_error: str | None = None
    backfill_remaining: int = 0
    backfill_total: int = 0


class BackfillStatusOut(BaseModel):
    state: str = Field(description="One of: idle, running")
    runs_remaining: int
    runs_total: int
    eta_seconds: int | None = None


class ImportStatusOut(BaseModel):
    scheduler: SchedulerStatusOut
    accounts: list[AccountStatusOut]
    backfill: BackfillStatusOut


# ----- Phase 2 (D-16) — Force-poll enqueue -----

class ImportEnqueueRowOut(BaseModel):
    account_id: int
    run_id: int


class ImportEnqueuedOut(BaseModel):
    enqueued: list[ImportEnqueueRowOut]


# ----- Phase 2 (D-07) — Backfill enqueue -----

class BackfillEnqueueIn(BaseModel):
    account_id: int | None = None
    months: int = Field(default=12, ge=1, le=36)


class BackfillEnqueueOut(BaseModel):
    run_ids: list[int]
```

**Style note (from `schemas.py` module docstring):** integer minor units, ISO-4217 alpha currencies, no float. The new schemas use `int` / `datetime` / `str` only — no money fields.

**2) Create `src/finance_bro/api/routes_status.py`** per PATTERNS.md lines 386-418.

```python
"""Status endpoint — scheduler + per-account + backfill state (D-14).

Single read-only join over accounts × import_runs × scheduler_state.
Cheap to compute; no caching needed in v1 (Pitfall 5 index makes the join O(log n))."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import (
    AccountStatusOut,
    BackfillStatusOut,
    ImportStatusOut,
    SchedulerStatusOut,
)
from finance_bro.db.scheduler_state_repo import SchedulerStateRepo

router = APIRouter()
_log = structlog.get_logger()


# RESEARCH.md Code Examples §4 — verbatim CTE
STATUS_QUERY = text(
    """
    WITH last_live AS (
        SELECT DISTINCT ON (account_id)
               account_id,
               completed_at,
               status,
               last_error,
               inserted,
               statement_count
          FROM import_runs
         WHERE run_kind = 'live'
         ORDER BY account_id, completed_at DESC NULLS LAST
    ),
    backfill_pending AS (
        SELECT account_id, count(*) AS remaining
          FROM import_runs
         WHERE run_kind = 'backfill' AND status IN ('pending','in_flight')
         GROUP BY account_id
    ),
    backfill_total AS (
        SELECT account_id, count(*) AS total
          FROM import_runs
         WHERE run_kind = 'backfill'
         GROUP BY account_id
    )
    SELECT a.id            AS account_id,
           a.source_account_id,
           a.mono_type,
           ll.completed_at  AS last_polled_at,
           ll.inserted      AS last_poll_inserted,
           0                AS last_poll_updated,         -- D-14: v1 always 0; v1.5 may add a separate `updated_in_place` column to import_runs
           ll.statement_count AS last_poll_statement_count,
           ll.status        AS last_status,
           ll.last_error,
           coalesce(bp.remaining, 0) AS backfill_remaining,
           coalesce(bt.total, 0)     AS backfill_total
      FROM accounts a
      LEFT JOIN last_live        ll ON ll.account_id = a.id
      LEFT JOIN backfill_pending bp ON bp.account_id = a.id
      LEFT JOIN backfill_total   bt ON bt.account_id = a.id
     WHERE a.source_kind = 'mono.card'
     ORDER BY a.id ASC
    """
)


@router.get("/api/import/status", response_model=ImportStatusOut)
async def import_status(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ImportStatusOut:
    # Scheduler state singleton
    sched_row = await SchedulerStateRepo(session).read()
    if sched_row is None:
        # Defensive: should never happen post-migration; treat as 'running'
        sched = SchedulerStatusOut(state="running", since=datetime.now(UTC), last_error=None)
    else:
        state, last_err, since = sched_row
        sched = SchedulerStatusOut(state=state, since=since, last_error=last_err)

    # Per-account snapshot
    rows = (await session.execute(STATUS_QUERY)).mappings().all()
    accounts: list[AccountStatusOut] = []
    backfill_remaining_total = 0
    backfill_total_total = 0
    for r in rows:
        accounts.append(
            AccountStatusOut(
                account_id=r["account_id"],
                source_account_id=r["source_account_id"],
                mono_type=r["mono_type"],
                last_polled_at=r["last_polled_at"],
                last_poll_inserted=r["last_poll_inserted"],
                last_poll_updated=r["last_poll_updated"],
                last_poll_statement_count=r["last_poll_statement_count"],
                last_status=r["last_status"],
                last_error=r["last_error"],
                backfill_remaining=r["backfill_remaining"],
                backfill_total=r["backfill_total"],
            )
        )
        backfill_remaining_total += r["backfill_remaining"]
        backfill_total_total += r["backfill_total"]

    backfill = BackfillStatusOut(
        state="running" if backfill_remaining_total > 0 else "idle",
        runs_remaining=backfill_remaining_total,
        runs_total=backfill_total_total,
        eta_seconds=None,   # v1.5: estimate from rate-limit budget × remaining
    )

    _log.info(
        "import.status.read",
        scheduler_state=sched.state,
        active_accounts=len(accounts),
        backfill_remaining=backfill_remaining_total,
    )
    return ImportStatusOut(scheduler=sched, accounts=accounts, backfill=backfill)
```

Add `from datetime import UTC, datetime` to imports.

**3) Reshape `src/finance_bro/api/routes_import.py`** per PATTERNS.md lines 462-475 + D-16.

Read the current 45-line file. Replace its body. New version:

```python
"""Force-poll endpoint — D-16 reshape.

POST /api/import enqueues a live-poll import_runs row for every active card
(D-01 allowlist) and returns 202 Accepted. The scheduler tick (≤10s away)
picks up the rows and routes them through the rate-limit gate (≤65s further
if the bucket is held).

Phase 1's synchronous body shape (statement_count / inserted / skipped_duplicates)
is GONE — the manual button is now an async hint, not a synchronous fetch."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status

from finance_bro.api.deps import get_scheduler_runner
from finance_bro.api.schemas import ImportEnqueuedOut, ImportEnqueueRowOut
from finance_bro.scheduler.runner import SchedulerRunner

router = APIRouter()
_log = structlog.get_logger()


@router.post(
    "/api/import",
    response_model=ImportEnqueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_import(
    runner: Annotated[SchedulerRunner, Depends(get_scheduler_runner)],
) -> ImportEnqueuedOut:
    _log.info("import.start")
    pairs = await runner.enqueue_live_for_all_active_cards()
    enqueued = [ImportEnqueueRowOut(account_id=aid, run_id=rid) for (aid, rid) in pairs]
    _log.info("import.done", enqueued_count=len(enqueued))
    return ImportEnqueuedOut(enqueued=enqueued)
```

**Removed (D-16):**
- `from finance_bro.api.deps import get_import_service` — no longer used.
- `from finance_bro.api.schemas import ImportResultOut` — no longer used by this route (still defined in schemas.py).
- `from finance_bro.services.import_service import ImportService, NoCardAccountFound`.
- The `try/except NoCardAccountFound` 409 branch — the runner's enqueue path returns an empty list when there are no cards, no exception.
- The `polled_account_id` / `statement_count` / `inserted` / `skipped_duplicates` log keys — replaced by `enqueued_count`.

**4) Mount the new router in `src/finance_bro/main.py`:**

Add imports:
```python
from finance_bro.api import (
    routes_accounts,
    routes_backfill,   # NEW (created in Task 2)
    routes_health,
    routes_import,
    routes_status,     # NEW
    routes_transactions,
)
```

Mount AFTER the existing 4 routers:
```python
app.include_router(routes_status.router)
app.include_router(routes_backfill.router)
```

Note: `routes_backfill.router` doesn't exist yet at this exact moment (it lands in Task 2). Either:
- (a) Add the import + mount in this task and let Task 2 create the file (Task 2's verify will fail until the file exists; the verify command in this task should NOT import main yet to avoid the race).
- (b) Add the import + mount only in Task 2.

**Recommendation: option (b)** — keep imports atomic to the task that creates the imported module. Move the `routes_backfill` import + mount to Task 2.

**5) Modify `tests/test_import_route.py`** — D-16 reshape.

Read the existing file first. Identify each assertion:
- `assert r.status_code == 200` → change to `assert r.status_code == 202`
- `assert body["statement_count"] == ...` → REMOVE
- `assert body["inserted"] == ...` → REMOVE
- `assert body["skipped_duplicates"] == ...` → REMOVE
- `assert body["polled_account_id"] == ...` → REMOVE
- ADD: `assert "enqueued" in body and isinstance(body["enqueued"], list)`

If the test originally seeded an account + statement and asserted N rows landed in the DB, that side-effect assertion no longer holds at the moment of the POST (the rows are enqueued, not fetched). Two options:
- **Option A (preferred for keep-it-simple):** rewrite the test to assert ONLY the 202+enqueued shape. The end-to-end "tick fetches and inserts" path is already tested in `test_scheduler_round_robin.py::test_three_cards_visited_three_ticks`. Don't double-test.
- **Option B (more thorough):** after POST, drive a tick: `runner = client.app.state.runner; await runner.tick()` and then assert `import_runs.status='done'` for the enqueued ids. Requires `respx` mocks for `/personal/client-info` (discovery — runner triggers if accounts is empty) and `/personal/statement/...` (the actual fetch).

**Choose Option A.** Simpler, faster, and the runner test already covers the consumed-by-tick path. Update the test to:

```python
@pytest.mark.asyncio
async def test_first_import_enqueues_returns_202(client, session_factory):
    """D-16: POST /api/import returns 202 + {enqueued: [...]} after enqueueing live rows
    for every active card. Phase 1's synchronous body is gone."""
    # Seed an active card so the runner has something to enqueue for.
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
            VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.commit()

    r = await client.post("/api/import")
    assert r.status_code == 202, r.text
    body = r.json()
    assert "enqueued" in body
    assert len(body["enqueued"]) == 1
    assert body["enqueued"][0]["account_id"] == 1
    assert isinstance(body["enqueued"][0]["run_id"], int)
```

Remove any respx setup that mocked `/personal/statement/...` for the synchronous-fetch path. If the test had a respx setup for `/personal/client-info` (discovery), keep it ONLY if the test seeds zero accounts (forcing discovery via the runner's _ensure_accounts_discovered); since this test seeds an account, discovery is skipped and respx is unnecessary.

**Do NOT delete the file** — VALIDATION.md notes test_force_poll_endpoint.py is the dedicated D-16 test (Task 2 creates it). The modified test_import_route.py exists to ensure Phase 1's existing test name continues to test something useful. Both tests can coexist with overlapping coverage.
  </action>
  <verify>
    <automated>uv run pytest tests/test_import_route.py -x &amp;&amp; uv run python -c "from finance_bro.api.schemas import ImportStatusOut, ImportEnqueuedOut, BackfillEnqueueIn, BackfillEnqueueOut, AccountStatusOut; assert 'last_poll_updated' in AccountStatusOut.model_fields; print('schemas ok')" &amp;&amp; uv run python -c "from finance_bro.api.routes_status import router as r1; print('status route ok')" &amp;&amp; grep -q "last_poll_updated" src/finance_bro/api/routes_status.py &amp;&amp; grep -q "last_poll_updated" src/finance_bro/api/schemas.py &amp;&amp; grep -q "HTTP_202_ACCEPTED" src/finance_bro/api/routes_import.py &amp;&amp; ! grep -q "ImportResultOut" src/finance_bro/api/routes_import.py &amp;&amp; ! grep -q "NoCardAccountFound" src/finance_bro/api/routes_import.py &amp;&amp; grep -q "enqueue_live_for_all_active_cards" src/finance_bro/api/routes_import.py</automated>
  </verify>
  <done>schemas.py has the 8 new Pydantic models; routes_status.py exists and mounts a single GET /api/import/status that joins per RESEARCH.md Code Examples §4; routes_import.py is fully reshaped (no ImportResultOut, no NoCardAccountFound, no run_one_card call) — returns 202 + ImportEnqueuedOut; tests/test_import_route.py asserts the 202+enqueued shape; full suite green so far in this plan's task scope.</done>
</task>

<task type="auto">
  <name>Task 2: Backfill route + status/force-poll/backfill tests + main.py router mount</name>
  <files>
    src/finance_bro/api/routes_backfill.py,
    src/finance_bro/main.py,
    tests/test_import_status_shape.py,
    tests/test_force_poll_endpoint.py
  </files>
  <action>
**1) Create `src/finance_bro/api/routes_backfill.py`** per PATTERNS.md lines 422-459.

```python
"""Backfill endpoint — debug/operator (D-07).

POST /api/backfill enqueues 12 backfill chunks per active card by default.
Returns 202 Accepted with {run_ids: [...]} immediately; the actual fetches
happen on subsequent scheduler ticks (no HTTP socket held)."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status

from finance_bro.api.deps import get_scheduler_runner
from finance_bro.api.schemas import BackfillEnqueueIn, BackfillEnqueueOut
from finance_bro.scheduler.runner import SchedulerRunner

router = APIRouter()
_log = structlog.get_logger()


@router.post(
    "/api/backfill",
    response_model=BackfillEnqueueOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_backfill(
    body: BackfillEnqueueIn,
    runner: Annotated[SchedulerRunner, Depends(get_scheduler_runner)],
) -> BackfillEnqueueOut:
    _log.info("backfill.enqueue.start", account_id=body.account_id, months=body.months)
    run_ids = await runner.enqueue_backfill(account_id=body.account_id, months=body.months)
    _log.info("backfill.enqueue.done", run_count=len(run_ids))
    return BackfillEnqueueOut(run_ids=run_ids)
```

**2) Mount routes_status + routes_backfill in `src/finance_bro/main.py`:**

Read the current main.py (already extended in Plan 02-03 with the lifespan changes). Add the two imports:

```python
from finance_bro.api import (
    routes_accounts,
    routes_backfill,   # NEW (this plan)
    routes_health,
    routes_import,
    routes_status,     # NEW (this plan)
    routes_transactions,
)
```

After the existing 4 `app.include_router(...)` lines, add:

```python
app.include_router(routes_status.router)
app.include_router(routes_backfill.router)
```

**3) Create `tests/test_import_status_shape.py`** — covers ING-08 + SC#4 + D-14 per VALIDATION.md.

Use Archetype C from PATTERNS.md (HTTP route test via `client` fixture).

```python
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_status_response_shape(client, session_factory):
    """ING-08 D-14: GET /api/import/status returns the full schema with all keys present.

    Seed 2 cards (1 black, 1 eAid) + a completed live run for the black card +
    a couple of pending backfill rows for it. Assert the JSON has the full
    nested shape and the values are sensible.
    """
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
              (2, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid')
        """))
        # One completed live run for the black card
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted)
            VALUES (1, 'live', now()-interval '1 hour', now()-interval '5 minutes', 'done', now()-interval '5 minutes', 7, 7)
        """))
        # 3 pending backfill rows for the black card
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status)
            VALUES (1, 'backfill', now()-interval '60 days', now()-interval '30 days', 'pending'),
                   (1, 'backfill', now()-interval '90 days', now()-interval '60 days', 'pending'),
                   (1, 'backfill', now()-interval '120 days', now()-interval '90 days', 'pending')
        """))
        await s.commit()

    r = await client.get("/api/import/status")
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level shape
    assert set(body.keys()) >= {"scheduler", "accounts", "backfill"}

    # scheduler section (D-14)
    assert body["scheduler"]["state"] == "running"
    assert "since" in body["scheduler"]
    assert body["scheduler"]["last_error"] is None

    # accounts section: BOTH cards present (Pitfall 10 — eAid visible with mono_type='eAid')
    assert len(body["accounts"]) == 2
    by_aid = {a["account_id"]: a for a in body["accounts"]}
    assert by_aid[1]["mono_type"] == "black"
    assert by_aid[1]["last_polled_at"] is not None
    assert by_aid[1]["last_poll_inserted"] == 7
    assert by_aid[1]["last_poll_statement_count"] == 7
    assert by_aid[1]["last_status"] == "done"
    assert by_aid[1]["backfill_remaining"] == 3
    assert by_aid[1]["backfill_total"] == 3
    assert by_aid[2]["mono_type"] == "eAid"
    assert by_aid[2]["last_polled_at"] is None   # never polled (eAid skipped by allowlist)

    # backfill section
    assert body["backfill"]["state"] == "running"   # 3 pending
    assert body["backfill"]["runs_remaining"] == 3
    assert body["backfill"]["runs_total"] == 3


@pytest.mark.asyncio
async def test_last_polled_at_per_account(client, session_factory):
    """ING-08: each account's last_polled_at reflects its most recent live run."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
              (2, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum')
        """))
        # Card 1: two completed runs; the more recent should appear in status
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted) VALUES
              (1, 'live', now()-interval '2 hours', now()-interval '90 minutes', 'done', now()-interval '90 minutes', 1, 1),
              (1, 'live', now()-interval '1 hour',  now()-interval '5 minutes',  'done', now()-interval '5 minutes',  3, 3)
        """))
        # Card 2: one completed run
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted)
            VALUES (2, 'live', now()-interval '30 minutes', now()-interval '20 minutes', 'done', now()-interval '20 minutes', 0, 0)
        """))
        await s.commit()

    r = await client.get("/api/import/status")
    assert r.status_code == 200
    by_aid = {a["account_id"]: a for a in r.json()["accounts"]}
    # Card 1's last_polled_at is the more recent (5 minutes ago, with inserted=3, not 1)
    assert by_aid[1]["last_poll_inserted"] == 3
    # Card 2's run is independent
    assert by_aid[2]["last_poll_inserted"] == 0


@pytest.mark.asyncio
async def test_401_vs_429_distinguished(client, session_factory):
    """SC#4: scheduler.state='auth_failed' (401 banner) is distinct from a per-account 429.

    Seed scheduler_state to auth_failed; seed a live run with last_error containing 429.
    Assert response distinguishes them: scheduler.state=='auth_failed' globally,
    AND the per-account row carries 429 in last_error."""
    async with session_factory() as s:
        await s.execute(text("UPDATE scheduler_state SET state='auth_failed', last_error='Mono token rejected (401)' WHERE id=1"))
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
            VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, last_error)
            VALUES (1, 'live', now()-interval '1 hour', now()-interval '5 minutes', 'error', now()-interval '5 minutes', '429 (Retry-After=60)')
        """))
        await s.commit()

    r = await client.get("/api/import/status")
    assert r.status_code == 200
    body = r.json()
    # 401 banner state at the scheduler level
    assert body["scheduler"]["state"] == "auth_failed"
    assert "401" in body["scheduler"]["last_error"]
    # 429 surfaces per-account, NOT at scheduler level
    by_aid = {a["account_id"]: a for a in body["accounts"]}
    assert "429" in by_aid[1]["last_error"]
    assert by_aid[1]["last_status"] == "error"
```

**4) Create `tests/test_force_poll_endpoint.py`** — covers D-16 per VALIDATION.md.

```python
from sqlalchemy import text
import pytest


@pytest.mark.asyncio
async def test_returns_202_enqueued(client, session_factory):
    """D-16: POST /api/import returns 202 with {enqueued: [{account_id, run_id}]}."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
              (2, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum')
        """))
        await s.commit()

    r = await client.post("/api/import")
    assert r.status_code == 202, r.text
    body = r.json()
    assert "enqueued" in body
    assert len(body["enqueued"]) == 2
    aids = {row["account_id"] for row in body["enqueued"]}
    assert aids == {1, 2}
    for row in body["enqueued"]:
        assert isinstance(row["run_id"], int)
        assert row["run_id"] > 0

    # Side-effect: import_runs has 2 pending live rows
    async with session_factory() as s:
        rows = (await s.execute(text("""
            SELECT account_id, run_kind, status FROM import_runs WHERE run_kind='live'
        """))).all()
    assert len(rows) == 2
    assert all(r.status == 'pending' for r in rows)


@pytest.mark.asyncio
async def test_enqueues_one_row_per_active_card(client, session_factory):
    """D-01 + D-16: only allowlisted cards get enqueued; eAid is skipped."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid'),
              (2, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
              (3, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum'),
              (4, 'mono.card', 'white-id', 'UAH', '{}'::jsonb, 'white')
        """))
        await s.commit()
    r = await client.post("/api/import")
    assert r.status_code == 202
    body = r.json()
    assert len(body["enqueued"]) == 3   # 4 cards minus eAid
    aids = {row["account_id"] for row in body["enqueued"]}
    assert aids == {2, 3, 4}


@pytest.mark.asyncio
async def test_enqueues_zero_when_no_cards(client, session_factory):
    """D-16: empty accounts table → 202 + {enqueued: []}, NOT a 409 (Phase 1 behavior gone)."""
    # No accounts seeded; conftest TRUNCATE already wiped them.
    r = await client.post("/api/import")
    assert r.status_code == 202
    body = r.json()
    assert body["enqueued"] == []
```

**Note on the discovery side-effect:** in `test_enqueues_zero_when_no_cards`, the runner's `_ensure_accounts_discovered` is NOT called by `enqueue_live_for_all_active_cards` (which only reads `list_pollable_cards`); discovery happens inside `tick()`. So the empty-accounts test does NOT hit Mono. **Verify by reading runner.py from Plan 02-03** — `enqueue_live_for_all_active_cards` reads accounts via `list_pollable_cards()` and inserts; it does NOT call discovery. If 02-03's runner DOES call discovery in this path (unlikely but possible during code review), this test will need a respx mock; otherwise no respx.
  </action>
  <verify>
    <automated>uv run pytest tests/test_import_status_shape.py tests/test_force_poll_endpoint.py -x &amp;&amp; uv run pytest -x &amp;&amp; uv run python -c "from finance_bro.api.routes_backfill import router; print('backfill route ok')" &amp;&amp; grep -q "include_router(routes_status.router)" src/finance_bro/main.py &amp;&amp; grep -q "include_router(routes_backfill.router)" src/finance_bro/main.py</automated>
  </verify>
  <done>routes_backfill.py exists; main.py mounts both new routers; status-shape test asserts the full D-14 schema (eAid visible, scheduler.state, per-account last_polled_at, backfill state aggregation); force-poll test asserts 202 + enqueued + side-effect rows; the 401-vs-429 distinction is testable via the JSON; full suite green; Phase 2 vertical slice complete.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Network → app | Tailscale/LAN-only (DEP-02). The new GET /api/import/status and POST /api/backfill mount at /api/* with no auth, same as Phase 1's routes. |
| HTTP body → enqueue path | `BackfillEnqueueIn` accepts `account_id?` and `months` (1..36 validated). No raw SQL exposure; pydantic validates ints. |
| Status response → user | The status JSON includes `last_error` strings sourced from `import_runs.last_error` (logged via structlog redaction at write time). |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-02-01 | Information Disclosure | Status JSON could leak Mono payload contents via last_error | mitigate | `last_error` is populated by the runner using `repr(exc)` / typed-error message strings (e.g. `"Mono 401"`, `"429 (Retry-After=60)"`, `"Mono 500"`). The runner does NOT include Mono response bodies in the error text (PATTERNS.md Pattern S3 + Phase 1 redaction guarantee). Verify by inspecting the strings written by `_mark_error` in `scheduler/runner.py` — they reference status codes only, never raw payloads. |
| T-02-12 | Denial of Service | Unauthenticated POST /api/backfill could enqueue ~144 rows in a single call (12 per card × 12 cards) | accept | DEP-02: network-gated trust boundary. `months` is bounded `1..36` (Pydantic validation). At ~360 rows/day live + bursty backfills, the index from Plan 02-01 (`ix_import_runs_status_created`) keeps the dequeue O(log n). Single-user app — no abuse vector inside the trust boundary. |
| T-02-13 | Tampering | POST /api/import with no body returning success-shape but doing nothing | accept | The behavior is intentional: when there are no allowlisted cards, the response is `{enqueued: []}` (200 — well, 202 — semantically "no work to do"). Tested by `test_enqueues_zero_when_no_cards`. Phase 1's 409 was misleading because Phase 2's discovery happens in the runner; the route now reflects steady-state truth. |
| T-02-14 | Repudiation | Status endpoint hits an empty `scheduler_state` table | mitigate | Defensive: `routes_status.py` falls back to `state='running'` if `read()` returns None. **But** Plan 02-01's migration seeds the singleton row, and the `client` fixture's TRUNCATE+reseed (Plan 02-01's conftest extension) ensures it's always there in tests. The defensive fallback covers the edge of a future migration that drops the seed. |
</threat_model>

<verification>
**Plan-level checks (run before commit/handoff):**

1. `uv run pytest -x` — full suite green.
2. `grep -c "HTTP_202_ACCEPTED" src/finance_bro/api/routes_import.py src/finance_bro/api/routes_backfill.py` returns ≥ 2 (both new routes use 202).
3. `grep -q "ImportResultOut" src/finance_bro/api/routes_import.py` should be FALSE (D-16: synchronous body shape gone from the route — schema can stay in schemas.py for back-compat).
4. `grep -q "NoCardAccountFound" src/finance_bro/api/routes_import.py` should be FALSE.
5. `grep -RE "(routes_status|routes_backfill)" src/finance_bro/main.py | wc -l` ≥ 2 (imports + mounts).
6. `grep -q "DISTINCT ON (account_id)" src/finance_bro/api/routes_status.py` — STATUS_QUERY uses the verbatim CTE.
7. **D-14 schema completeness:** `grep -E "(scheduler|accounts|backfill).*ImportStatusOut" src/finance_bro/api/schemas.py | wc -l` ≥ 3.
8. **Phase 1 regression:** `uv run pytest tests/test_health.py tests/test_no_auth.py tests/test_partial_unique_index.py tests/test_log_redaction.py tests/test_idempotency.py tests/test_money_invariants.py tests/test_schema_invariants.py tests/test_settings.py -x`.
9. **Phase 2 cumulative:** `uv run pytest tests/test_scheduler_round_robin.py tests/test_backfill_enqueue.py tests/test_backfill_resumability.py tests/test_backfill_window_math.py tests/test_hold_cleared_upsert.py tests/test_401_stops_scheduler.py tests/test_429_does_not_stop.py tests/test_import_status_shape.py tests/test_force_poll_endpoint.py tests/test_import_route.py tests/test_import_run_repo.py tests/test_scheduler_state_repo.py -x`.
</verification>

<success_criteria>
- All Tasks' `<verify>` commands pass.
- Every Phase 2 success criterion is observable through the API now:
  - SC#1 (auto-poll): tick runs autonomously; status surface shows last_polled_at advancing.
  - SC#2 (12-month resumable backfill): POST /api/backfill enqueues 12 chunks per card; status surface shows backfill_remaining decreasing.
  - SC#3 (hold→cleared in-place): the upsert from Plan 02-02 plus the runner's Plan 02-03 fetch path produces a single row that the GET /api/transactions endpoint returns with `hold` reflecting the latest payload.
  - SC#4 (401/429 distinct): GET /api/import/status renders `scheduler.state='auth_failed'` for 401, leaves it `'running'` for 429 with the per-account `last_error` carrying the 429 detail.
- ING-05, ING-06, ING-08 are deliverable.
- D-16 reshape is in place; existing test_import_route.py is updated; no regression in Phase 1 invariants.
- The four route handlers (`routes_status`, `routes_backfill`, `routes_import`, `routes_transactions`) all coexist; main.py mounts 6 routers total.
- `must_haves.truths` verifiable: each truth has a passing assertion in `test_import_status_shape.py`, `test_force_poll_endpoint.py`, or the runner-level tests from Plan 02-03.
</success_criteria>

<output>
After completion, create `.planning/phases/02-reliable-sync/02-04-SUMMARY.md` covering: the 8 new schemas, the STATUS_QUERY CTE shape, the D-16 reshape (call out which Phase 1 fields are GONE: polled_account_id, statement_count, inserted, skipped_duplicates), the new routes (status read, force-poll write, backfill write), the 401-vs-429 distinction in the status JSON, and the closing of all Phase 2 success criteria. Note any open questions remaining for empirical resolution (Mono retention horizon, 429 Retry-After shape, statementItem.id global vs per-account uniqueness — see CONTEXT.md "specifics" Open Questions 1-3 + RESEARCH.md Open Questions section).
</output>
