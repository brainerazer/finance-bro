---
phase: 02-reliable-sync
plan: 03
type: execute
wave: 3
depends_on: ["02-01", "02-02"]
files_modified:
  - src/finance_bro/scheduler/__init__.py
  - src/finance_bro/scheduler/errors.py
  - src/finance_bro/scheduler/window.py
  - src/finance_bro/scheduler/runner.py
  - src/finance_bro/importers/monobank.py
  - src/finance_bro/services/import_service.py
  - src/finance_bro/api/deps.py
  - src/finance_bro/main.py
  - tests/test_scheduler_round_robin.py
  - tests/test_backfill_enqueue.py
  - tests/test_backfill_resumability.py
  - tests/test_backfill_window_math.py
  - tests/test_401_stops_scheduler.py
  - tests/test_429_does_not_stop.py
autonomous: true
requirements:
  - ING-05
  - ING-06
  - ING-08
tags: [phase-02, scheduler, apscheduler, backfill, lifespan, typed-errors, round-robin]

must_haves:
  truths:
    - "APScheduler AsyncIOScheduler starts inside FastAPI lifespan after init_engine; tick fires every 10s with max_instances=1, coalesce=True (D-03/D-04)."
    - "Lifespan shutdown calls scheduler.shutdown(wait=False) BEFORE engine teardown (Pitfall 8)."
    - "On lifespan startup, recover_in_flight() resets stale 'in_flight' rows older than 5 minutes back to 'pending' (Pattern 7)."
    - "If scheduler_state.state == 'auth_failed', the scheduler.add_job is skipped at startup (Pitfall 4)."
    - "tick() picks oldest pending import_runs row, claim-and-execute via ImportRunRepo, calls fetch_statement, upserts via TransactionRepo.insert_many, marks done with statement_count + inserted."
    - "tick() with no pending row picks the next pollable card by oldest last-live completed_at and enqueues a single live row, returning."
    - "tick() with backfill rows pending for an account skips live polls for that account (D-06) but continues polling other accounts."
    - "MonobankImporter raises MonoAuthError on 401, MonoRateLimitError on 429 (with Retry-After if present), MonoTransientError on other 4xx/5xx; gate.acquire still runs FIRST in both discover_accounts and fetch_statement (Pattern 4 + S7)."
    - "MonobankImporter.discover_accounts emits CanonicalAccount.mono_type for cards (extracted from acc.get('type'))."
    - "MonobankImporter.fetch_statement populates CanonicalTransaction.hold/description/mcc from each statementItem."
    - "Round-robin order is by account id ASC over the allowlist (mono.card AND mono_type ∈ {black,platinum,white}); eAid is never picked."
    - "12-month backfill enqueue creates 12 import_runs rows newest-first (chunks of 30 days); resume picks the remaining unfinished chunks across a simulated restart."
    - "401 sets scheduler_state.state='auth_failed' and persists; a fresh runner instance reading state at startup observes the sticky bit."
    - "429 leaves scheduler_state alone; the import_runs row gets status='error', last_error mentions 429."
    - "4xx response inside backfill chunk → import_runs.status='error', NOT silent skip (Pitfall 3)."
  artifacts:
    - path: "src/finance_bro/scheduler/__init__.py"
      provides: "package marker"
      min_lines: 0
    - path: "src/finance_bro/scheduler/errors.py"
      provides: "MonoAuthError, MonoRateLimitError, MonoTransientError"
      exports: ["MonoAuthError", "MonoRateLimitError", "MonoTransientError"]
    - path: "src/finance_bro/scheduler/window.py"
      provides: "MONO_STATEMENT_MAX_WINDOW_SECONDS=2_682_000, MONO_STATEMENT_BACKFILL_WINDOW_DAYS=30, backfill_chunks(now, months) iterator"
      exports: ["MONO_STATEMENT_MAX_WINDOW_SECONDS", "MONO_STATEMENT_BACKFILL_WINDOW_DAYS", "backfill_chunks"]
    - path: "src/finance_bro/scheduler/runner.py"
      provides: "SchedulerRunner with tick, recover_in_flight, read_state, enqueue_backfill, enqueue_live_for_all_active_cards, aclose"
      exports: ["SchedulerRunner"]
    - path: "src/finance_bro/importers/monobank.py"
      provides: "Typed exceptions on 401/429/other; mono_type extraction; CanonicalTransaction populated with hold/description/mcc"
      contains: "MonoAuthError"
    - path: "src/finance_bro/main.py"
      provides: "Lifespan starts and stops the scheduler; mounts the runner on app.state"
      contains: "AsyncIOScheduler"
  key_links:
    - from: "src/finance_bro/main.py::lifespan"
      to: "src/finance_bro/scheduler/runner.py::SchedulerRunner"
      via: "instantiates SchedulerRunner with session_factory + importer; await runner.recover_in_flight() and runner.read_state() BEFORE scheduler.start()"
      pattern: "SchedulerRunner"
    - from: "src/finance_bro/scheduler/runner.py::tick"
      to: "src/finance_bro/db/import_run_repo.py::claim_next_pending"
      via: "await ImportRunRepo(session).claim_next_pending() inside session.begin()"
      pattern: "claim_next_pending"
    - from: "src/finance_bro/scheduler/runner.py::tick"
      to: "src/finance_bro/importers/monobank.py::fetch_statement"
      via: "await self._importer.fetch_statement(account.source_account_id, run.window_from, run.window_to)"
      pattern: "fetch_statement"
    - from: "src/finance_bro/scheduler/runner.py::tick"
      to: "src/finance_bro/db/transaction_repo.py::insert_many"
      via: "await TransactionRepo(session).insert_many(account_id, items) and unpack (inserted, updated)"
      pattern: "insert_many"
    - from: "src/finance_bro/importers/monobank.py"
      to: "src/finance_bro/scheduler/errors.py"
      via: "raises MonoAuthError/MonoRateLimitError/MonoTransientError on raise_for_status branching"
      pattern: "MonoAuthError"
    - from: "src/finance_bro/scheduler/runner.py::tick (401 handler)"
      to: "scheduler_state singleton (UPDATE id=1 SET state='auth_failed')"
      via: "SchedulerStateRepo.write('auth_failed', error_message); update self._cached_state"
      pattern: "auth_failed"
---

<objective>
Stand up the autonomous polling and backfill engine. After this plan, the FastAPI process owns an in-process APScheduler that ticks every 10s, the existing `RateLimitGate` continues to be the sole 65s budget owner, the importer raises typed exceptions on 401/429/other so the runner can branch on intent, and the `import_runs` cursor table drives both live polls and 12-month backfills with restart-resilient resumability via the `recover_in_flight` sweep.

This is the largest plan in Phase 2 — three new files, three modified files, six new test files. It is the wave where the "Bohdan stops clicking import" promise becomes true: the very first tick after `docker compose up` discovers accounts, enqueues a backfill (if first run), and starts polling.

Purpose: deliver SC#1, SC#2, parts of SC#4 (the 401/429 paths), and ING-06 end-to-end. Plan 02-04 then surfaces what this engine does via the status JSON.
Output: 4 new src files (`scheduler/__init__.py`, `errors.py`, `window.py`, `runner.py`), 4 modified src files (`monobank.py`, `import_service.py`, `deps.py`, `main.py`), 6 new test files, all green under `uv run pytest -x`, and the existing Phase 1 `test_import_route.py` continues to pass UNCHANGED in this plan (02-04 reshapes it).
</objective>

<phase_goal>
Bohdan stops clicking import. The app polls Mono on its own at the rate-limit budget, ingests holds correctly (and updates them in place when they clear), can backfill 12 months on first connect, and surfaces "last poll N min ago" plus 401/429 distinctly so silent failures are impossible.
</phase_goal>

<plan_scope>
**Delivers (this plan):**

1. New `src/finance_bro/scheduler/` package:
   - `__init__.py` — empty marker (mirrors `src/finance_bro/importers/__init__.py`).
   - `errors.py` — `MonoAuthError`, `MonoRateLimitError(retry_after_seconds)`, `MonoTransientError`. **Verbatim from RESEARCH.md Pattern 4 lines 494-507.**
   - `window.py` — `MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000`, `MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30`, `backfill_chunks(now, months=12)` iterator. **Verbatim from RESEARCH.md Pattern 6 lines 562-582.**
   - `runner.py` — `SchedulerRunner` with `tick()`, `recover_in_flight()`, `read_state()`, `enqueue_backfill(account_id, months)`, `enqueue_live_for_all_active_cards()`, `aclose()`. Tick body verbatim from RESEARCH.md Code Examples §3 lines 854-909.

2. `src/finance_bro/importers/monobank.py` (modified):
   - Wrap `resp.raise_for_status()` in `discover_accounts` and `fetch_statement` with the typed-exception branch from RESEARCH.md Pattern 4 (lines 514-525).
   - In `discover_accounts`, capture `acc.get("type")` and pass to `CanonicalAccount(... mono_type=...)`. Requires `CanonicalAccount` to gain `mono_type: str | None = None`.
   - In `fetch_statement`'s yielded `CanonicalTransaction`, populate `hold`, `description`, `mcc` from `item.get(...)`.

3. `src/finance_bro/importers/base.py` (modified):
   - `CanonicalAccount` gains `mono_type: str | None = None`.
   - `CanonicalTransaction.hold/description/mcc` were ALREADY added in Plan 02-02 — confirm they exist and use them. Do NOT re-add.

4. `src/finance_bro/services/import_service.py` (modified):
   - Phase 2 keeps Phase 1's `run_one_card` call site (Plan 02-04 may reshape the route to bypass it, but for now `routes_import.py` still calls `run_one_card` — see Plan 02-02's call-site adapter). The runner's tick uses `ImportRunRepo` + `TransactionRepo` + `MonobankImporter` directly without going through `ImportService` to avoid circular orchestration. **Discretion call (per Discretion bullet 5):** `ImportService` is left intact for the Phase 1 manual-import contract; the runner does its own session/transaction management mirroring `ImportService.run_one_card`'s `async with self._session_factory() as session, session.begin()` pattern (PATTERNS.md Pattern S2).
   - Discovery is preserved: when the runner observes `accounts` is empty (cold-boot), it calls `self._importer.discover_accounts()` and persists via `AccountRepo.upsert_many`. This is a copy-paste from `import_service.py` lines 52-62 into a runner helper `_ensure_accounts_discovered()`. The `ImportService` keeps its lazy-discovery for the Phase 1 manual path; deduplication via `uq_accounts_source` constraint makes this safe even if both paths run.

5. `src/finance_bro/api/deps.py` (modified):
   - Add `get_scheduler_runner(request: Request) -> SchedulerRunner` that returns `request.app.state.runner` (set in lifespan). Plan 02-04 consumes this for `/api/import` reshape and `/api/backfill`.
   - Do NOT add `get_scheduler()` — APScheduler instance is implementation detail (PATTERNS.md anti-pattern callout, line 532).

6. `src/finance_bro/main.py` (modified):
   - Imports: `AsyncIOScheduler`, `IntervalTrigger`, `SchedulerRunner`, `MonobankImporter`, `RateLimitGate`, `get_session_factory`.
   - Lifespan body extends per RESEARCH.md Code Examples §2 lines 819-851. Order: configure logging → init_engine → instantiate SchedulerRunner (with importer + session_factory) → recover_in_flight → read_state → if running, add_job(tick, IntervalTrigger(seconds=10), max_instances=1, coalesce=True, misfire_grace_time=30) → scheduler.start() → mount runner+scheduler on app.state → yield → finally: scheduler.shutdown(wait=False) → runner.aclose().
   - Plan 02-04 will mount additional routers (`routes_status`, `routes_backfill`); for now, leave the existing 4 routers intact.

7. New tests (Wave 0 of this plan, then implementation in tasks):
   - `tests/test_scheduler_round_robin.py` — SC#1 + D-01 + D-02. eAid skipped, allowlisted cards visited by id ASC.
   - `tests/test_backfill_enqueue.py` — D-05 + D-08. 12 chunks, newest-first, run_kind='backfill', all pending.
   - `tests/test_backfill_resumability.py` — SC#2 + ING-06 + Pitfall 7. Includes `test_recover_in_flight_on_restart`, `test_resume_picks_remaining_chunks`, `test_full_12_month_walk`, `test_4xx_marks_error_not_skip`.
   - `tests/test_backfill_window_math.py` — pure-function unit test for `backfill_chunks`.
   - `tests/test_401_stops_scheduler.py` — SC#4 + D-15 sticky-401. Includes `test_401_persists_across_restart`.
   - `tests/test_429_does_not_stop.py` — SC#4 + D-15 transient-429.

**Does NOT deliver (in this plan):**
- The status surface route (`GET /api/import/status`), the backfill route (`POST /api/backfill`), and the D-16 reshape of `POST /api/import` — those are Plan 02-04. Until 02-04 lands, the runner can be observed via direct DB inspection of `import_runs` and `scheduler_state` (which is exactly what the runner tests do).
- A new ImportRunRepo or schema change — those are Plan 02-01.
- A new upsert clause — that's Plan 02-02.

**Why this slice is end-to-end testable on its own:** the runner tests instantiate `SchedulerRunner` directly with the test `session_factory` and a respx-mocked importer. They drive `await runner.tick()` repeatedly (PATTERNS.md Archetype A + B + Pitfall 9 — never via `freezegun` on APScheduler internals). The lifespan integration test piggybacks on the existing `LifespanManager(app)` from `tests/conftest.py::client`, which now starts the scheduler — but the existing Phase 1 route tests must continue to pass, so the scheduler MUST coexist with the test environment without polling Mono. **Mitigation: the scheduler tick is a no-op in tests because no accounts exist after the conftest TRUNCATE; the tick takes the "no pending → enqueue next live → return" branch which is benign.**

**Test environment note:** the existing `tests/conftest.py::client` fixture brings up the lifespan via `LifespanManager`. After this plan, the lifespan starts the APScheduler. Two concerns:
  1. APScheduler's IntervalTrigger fires every 10s — but the tests only use the client briefly (sub-second). The first tick is unlikely to fire. **Defense:** set `coalesce=True, misfire_grace_time=30` and rely on the fact that startup→teardown happens before the first 10s tick.
  2. If a tick somehow runs: it reads `scheduler_state` (state='running' after conftest reseed), claims pending rows (none), tries to pick the next active card (none, post-truncate), enqueues nothing, returns. **No external HTTP calls.** Importer is constructed, but its `_client` makes no network calls until `.get()` is called — which never happens because the tick has no work.
  3. If you want deterministic test isolation, optionally set `APP_DISABLE_SCHEDULER=1` env var to skip `scheduler.start()` in tests. **Recommendation: do this.** Read `os.environ.get("APP_DISABLE_SCHEDULER")` in the lifespan and skip the scheduler.add_job/start path when truthy. The conftest already sets `os.environ` for `DATABASE_URL`/`MONO_TOKEN`; add `APP_DISABLE_SCHEDULER=1` next to it. The runner is still instantiated (so `app.state.runner` exists for `get_scheduler_runner`) and `recover_in_flight` still runs. Only `scheduler.start()` is skipped.
</plan_scope>

<plan_dependencies>
- **Hard depends on:**
  - `02-01-schema-repos-PLAN.md` — uses `ImportRunRepo`, `SchedulerStateRepo`, `AccountRepo.list_pollable_cards`, `accounts.mono_type`, `import_runs` and `scheduler_state` schemas. Imports `ImportRun` and `SchedulerState` ORM models.
  - `02-02-hold-aware-upsert-PLAN.md` — calls `TransactionRepo.insert_many` and unpacks the new `(inserted, updated)` tuple. Also relies on `CanonicalTransaction.hold/description/mcc` fields existing (Plan 02-02 added them with defaults; this plan starts populating them from Mono payloads).
- **Independent of:** `02-04-status-surface-PLAN.md` (it consumes the runner via `app.state.runner` but does not affect this plan's behavior).
- **Blocks:** `02-04-status-surface-PLAN.md` (the status route reads `import_runs` and `scheduler_state` populated by the runner; the D-16 reshape needs `runner.enqueue_live_for_all_active_cards()`).
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
@CLAUDE.md
@src/finance_bro/main.py
@src/finance_bro/api/deps.py
@src/finance_bro/importers/monobank.py
@src/finance_bro/importers/rate_limit.py
@src/finance_bro/importers/base.py
@src/finance_bro/services/import_service.py
@src/finance_bro/db/engine.py
@tests/conftest.py
@tests/test_importer_statement.py
@tests/test_rate_limit_gate.py
@tests/test_importer_no_token_in_url.py

<interfaces>
<!-- Key types/contracts the executor will consume. Extracted from upstream plans + Phase 1. -->

From Plan 02-01 (`src/finance_bro/db/import_run_repo.py`):
```python
class ImportRunRepo:
    def __init__(self, session: AsyncSession) -> None: ...
    async def claim_next_pending(self) -> ImportRun | None: ...
    async def enqueue_backfill(self, account_id: int, chunks: list[tuple[datetime, datetime]]) -> list[int]: ...
    async def enqueue_live(self, account_id: int, window_from: datetime, window_to: datetime) -> int: ...
    async def mark_done(self, run_id: int, statement_count: int, inserted: int, updated: int) -> None: ...
    async def mark_error(self, run_id: int, error: str) -> None: ...
    async def recover_in_flight(self, threshold_seconds: int = 300) -> int: ...
    async def count_pending_or_in_flight_backfill(self, account_id: int) -> int: ...
    async def last_live_per_account(self) -> dict[int, ImportRun]: ...
```

From Plan 02-01 (`src/finance_bro/db/scheduler_state_repo.py`):
```python
class SchedulerStateRepo:
    def __init__(self, session: AsyncSession) -> None: ...
    async def read(self) -> tuple[str, str | None, datetime] | None: ...
    async def write(self, state: str, last_error: str | None) -> None: ...
```

From Plan 02-01 (`src/finance_bro/db/account_repo.py`):
```python
class AccountRepo:
    async def list_pollable_cards(self) -> list[Account]: ...   # NEW in 02-01: filter by mono_type allowlist
    async def list_all(self) -> list[Account]: ...
    async def get_first_card(self) -> Account | None: ...   # Phase 1 — kept
    async def upsert_many(self, items: list[CanonicalAccount]) -> int: ...
```

From Plan 02-02 (`src/finance_bro/db/transaction_repo.py`):
```python
async def insert_many(self, account_id: int, items: list[CanonicalTransaction]) -> tuple[int, int]:
    """Returns (inserted, updated_in_place); on conflict mutates ONLY hold/amount_minor/raw_payload (D-10)."""
```

From Plan 02-02 (`src/finance_bro/importers/base.py`):
```python
@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str
    source_account_id: str
    occurred_at: datetime
    amount_minor: int
    currency: str
    raw: dict[str, Any]
    hold: bool = False
    description: str | None = None
    mcc: int | None = None
```

From Phase 1 (`src/finance_bro/importers/rate_limit.py`):
```python
class RateLimitGate:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...
    async def acquire(self, token: str) -> None: ...   # 65s gate; reused unchanged
```

From Phase 1 (`src/finance_bro/importers/monobank.py` current shape):
```python
class MonobankImporter:
    source_kind = "monobank"
    def __init__(self, token: str, gate: RateLimitGate) -> None: ...
    async def aclose(self) -> None: ...
    async def discover_accounts(self) -> list[CanonicalAccount]: ...
    async def fetch_statement(self, source_account_id: str, since: datetime, until: datetime) -> AsyncIterator[CanonicalTransaction]: ...
```

From Phase 1 (`src/finance_bro/main.py` current shape — extend per RESEARCH.md Code Examples §2):
```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    init_engine()
    yield
```

The verbatim runner tick body is RESEARCH.md Code Examples §3 (lines 854-909).
The verbatim lifespan body is RESEARCH.md Code Examples §2 (lines 819-851) + Pattern 1 (lines 322-354).
The verbatim 401/429 branching is RESEARCH.md Pattern 4 (lines 514-525).
The verbatim window math is RESEARCH.md Pattern 6 (lines 562-582).
The verbatim recovery sweep is RESEARCH.md Pattern 7 (lines 596-614).
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Wave 0 — scheduler package skeleton (errors, window, __init__); MonobankImporter typed-error + mono_type extraction; backfill/window unit tests</name>
  <files>
    src/finance_bro/scheduler/__init__.py,
    src/finance_bro/scheduler/errors.py,
    src/finance_bro/scheduler/window.py,
    src/finance_bro/importers/monobank.py,
    src/finance_bro/importers/base.py,
    tests/test_backfill_window_math.py
  </files>
  <action>
**1) `src/finance_bro/scheduler/__init__.py`** — empty file. Mirrors `src/finance_bro/importers/__init__.py`.

**2) `src/finance_bro/scheduler/errors.py`** — verbatim from RESEARCH.md Pattern 4 lines 494-507. Module docstring: "Typed Mono errors at the importer boundary. The runner branches on these per D-15 (401 sticky, 429 transient, transient otherwise)."

```python
class MonoAuthError(Exception):
    """Raised when Mono returns 401. Sticky — sets scheduler_state='auth_failed' (D-15)."""


class MonoRateLimitError(Exception):
    """Raised when Mono returns 429. Transient — surfaced per-call only (D-15)."""

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Mono 429 (Retry-After={retry_after_seconds})")


class MonoTransientError(Exception):
    """Raised on 5xx / connect-timeout / other 4xx. Per-call import_runs.error;
    the next tick tries the next pending row."""
```

**3) `src/finance_bro/scheduler/window.py`** — verbatim from RESEARCH.md Pattern 6 lines 562-582 + Pitfall 5 in CONTEXT.md "specifics".

```python
"""Backfill window math. Constants:
  MONO_STATEMENT_MAX_WINDOW_SECONDS — Mono cap (31d + 1h, Pitfall 5).
  MONO_STATEMENT_BACKFILL_WINDOW_DAYS — operating chunk size (1h+ headroom).

All Mono time math in SECONDS, never milliseconds (Pitfall 5 sub-point;
Phase 1 invariant in `MonobankImporter.fetch_statement`)."""

from collections.abc import Iterator
from datetime import datetime, timedelta

MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000  # 31d + 1h — Mono cap
MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30        # 1h+ headroom inside the cap


def backfill_chunks(now: datetime, months: int = 12) -> Iterator[tuple[datetime, datetime]]:
    """Yield (window_from, window_to) tuples in newest-first order.

    For months=12, yields 12 tuples covering [now - 360d, now] in 30d slices.
    DST-blind (UTC seconds at the API boundary; Mono accepts UNIX seconds).
    """
    for n in range(months):
        window_to = now - timedelta(days=n * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        window_from = now - timedelta(days=(n + 1) * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        yield window_from, window_to
```

No new imports beyond stdlib. Module is dependency-free.

**4) `src/finance_bro/importers/base.py`** — extend `CanonicalAccount` with `mono_type: str | None = None`:

```python
@dataclass(frozen=True)
class CanonicalAccount:
    source_account_id: str
    source_kind: str
    currency: str
    raw: dict[str, Any]
    mono_type: str | None = None   # NEW (D-01 + Discretion bullet 2)
```

`CanonicalTransaction` already has `hold/description/mcc` from Plan 02-02 — DO NOT TOUCH.

**5) `src/finance_bro/importers/monobank.py`** — three changes per PATTERNS.md lines 200-218.

(a) Add imports near top:
```python
from finance_bro.scheduler.errors import MonoAuthError, MonoRateLimitError, MonoTransientError
```

(b) `discover_accounts` — wrap the existing `resp.raise_for_status()` (currently line 44) and emit `mono_type` for cards:

```python
async def discover_accounts(self) -> list[CanonicalAccount]:
    await self._gate.acquire(self._token)   # KEEP THIS FIRST — Pattern S7 invariant
    resp = await self._client.get("/personal/client-info")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise MonoAuthError("Mono token rejected (401)") from e
        if e.response.status_code == 429:
            retry = e.response.headers.get("Retry-After")
            retry_seconds = int(retry) if retry and retry.isdigit() else None
            raise MonoRateLimitError(retry_seconds) from e
        raise MonoTransientError(f"Mono {e.response.status_code}") from e
    data = resp.json()
    out: list[CanonicalAccount] = []
    for acc in data.get("accounts", []):
        kind = "mono.fop" if acc.get("type") == "fop" else "mono.card"
        mono_type = acc.get("type") if kind == "mono.card" else None
        out.append(
            CanonicalAccount(
                source_account_id=acc["id"],
                source_kind=kind,
                currency=numeric_to_alpha(acc["currencyCode"]),
                raw=acc,
                mono_type=mono_type,
            )
        )
    for jar in data.get("jars", []):
        out.append(
            CanonicalAccount(
                source_account_id=jar["id"],
                source_kind="mono.jar",
                currency=numeric_to_alpha(jar["currencyCode"]),
                raw=jar,
                mono_type=None,
            )
        )
    return out
```

(c) `fetch_statement` — same try/except wrap around `resp.raise_for_status()`, plus populate hold/description/mcc on the yielded CanonicalTransaction:

```python
async def fetch_statement(
    self,
    source_account_id: str,
    since: datetime,
    until: datetime,
) -> AsyncIterator[CanonicalTransaction]:
    await self._gate.acquire(self._token)   # KEEP THIS FIRST — Pattern S7 invariant
    from_ts = int(since.timestamp())
    to_ts = int(until.timestamp())
    resp = await self._client.get(f"/personal/statement/{source_account_id}/{from_ts}/{to_ts}")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise MonoAuthError("Mono token rejected (401)") from e
        if e.response.status_code == 429:
            retry = e.response.headers.get("Retry-After")
            retry_seconds = int(retry) if retry and retry.isdigit() else None
            raise MonoRateLimitError(retry_seconds) from e
        raise MonoTransientError(f"Mono {e.response.status_code}") from e
    for item in resp.json():
        yield CanonicalTransaction(
            source_tx_id=item["id"],
            source_account_id=source_account_id,
            occurred_at=datetime.fromtimestamp(item["time"], tz=UTC),
            amount_minor=int(item["amount"]),
            currency=numeric_to_alpha(item["currencyCode"]),
            raw=item,
            hold=item.get("hold", False),
            description=item.get("description"),
            mcc=item.get("mcc"),
        )
```

**Critical invariants to preserve (PATTERNS.md Pattern S7 + Phase 1 invariants):**
- `await self._gate.acquire(self._token)` MUST remain the FIRST line of both methods.
- The token MUST stay in `self._client.headers` only; URL paths NEVER carry the token. `tests/test_importer_no_token_in_url.py` enforces this — must remain green.
- Currency conversion via `numeric_to_alpha` unchanged.
- `int(item["amount"])` (no float at boundary) unchanged.

**6) Create `tests/test_backfill_window_math.py`** — pure-function unit test (PATTERNS.md Archetype A; no DB, no HTTP).

```python
from datetime import UTC, datetime, timedelta

import pytest

from finance_bro.scheduler.window import (
    MONO_STATEMENT_BACKFILL_WINDOW_DAYS,
    MONO_STATEMENT_MAX_WINDOW_SECONDS,
    backfill_chunks,
)


def test_constants_match_pitfall_5():
    assert MONO_STATEMENT_MAX_WINDOW_SECONDS == 2_682_000
    assert MONO_STATEMENT_BACKFILL_WINDOW_DAYS == 30


def test_twelve_chunks_newest_first():
    """ING-06 + D-09 + Pitfall 5: 12 chunks of 30d, newest-first."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    chunks = list(backfill_chunks(now, months=12))
    assert len(chunks) == 12
    # Newest-first: chunk[0].window_to is `now`; subsequent windows go backwards.
    assert chunks[0][1] == now
    assert chunks[0][0] == now - timedelta(days=30)
    # Each subsequent chunk is 30d older.
    for n in range(1, 12):
        prev_from = chunks[n-1][0]
        assert chunks[n][1] == prev_from
        assert chunks[n][0] == prev_from - timedelta(days=30)
    # Deepest chunk: 360 days back.
    assert chunks[11][0] == now - timedelta(days=360)


def test_each_chunk_within_mono_max_window():
    """Defensive: every chunk's seconds-span is below the Mono cap with headroom."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    for window_from, window_to in backfill_chunks(now, months=12):
        span_seconds = (window_to - window_from).total_seconds()
        assert span_seconds <= MONO_STATEMENT_MAX_WINDOW_SECONDS
        # 30d = 2_592_000s; cap = 2_682_000s; headroom = 90_000s = 25h. Confirms Pitfall 5 design.


def test_no_milliseconds_in_unix_conversion():
    """Pitfall 5 sub-point: never multiply by 1000. backfill_chunks yields datetimes;
    int(dt.timestamp()) gives SECONDS, which is what Mono expects."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    window_from, window_to = next(backfill_chunks(now, months=1))
    # Sanity: a 30d window in seconds is 2_592_000 (not 2_592_000_000).
    assert int(window_to.timestamp()) - int(window_from.timestamp()) == 30 * 86_400


def test_zero_months_returns_empty():
    """Edge: months=0 yields no chunks (defensive; not directly used but well-defined)."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    assert list(backfill_chunks(now, months=0)) == []
```
  </action>
  <verify>
    <automated>uv run pytest tests/test_backfill_window_math.py tests/test_importer_no_token_in_url.py tests/test_importer_statement.py -x &amp;&amp; uv run python -c "from finance_bro.scheduler.errors import MonoAuthError, MonoRateLimitError, MonoTransientError; from finance_bro.scheduler.window import backfill_chunks; from finance_bro.importers.base import CanonicalAccount; ca = CanonicalAccount(source_account_id='x', source_kind='mono.card', currency='UAH', raw={}, mono_type='black'); assert ca.mono_type == 'black'; print('ok')"</automated>
  </verify>
  <done>scheduler package exists with __init__/errors/window; CanonicalAccount has mono_type; MonobankImporter raises typed exceptions on 401/429/other AND populates mono_type/hold/description/mcc; window math test passes; existing importer tests (no_token_in_url, statement) STILL pass — Phase 1 contract preserved.</done>
</task>

<task type="auto">
  <name>Task 2: SchedulerRunner — tick + recover_in_flight + read_state + enqueue helpers + 4 runner tests</name>
  <files>
    src/finance_bro/scheduler/runner.py,
    tests/test_scheduler_round_robin.py,
    tests/test_backfill_enqueue.py,
    tests/test_backfill_resumability.py,
    tests/test_401_stops_scheduler.py
  </files>
  <action>
**1) Create `src/finance_bro/scheduler/runner.py`** per PATTERNS.md lines 306-335 + RESEARCH.md Code Examples §3 (lines 854-909) + Pattern 7 (lines 596-614).

Module docstring:
```
"""SchedulerRunner — owns the tick logic, the recovery sweep, and the lifecycle helpers.

The runner is instantiated once per process from the FastAPI lifespan (D-04).
APScheduler fires runner.tick() every 10s with max_instances=1, coalesce=True
(D-03). The runner does NOT own rate limiting — RateLimitGate (Phase 1) is the
sole 65s budget owner; gate.acquire() runs inside MonobankImporter.fetch_statement
which the runner calls.

Anti-patterns explicitly avoided (per RESEARCH.md):
  - No second timer / time.sleep / second timestamp tracker.
  - No SQLAlchemyJobStore — APScheduler MemoryJobStore is correct; persisted
    state lives in `import_runs` and `scheduler_state`, not in the schedule.
  - No SKIP LOCKED on `import_runs` claim — single tick consumer.
"""
```

Class structure:

```python
import structlog
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_bro.db.account_repo import AccountRepo
from finance_bro.db.import_run_repo import ImportRunRepo
from finance_bro.db.models import Account, ImportRun
from finance_bro.db.scheduler_state_repo import SchedulerStateRepo
from finance_bro.db.transaction_repo import TransactionRepo
from finance_bro.importers.monobank import MonobankImporter
from finance_bro.scheduler.errors import MonoAuthError, MonoRateLimitError, MonoTransientError
from finance_bro.scheduler.window import (
    MONO_STATEMENT_BACKFILL_WINDOW_DAYS,
    backfill_chunks,
)

LIVE_POLL_LOOKBACK = timedelta(hours=1)   # D-16: window_from = last_polled_at - 1h ≈ now - 1h on first poll
RECOVER_THRESHOLD_SECONDS = 300            # 5 min — Pattern 7

_log = structlog.get_logger()


class SchedulerRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        importer: MonobankImporter,
    ) -> None:
        self._session_factory = session_factory
        self._importer = importer
        self._cached_state: tuple[str, str | None] = ("running", None)

    # ---- lifecycle helpers (called by lifespan) ----

    async def recover_in_flight(self) -> int:
        """Sweep stale in_flight rows back to pending (Pattern 7).
        Returns count swept. Called once at lifespan startup."""
        async with self._session_factory() as session, session.begin():
            count = await ImportRunRepo(session).recover_in_flight(RECOVER_THRESHOLD_SECONDS)
        if count:
            _log.info("scheduler.recover.in_flight_swept", count=count)
        return count

    async def read_state(self) -> tuple[str, str | None]:
        """Read scheduler_state singleton; cache in process. Called at lifespan startup
        and once per process (D-15 + Pattern 5 — never re-read in tick)."""
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
        """Enqueue 12 backfill chunks per active card (or just the requested account).
        Returns the list of inserted import_run ids."""
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
        _log.info("scheduler.backfill.enqueue", account_count=len(accounts), runs=len(ids_out))
        return ids_out

    async def enqueue_live_for_all_active_cards(self) -> list[tuple[int, int]]:
        """D-16: enqueue a live-poll import_run for each active card. Used by
        the reshaped POST /api/import (Plan 02-04). window = now-1h..now."""
        now = datetime.now(UTC)
        window_from = now - LIVE_POLL_LOOKBACK
        out: list[tuple[int, int]] = []
        async with self._session_factory() as session, session.begin():
            accounts = await AccountRepo(session).list_pollable_cards()
            repo = ImportRunRepo(session)
            for acc in accounts:
                run_id = await repo.enqueue_live(acc.id, window_from, now)
                out.append((acc.id, run_id))
        _log.info("scheduler.live.enqueue", account_count=len(accounts), runs=len(out))
        return out

    # ---- discovery (cold-boot) ----

    async def _ensure_accounts_discovered(self) -> None:
        """If accounts table is empty, run discovery. Mirrors ImportService Phase 1
        path (lines 52-62) and is safe to call multiple times due to uq_accounts_source.
        Raises MonoAuthError/MonoRateLimitError/MonoTransientError — caller (tick) handles."""
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
        id-asc among never-polled cards (last_live is None for them)."""
        async with self._session_factory() as session, session.begin():
            cards = await AccountRepo(session).list_pollable_cards()
            if not cards:
                return None
            ir_repo = ImportRunRepo(session)
            last_live = await ir_repo.last_live_per_account()
            # Filter out cards with active backfill (D-06)
            eligible: list[Account] = []
            for c in cards:
                if await ir_repo.count_pending_or_in_flight_backfill(c.id) > 0:
                    continue
                eligible.append(c)
            if not eligible:
                return None
            # Prefer never-polled (last_live is None) by id ASC; otherwise oldest completed_at.
            never_polled = [c for c in eligible if c.id not in last_live]
            if never_polled:
                return never_polled[0]   # cards is already ORDER BY id ASC
            # Sort by last_live[c.id].completed_at ASC
            return min(eligible, key=lambda c: last_live[c.id].completed_at or datetime.min.replace(tzinfo=UTC))

    # ---- tick (the heart) ----

    async def tick(self) -> None:
        """Verbatim from RESEARCH.md Code Examples §3 lines 864-908."""
        if self._cached_state[0] != "running":
            return

        # Ensure discovery has run (cold-boot — no accounts yet)
        try:
            await self._ensure_accounts_discovered()
        except MonoAuthError as e:
            await self._set_state_auth_failed(str(e))
            _log.error("scheduler.tick.discovery.auth_failed")
            return
        except (MonoRateLimitError, MonoTransientError) as e:
            _log.warning("scheduler.tick.discovery.transient", error=str(e))
            return   # next tick retries

        # Step 2: claim a pending row
        async with self._session_factory() as session, session.begin():
            run = await ImportRunRepo(session).claim_next_pending()

        if run is None:
            # Step 4: no pending — enqueue a fresh live row for the next active card
            card = await self._pick_next_active_card()
            if card is None:
                return
            now = datetime.now(UTC)
            window_from = now - LIVE_POLL_LOOKBACK
            async with self._session_factory() as session, session.begin():
                await ImportRunRepo(session).enqueue_live(card.id, window_from, now)
            return

        # Step 4-5: fetch + upsert
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
                t async for t in self._importer.fetch_statement(
                    account.source_account_id, run.window_from, run.window_to
                )
            ]
            async with self._session_factory() as session, session.begin():
                inserted, updated = await TransactionRepo(session).insert_many(account.id, items)
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
            await self._mark_error(run.id, f"429 (Retry-After={e.retry_after_seconds})")
            _log.warning("scheduler.tick.mono_429", import_run_id=run.id, retry_after=e.retry_after_seconds)
        except MonoTransientError as e:
            await self._mark_error(run.id, str(e))
            _log.warning("scheduler.tick.transient", import_run_id=run.id, error=str(e))
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
```

**Critical anti-patterns to avoid (RESEARCH.md):**
- Adding a `time.sleep` or second timer.
- Catching `httpx.HTTPStatusError` directly in tick — typed exceptions only.
- Using `SELECT ... FOR UPDATE SKIP LOCKED` for `import_runs` claim.
- Multiplying timestamps by 1000.

**2) Create `tests/test_scheduler_round_robin.py`** — SC#1 + D-01 + D-02. Archetype B + respx-mocked importer.

Required test functions:

```python
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import text

from finance_bro.importers.monobank import MonobankImporter
from finance_bro.importers.rate_limit import RateLimitGate
from finance_bro.scheduler.runner import SchedulerRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _make_runner(session_factory):
    gate = RateLimitGate(session_factory)
    importer = MonobankImporter(token="dummy-token-32chars-aaaaaaaaaaaaaaaa", gate=gate)
    return SchedulerRunner(session_factory=session_factory, importer=importer)


@pytest.mark.asyncio
async def test_eaid_skipped(session_factory):
    """SC#1 + D-01: list_pollable_cards excludes eAid; the runner's pick path never sees it.

    Seed 4 cards (eAid, black, platinum, white) directly. Call _pick_next_active_card
    repeatedly across simulated polls; assert eAid id never returned.
    """
    # Seed accounts with explicit ids for deterministic id-ASC ordering.
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid'),
              (2, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
              (3, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum'),
              (4, 'mono.card', 'white-id', 'UAH', '{}'::jsonb, 'white')
        """))
        await s.commit()

    runner = _make_runner(session_factory)
    picked_ids = set()
    for _ in range(10):
        card = await runner._pick_next_active_card()
        if card is None:
            break
        picked_ids.add(card.id)
        # Simulate completion of a live run so next pick rotates
        async with session_factory() as s:
            await s.execute(text("""
                INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, completed_at, statement_count, inserted)
                VALUES (:aid, 'live', now()-interval '1 hour', now(), 'done', now(), 0, 0)
            """), {"aid": card.id})
            await s.commit()
    assert 1 not in picked_ids   # eAid (id=1) never picked
    assert picked_ids == {2, 3, 4}


@pytest.mark.asyncio
async def test_three_cards_visited_three_ticks(session_factory):
    """SC#1: 3 active cards round-robin; with respx mock returning empty statements,
    runner.tick() x N visits each card. Use a stub gate (sleep patched) so tests don't hang."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black'),
              (2, 'mono.card', 'platinum-id', 'UAH', '{}'::jsonb, 'platinum'),
              (3, 'mono.card', 'white-id', 'UAH', '{}'::jsonb, 'white')
        """))
        await s.commit()

    runner = _make_runner(session_factory)

    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(return_value=httpx.Response(200, json=[]))
        # Tick 1: no pending → enqueues live for card id=1 (oldest = never polled, picked by id ASC)
        await runner.tick()
        # Tick 2: claims the live row for card 1 → fetches → marks done
        await runner.tick()
        # Repeat for the next two cards (4 more ticks: enqueue+execute each)
        for _ in range(4):
            await runner.tick()

    # Assert each card got at least one done live run
    async with session_factory() as s:
        rows = (await s.execute(text("""
            SELECT account_id, count(*) FROM import_runs WHERE run_kind='live' AND status='done'
            GROUP BY account_id ORDER BY account_id
        """))).all()
    visited = {r[0] for r in rows}
    assert visited == {1, 2, 3}


@pytest.mark.asyncio
async def test_eaid_skipped_via_tick(session_factory):
    """E2E: the tick path also never picks eAid even with the live-row claim path."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid'),
              (2, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.commit()

    runner = _make_runner(session_factory)
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(return_value=httpx.Response(200, json=[]))
        for _ in range(6):
            await runner.tick()

    async with session_factory() as s:
        rows = (await s.execute(text("SELECT DISTINCT account_id FROM import_runs"))).scalars().all()
    assert 1 not in rows
```

**3) Create `tests/test_backfill_enqueue.py`** — D-05 + D-08.

```python
@pytest.mark.asyncio
async def test_twelve_chunks_newest_first(session_factory):
    """ING-06: enqueue_backfill creates 12 import_runs rows with run_kind='backfill',
    status='pending', windows 30d apart in newest-first order."""
    # Seed one allowlisted card.
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
            VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.commit()

    runner = _make_runner(session_factory)
    ids = await runner.enqueue_backfill(account_id=1, months=12)
    assert len(ids) == 12

    async with session_factory() as s:
        rows = (await s.execute(text("""
            SELECT id, window_from, window_to, run_kind, status FROM import_runs
            WHERE account_id=1 ORDER BY window_to DESC
        """))).all()
    assert len(rows) == 12
    assert all(r.run_kind == 'backfill' for r in rows)
    assert all(r.status == 'pending' for r in rows)
    # Newest-first: rows[0].window_to is the latest.
    deltas = [(rows[i].window_from, rows[i].window_to) for i in range(12)]
    for i in range(11):
        # Each chunk is 30d wide
        assert (deltas[i][1] - deltas[i][0]).days == 30
        # Adjacent chunks abut: rows[i].window_from == rows[i+1].window_to
        assert deltas[i][0] == deltas[i+1][1]


@pytest.mark.asyncio
async def test_enqueue_backfill_skips_eaid(session_factory):
    """D-01: enqueue_backfill respects list_pollable_cards (no eAid)."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type) VALUES
              (1, 'mono.card', 'eaid-id', 'UAH', '{}'::jsonb, 'eAid'),
              (2, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.commit()
    runner = _make_runner(session_factory)
    ids = await runner.enqueue_backfill(account_id=None, months=12)   # all active cards
    assert len(ids) == 12   # only card 2, 12 chunks

    async with session_factory() as s:
        eaid_count = (await s.execute(text("SELECT count(*) FROM import_runs WHERE account_id=1"))).scalar_one()
    assert eaid_count == 0
```

**4) Create `tests/test_backfill_resumability.py`** — SC#2 + ING-06 + Pitfall 7. Mix of Archetype B and D.

Required test functions:
- `test_recover_in_flight_on_restart` — seed an in_flight row with stale started_at; create a fresh runner; call recover_in_flight; assert row is now pending. Mirrors `tests/test_rate_limit_gate.py::test_persists_across_restart` (PATTERNS.md Archetype D).
- `test_resume_picks_remaining_chunks` — enqueue 12 backfill rows; mark 5 as done; instantiate a new runner; call tick repeatedly; assert the remaining 7 rows complete in newest-first order (since the dequeue is `created_at ASC` and they were enqueued newest-first, they execute newest-first naturally).
- `test_full_12_month_walk` — full 12-chunk walk with respx returning `statement_empty.json` (`[]`). Run tick 12 times after enqueue; assert all 12 done.
- `test_4xx_marks_error_not_skip` — respx returns 400 for one chunk; tick claims and fails with `MonoTransientError`; the row's status='error', last_error mentions 400.

```python
@pytest.mark.asyncio
async def test_recover_in_flight_on_restart(session_factory):
    """Pattern 7: a stale in_flight row is reset to pending by recover_in_flight."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
            VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status, started_at, attempts)
            VALUES (1, 'live', now()-interval '1 hour', now(), 'in_flight', now() - interval '6 minutes', 1)
        """))
        await s.commit()
    # Simulate restart: fresh runner instance reads same DB
    runner = _make_runner(session_factory)
    swept = await runner.recover_in_flight()
    assert swept == 1
    async with session_factory() as s:
        status = (await s.execute(text("SELECT status FROM import_runs"))).scalar_one()
    assert status == 'pending'


# Other tests follow the patterns above. Use respx + asyncio.sleep patch for any
# tick that goes through the gate; load tests/fixtures/statement_empty.json for [].
```

**5) Create `tests/test_401_stops_scheduler.py`** — SC#4 + D-15. Archetype C + D.

```python
@pytest.mark.asyncio
async def test_401_persists_across_restart(session_factory):
    """D-15: a 401 from Mono sets scheduler_state.state='auth_failed' and persists
    so a fresh runner instance reads sticky."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
            VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status)
            VALUES (1, 'live', now()-interval '1 hour', now(), 'pending')
        """))
        await s.commit()

    runner_a = _make_runner(session_factory)
    await runner_a.read_state()   # cache state='running'
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(401, json={"errorDescription": "Unknown 'X-Token'"})
        )
        await runner_a.tick()
    # Assert in-process cache flipped
    assert runner_a._cached_state[0] == 'auth_failed'
    # Assert DB persisted
    async with session_factory() as s:
        state = (await s.execute(text("SELECT state FROM scheduler_state WHERE id=1"))).scalar_one()
    assert state == 'auth_failed'
    # Simulate restart: fresh runner reads sticky bit
    runner_b = _make_runner(session_factory)
    state_b, err_b = await runner_b.read_state()
    assert state_b == 'auth_failed'
    assert err_b is not None and '401' in err_b
    # Subsequent tick is a no-op
    await runner_b.tick()   # must not raise; must not call Mono
```

This single test exercises the full SC#4 401 path end-to-end. The cross-restart simulation is the key novelty.
  </action>
  <verify>
    <automated>uv run pytest tests/test_scheduler_round_robin.py tests/test_backfill_enqueue.py tests/test_backfill_resumability.py tests/test_401_stops_scheduler.py -x &amp;&amp; uv run python -c "from finance_bro.scheduler.runner import SchedulerRunner; import inspect; assert 'tick' in dir(SchedulerRunner); assert 'recover_in_flight' in dir(SchedulerRunner); assert 'enqueue_backfill' in dir(SchedulerRunner); assert 'enqueue_live_for_all_active_cards' in dir(SchedulerRunner); print('runner ok')" &amp;&amp; ! grep -E "time\.sleep|SKIP LOCKED" src/finance_bro/scheduler/runner.py &amp;&amp; ! grep -E "raw\.HTTPStatusError" src/finance_bro/scheduler/runner.py</automated>
  </verify>
  <done>SchedulerRunner exists and exports the required public methods; round-robin test confirms eAid is never picked and the 3 allowlisted cards are visited; backfill enqueue produces 12 newest-first rows per active card; recover_in_flight resets stale in_flight; 401 cycle through tick → scheduler_state='auth_failed' AND survives "restart" via fresh runner reading the same DB.</done>
</task>

<task type="auto">
  <name>Task 3: Lifespan integration (main.py + deps.py); 429 test; APP_DISABLE_SCHEDULER env switch</name>
  <files>
    src/finance_bro/main.py,
    src/finance_bro/api/deps.py,
    tests/conftest.py,
    tests/test_429_does_not_stop.py
  </files>
  <action>
**1) `src/finance_bro/main.py`** — extend per RESEARCH.md Code Examples §2 + Pattern 1.

Read the current 41-line file in full first.

Modify imports:
```python
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from finance_bro.api import (
    routes_accounts,
    routes_health,
    routes_import,
    routes_transactions,
)
from finance_bro.core import logging as logging_cfg
from finance_bro.core.settings import get_settings
from finance_bro.db.engine import get_session_factory, init_engine
from finance_bro.importers.monobank import MonobankImporter
from finance_bro.importers.rate_limit import RateLimitGate
from finance_bro.scheduler.runner import SchedulerRunner
```

Replace lifespan body:
```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    init_engine()

    # Build the runner regardless of scheduler enable so app.state.runner is
    # always available for routes (Plan 02-04 force-poll + backfill endpoints
    # depend on this).
    session_factory = get_session_factory()
    gate = RateLimitGate(session_factory)
    importer = MonobankImporter(settings.mono_token, gate)
    runner = SchedulerRunner(session_factory=session_factory, importer=importer)
    swept = await runner.recover_in_flight()
    state, last_err = await runner.read_state()

    scheduler = AsyncIOScheduler()
    disable_scheduler = os.environ.get("APP_DISABLE_SCHEDULER") == "1"
    if state == "running" and not disable_scheduler:
        scheduler.add_job(
            runner.tick,
            IntervalTrigger(seconds=10),
            id="finance-bro-tick",
            max_instances=1,           # D-03
            coalesce=True,             # D-03
            misfire_grace_time=30,
        )
        scheduler.start()

    app.state.scheduler = scheduler
    app.state.runner = runner

    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)   # Pitfall 8 — wait=False
        await runner.aclose()


app = FastAPI(title="finance-bro", lifespan=lifespan)
app.include_router(routes_health.router)
app.include_router(routes_accounts.router)
app.include_router(routes_transactions.router)
app.include_router(routes_import.router)
```

**Critical ordering (RESEARCH.md Pattern 1 + Pitfall 8):**
- `init_engine()` BEFORE creating runner (engine must exist).
- `runner.recover_in_flight()` BEFORE `scheduler.start()` (no concurrent tick at sweep time).
- `scheduler.shutdown(wait=False)` BEFORE `runner.aclose()` (in-flight tick canceled cleanly; httpx client closed last).
- The 4 Phase 1 routers stay mounted as before. Plan 02-04 mounts `routes_status` and `routes_backfill` in addition.

**2) `src/finance_bro/api/deps.py`** — add `get_scheduler_runner` per PATTERNS.md lines 526-528.

```python
from fastapi import Request   # add to imports

# ... existing providers unchanged ...

def get_scheduler_runner(request: Request) -> SchedulerRunner:
    """Return the process-scoped SchedulerRunner attached at lifespan startup
    (RESEARCH.md Pattern 1 / Code Examples §2 line 346-347).
    Used by Plan 02-04's POST /api/import (D-16) and POST /api/backfill (D-07)."""
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise RuntimeError("SchedulerRunner missing from app.state — did lifespan fire?")
    return runner
```

Add `from finance_bro.scheduler.runner import SchedulerRunner` to imports (alongside the existing imports). Do NOT add `get_scheduler` for the APScheduler instance itself — anti-pattern per PATTERNS.md.

**3) `tests/conftest.py`** — add `APP_DISABLE_SCHEDULER` to the env vars set during fixture setup so the lifespan does NOT start the APScheduler in test mode.

In the `pg_url` fixture (already sets `os.environ["DATABASE_URL"]`, `os.environ.setdefault("MONO_TOKEN", ...)`), add right after:
```python
os.environ["APP_DISABLE_SCHEDULER"] = "1"
```
Place it AFTER the existing env setdefault calls. This guarantees the lifespan's runner instantiation still runs (so `app.state.runner` exists for any test that wants to call it directly), but `scheduler.start()` is skipped — preventing the tick from firing during HTTP-route tests.

The `client` fixture already runs `LifespanManager(app)`; with the disable flag set, the lifespan code completes the recover_in_flight + read_state path but skips scheduler.start.

**4) Create `tests/test_429_does_not_stop.py`** — SC#4 + D-15 transient-429.

```python
@pytest.mark.asyncio
async def test_429_marks_run_error_but_state_remains_running(session_factory):
    """D-15: a 429 sets import_runs.status='error' with 429 in last_error
    but does NOT transition scheduler_state to auth_failed."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
            VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status)
            VALUES (1, 'live', now()-interval '1 hour', now(), 'pending')
        """))
        await s.commit()

    runner = _make_runner(session_factory)
    await runner.read_state()
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "60"}, json={"errorDescription": "Too many requests"})
        )
        await runner.tick()

    # Run is marked error with 429 in the message
    async with session_factory() as s:
        row = (await s.execute(text("SELECT status, last_error FROM import_runs WHERE account_id=1"))).first()
    assert row.status == 'error'
    assert '429' in row.last_error or 'Retry-After' in row.last_error
    # Scheduler state is unchanged
    assert runner._cached_state[0] == 'running'
    async with session_factory() as s:
        state = (await s.execute(text("SELECT state FROM scheduler_state WHERE id=1"))).scalar_one()
    assert state == 'running'


@pytest.mark.asyncio
async def test_429_without_retry_after_handled(session_factory):
    """Pattern 4: missing Retry-After header → retry_after_seconds=None; no crash."""
    async with session_factory() as s:
        await s.execute(text("""
            INSERT INTO accounts (id, source_kind, source_account_id, currency, raw_payload, mono_type)
            VALUES (1, 'mono.card', 'black-id', 'USD', '{}'::jsonb, 'black')
        """))
        await s.execute(text("""
            INSERT INTO import_runs (account_id, run_kind, window_from, window_to, status)
            VALUES (1, 'live', now()-interval '1 hour', now(), 'pending')
        """))
        await s.commit()
    runner = _make_runner(session_factory)
    await runner.read_state()
    with respx.mock(base_url="https://api.monobank.ua") as mock, patch(
        "finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock
    ):
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(429, json={})   # no Retry-After header
        )
        await runner.tick()
    async with session_factory() as s:
        status = (await s.execute(text("SELECT status FROM import_runs WHERE account_id=1"))).scalar_one()
    assert status == 'error'
```

Use the same `_make_runner` helper from `test_scheduler_round_robin.py` (either copy-paste or import; copy-paste keeps each test file standalone — preferred per Phase 1 testing style).
  </action>
  <verify>
    <automated>uv run pytest tests/test_429_does_not_stop.py -x &amp;&amp; uv run pytest -x &amp;&amp; uv run python -c "from finance_bro.api.deps import get_scheduler_runner; from finance_bro.main import app; print('lifespan integration ok')" &amp;&amp; grep -q "AsyncIOScheduler" src/finance_bro/main.py &amp;&amp; grep -q "shutdown(wait=False)" src/finance_bro/main.py &amp;&amp; grep -q "APP_DISABLE_SCHEDULER" src/finance_bro/main.py &amp;&amp; grep -q "max_instances=1" src/finance_bro/main.py &amp;&amp; grep -q "coalesce=True" src/finance_bro/main.py</automated>
  </verify>
  <done>Lifespan starts/stops the scheduler with the correct flags (max_instances=1, coalesce=True, misfire_grace_time=30, IntervalTrigger(seconds=10)); shutdown uses wait=False (Pitfall 8); the disable env switch is honored so existing route tests don't see scheduler ticks; 429 path keeps scheduler_state='running' but marks the run errored; full suite passes including Phase 1 invariants.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Network → app | Tailscale/LAN-only (DEP-02). No new boundary in this plan. |
| Mono API → importer | TLS-authenticated; the importer maps HTTP 401/429/other into typed exceptions at this seam. |
| Importer → scheduler | Typed exceptions (`MonoAuthError`/`MonoRateLimitError`/`MonoTransientError`); scheduler branches on type, never on HTTP status string match. |
| Scheduler → DB | Same connection pool as Phase 1; runner uses `async with session_factory() as session, session.begin()` per Pattern S2. |
| Scheduler → APScheduler clock | `time.monotonic()` based; tests do NOT use `freezegun` (Pitfall 9 — direct `await runner.tick()` instead). |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-02-01 | Information Disclosure | Mono token leakage via tick logging | mitigate | Phase 1's structlog redaction processor masks any key matching `token`/`X-Token`/`amount` substrings (`tests/test_log_redaction.py`). All new log keys in this plan (`import_run_id`, `account_id`, `run_kind`, `statement_count`, `inserted`, `updated_in_place`, `retry_after`) are non-sensitive identifiers. **Verify** by running `tests/test_log_redaction.py` after this plan lands — must remain green. |
| T-02-02 | Denial of Service | DoS-via-Mono on 401 retry storm | mitigate | D-15 sticky `auth_failed` is persisted to `scheduler_state` (Pattern 5). Lifespan reads this BEFORE calling `scheduler.start()` (Pitfall 4). A fresh container with the same bad token does NOT re-poll Mono. Test: `test_401_persists_across_restart` exercises this end-to-end with a "fresh runner" simulating restart. |
| T-02-03 | Tampering / Data Loss | mid-backfill kill leaves orphaned data | mitigate | `recover_in_flight` sweep at lifespan startup (Pattern 7) resets stale `in_flight` rows back to `pending`. Test: `test_recover_in_flight_on_restart`. The `(account_id, source_tx_id) WHERE NOT is_deleted` partial unique index (Phase 1 invariant) makes the re-execution idempotent. |
| T-02-09 | Tampering | Importer-level mono_type spoofing | accept | Mono is the authoritative source of `acc["type"]`; we trust the TLS-authenticated payload. The allowlist is fail-closed: an unknown type maps to NULL (jars/non-cards) or stays excluded (eAid). No new attack surface — same trust boundary as Phase 1's `currencyCode` mapping. |
| T-02-10 | DoS | APScheduler tick fires while a previous tick is still inside gate.acquire | mitigate | Pitfall 1 + RESEARCH.md Code Examples §2: `max_instances=1, coalesce=True` set explicitly. APScheduler 3.x default is also 1 but explicit is enforceable. Verified by grep gate in Task 3 verify. |
| T-02-11 | Repudiation | scheduler.shutdown blocks lifespan exit | mitigate | Pitfall 8: `scheduler.shutdown(wait=False)` is correct. Verified by grep gate. |
</threat_model>

<verification>
**Plan-level checks (run before commit/handoff):**

1. `uv run pytest -x` — full suite green.
2. `grep -E "time\.sleep|SKIP LOCKED|advisory_lock|forwardRef" src/finance_bro/scheduler/` returns nothing.
3. `grep -c "max_instances=1" src/finance_bro/main.py` returns `1` (D-03).
4. `grep -c "coalesce=True" src/finance_bro/main.py` returns `1` (D-03).
5. `grep -c "shutdown(wait=False)" src/finance_bro/main.py` returns `1` (Pitfall 8).
6. `grep -E "raise MonoAuthError|raise MonoRateLimitError|raise MonoTransientError" src/finance_bro/importers/monobank.py | wc -l` ≥ `4` (two methods × two error pairs minimum).
7. `grep -q "await self._gate.acquire(self._token)" src/finance_bro/importers/monobank.py` — Pattern S7 invariant: gate FIRST in both methods.
8. `grep -c "literal_column" src/finance_bro/db/transaction_repo.py` returns `1` (Plan 02-02 invariant — sanity check it didn't regress).
9. **Phase 1 regression suite:** `uv run pytest tests/test_health.py tests/test_no_auth.py tests/test_idempotency.py tests/test_partial_unique_index.py tests/test_log_redaction.py tests/test_rate_limit_gate.py tests/test_importer_no_token_in_url.py tests/test_importer_statement.py tests/test_money_invariants.py tests/test_schema_invariants.py tests/test_settings.py -x`.

**Sanity grep:** `grep -RE "(routes_status|routes_backfill|ImportEnqueuedOut|ImportStatusOut)" src/` should be empty. Those are Plan 02-04.
</verification>

<success_criteria>
- All Tasks' `<verify>` commands pass.
- `uv run pytest -x` is fully green; Phase 1 invariants intact.
- The runner's must_haves.truths are each backed by a passing test:
  - Round-robin with eAid skipped: `test_eaid_skipped`, `test_eaid_skipped_via_tick`, `test_three_cards_visited_three_ticks`.
  - 12-month backfill enqueue: `test_twelve_chunks_newest_first`, `test_enqueue_backfill_skips_eaid`.
  - Resumability: `test_recover_in_flight_on_restart`, `test_resume_picks_remaining_chunks`, `test_full_12_month_walk`, `test_4xx_marks_error_not_skip`.
  - Window math: every test in `test_backfill_window_math.py`.
  - 401 sticky: `test_401_persists_across_restart`.
  - 429 transient: `test_429_marks_run_error_but_state_remains_running`, `test_429_without_retry_after_handled`.
- Lifespan starts the scheduler in production but skips it in tests via `APP_DISABLE_SCHEDULER=1`. `app.state.runner` is always populated.
- `MonobankImporter` continues to honor Pattern S7 (gate FIRST, token in header only).
- The runner's tick body matches RESEARCH.md Code Examples §3 structurally; no second timer or sleep introduced.
</success_criteria>

<output>
After completion, create `.planning/phases/02-reliable-sync/02-03-SUMMARY.md` covering: scheduler package layout, the runner's three lifecycle entry points (recover_in_flight, read_state, tick), the typed-exception split, the lifespan ordering (init_engine → recover → read_state → maybe-add_job → start → yield → shutdown(wait=False) → aclose), the APP_DISABLE_SCHEDULER env switch and why it exists, any empirical findings from running the runner tests (e.g. did `respx` cleanly mock the importer? did `claim_next_pending` race in any way that single-consumer made invisible?), and any open questions ready for empirical resolution in Phase 2 production (see CONTEXT.md "specifics" Open Questions 1-3).
</output>
