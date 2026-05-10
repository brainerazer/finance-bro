# Phase 2: Reliable Sync - Research

**Researched:** 2026-05-10
**Domain:** APScheduler-driven Mono polling, resumable backfill cursoring, Postgres upsert with mutate-fields-on-conflict, sticky scheduler-state recovery, 401/429 distinction over httpx
**Confidence:** HIGH (all locked decisions in CONTEXT.md were verified against APScheduler 3.11 docs, Mono docs from Context7, the existing Phase 1 codebase, and Postgres 17 INSERT semantics)

## Summary

Phase 2 turns Phase 1's manual `POST /api/import` into autonomous round-robin polling at the rate-limit budget (≤1 Mono call per 65 s, owned by the existing `RateLimitGate`), adds 12-month chunked-resumable backfill, makes the hold→cleared transition an in-place upsert that never overwrites manual edits, and exposes `/api/import/status` so 401 (token revoked → sticky stop) and 429 (rate-limit transient → keep going) are distinguishable in the UI.

The shape is fully determined by 17 locked decisions in `02-CONTEXT.md`. This research is prescriptive — it documents the *implementation shape* of those decisions (APScheduler lifespan integration, the `import_runs` enqueue/dequeue idiom, the `ON CONFLICT DO UPDATE` SQL with EXCLUDED references, the `xmax = 0` insert/update detection trick, the `scheduler_state` singleton, and the recovery-on-startup sweep) so the planner can task-decompose without reopening choices. No alternatives are explored where CONTEXT.md already chose.

**Primary recommendation:** Use APScheduler 3.11.2's `AsyncIOScheduler` started inside the FastAPI `lifespan` after `init_engine()`. The single `tick()` job runs every 10 s with `max_instances=1, coalesce=True`. The tick does not own rate limiting — it asks the existing `RateLimitGate` for a slot exactly where the importer already does (Phase 1's `MonobankImporter._gate.acquire(token)`). Backfill is encoded as 12 pre-enqueued `import_runs` rows per active card (run_kind='backfill', newest-first, 30-day windows). Live polling uses the same table (run_kind='live', last_polled_at-1h..now window). Hold→cleared transitions ride on `INSERT ... ON CONFLICT (account_id, source_tx_id) WHERE NOT is_deleted DO UPDATE SET hold = EXCLUDED.hold, amount_minor = EXCLUDED.amount_minor, raw_payload = EXCLUDED.raw_payload`. 401 is sticky via a one-row `scheduler_state(id INTEGER PK CHECK(id=1))` table. 429 is per-call surface, not sticky. On lifespan startup, sweep `import_runs.status='in_flight'` rows older than 5 minutes back to `'pending'`.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

#### Polling scope & round-robin
- **D-01 (poll-set):** Only `accounts` rows with `source_kind = mono.card` AND `mono_type ∈ {black, platinum, white}` enter the poll rotation. Persist Mono's `type` field as a new top-level `accounts.mono_type TEXT NULL` column (extracted from `raw_payload.type` at discovery time). The allowlist excludes the eAid charity card (Phase 1's empirical landmine) and is **fail-closed**: a future Mono card type (e.g. `iron`) does NOT auto-poll until the allowlist is widened. Jars (`source_kind = mono.jar`) and FOPs (`source_kind = mono.fop`) are persisted on discovery (Phase 1 D-05 stays) but the scheduler skips them entirely; they still appear in `GET /api/accounts`.
- **D-02 (order):** Round-robin order is `ORDER BY id ASC` over the allowlisted set. Deterministic, no extra state. New accounts join at the tail naturally as discovery upserts them. No activity-weighting, no skip-after-N-empty backoff in v1.
- **D-03 (cadence):** The scheduler does **not** own rate-limit timing — the Phase 1 `RateLimitGate` (65 s, Postgres `FOR UPDATE`) does. APScheduler fires a single `poll_next_account` job at a tighter interval (10 s) with `max_instances=1, coalesce=True`; the gate naturally serializes everything to one Mono call per 65 s.
- **D-04 (lifecycle):** `AsyncIOScheduler` starts inside the FastAPI `lifespan` startup phase, after `init_engine()`. It stops on `lifespan` shutdown and on a sticky `auth_failed` state (D-15). No manual start/stop endpoint in v1.

#### Backfill orchestration
- **D-05 (trigger):** Backfill is **auto-triggered** on the first scheduler tick after boot. Trigger condition: an active card has fewer than ~30 days of historical transactions in the DB. The scheduler enqueues 12 chunked `import_runs` rows for that card before any normal-poll rows. Manual trigger via `POST /api/backfill?account_id=X` is supported as an escape hatch.
- **D-06 (gate sharing):** While any `import_runs` row for an account has `status IN ('pending', 'in_flight')` AND `run_kind = 'backfill'`, the scheduler **skips normal polling for that account** (other accounts continue normally). The gate still enforces the global 65 s cadence so total throughput is unchanged.
- **D-07 (execution):** Backfill runs as APScheduler jobs (one per `import_runs` row), not as a foreground HTTP call. `POST /api/backfill` returns `202 Accepted` with `{run_ids: [...]}` immediately.
- **D-08 (cursor model):** New `import_runs` table:
  ```
  import_runs(
    id              BIGINT PK,
    account_id      BIGINT FK accounts(id),
    run_kind        TEXT NOT NULL CHECK (run_kind IN ('backfill', 'live')),
    window_from     TIMESTAMPTZ NOT NULL,
    window_to       TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'done', 'error')),
    last_error      TEXT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    statement_count INTEGER NULL,
    inserted        INTEGER NULL,
    started_at      TIMESTAMPTZ NULL,
    completed_at    TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
  )
  ```
  Backfill enqueues 12 `pending` rows with `run_kind='backfill'` walking newest-first in 30-day chunks (constant `MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30`). Resume = `SELECT WHERE status != 'done' ORDER BY window_from DESC`. A killed-mid-chunk row stays `in_flight`; on restart it gets re-run; idempotent because the `(account_id, source_tx_id) WHERE NOT is_deleted` partial unique index swallows duplicates.
- **D-09 (window walk):** Newest-first per ROADMAP.md SC#2. The first chunk Bohdan sees populated is "this month"; the deepest finishes last.

#### Hold → cleared upsert semantics
- **D-10 (mutable fields):** `TransactionRepo.insert_many` switches from `ON CONFLICT DO NOTHING` to `ON CONFLICT (account_id, source_tx_id) WHERE NOT is_deleted DO UPDATE SET hold = EXCLUDED.hold, amount_minor = EXCLUDED.amount_minor, raw_payload = EXCLUDED.raw_payload`. **Only those three fields mutate.** `currency`, `time`, `account_id`, `source_tx_id`, `created_at` are frozen by omission. `is_user_locked`, `category_id`, `category_source`, `is_deleted`, `description`, `mcc`, `attributed_day` are left alone — Phase 1's Pitfall-10 promise that the importer never overwrites manual edits stays a hard invariant.
- **D-11 (raw_payload):** The cleared payload **overwrites** the hold payload. No history table, no JSONB array. `import_runs` carries enough audit to debug Mono quirks.
- **D-12 (API shape):** `TransactionOut` gains a `hold: bool` field. `GET /api/transactions` returns ALL rows (cleared + held) in time-desc order; the client filters if needed.
- **D-13 (no audit columns):** No `prior_amount_minor`, no separate hold-history table.

#### Sync status surface
- **D-14 (status shape):** `GET /api/import/status` returns:
  ```json
  {
    "scheduler": { "state": "running" | "auth_failed" | "stopped", "since": "...", "last_error": null | "..." },
    "accounts": [ { "account_id": 1, "source_account_id": "...", "mono_type": "black",
                    "last_polled_at": "...", "last_poll_inserted": 3, "last_poll_updated": 0,
                    "last_status": "ok" | "error" | "rate_limited", "last_error": null | "..." }, ... ],
    "backfill": { "state": "idle" | "running", "runs_remaining": 0, "runs_total": 0, "eta_seconds": null }
  }
  ```
- **D-15 (401 vs 429):** **401** sets `scheduler.state = 'auth_failed'`, persists, **stops the APScheduler job permanently** until app restart. **429** is per-call: log it, set `accounts[i].last_status = 'rate_limited'`, do not stop the scheduler.
- **D-16 (manual import):** `POST /api/import` is **kept** but its semantics change: it enqueues an immediate live-poll for **every active card** (D-01's allowlisted set) by inserting `import_runs` rows with `status='pending', run_kind='live', window_from=last_polled_at-1h, window_to=now`. Returns `202 Accepted` with `{enqueued: [{account_id, run_id}, ...]}`. **Phase 1's synchronous body is GONE** — `tests/test_import_route.py` breaks and must be rewritten.
- **D-17 (error history depth):** Status response carries last-error per account + last-error per scheduler only.

### Claude's Discretion
- **Scheduler tick interval** — 10 s with `max_instances=1, coalesce=True`. The actual rate-limiting is the gate (65 s).
- **`mono_type` extraction** — at the `MonobankImporter.discover_accounts` boundary, pull `acc.get("type")` for cards. Jars don't have `type`; FOPs use the existing `mono.fop` source_kind; `mono_type` is NULL for non-cards.
- **`accounts.mono_type` migration** — single Alembic revision adds `accounts.mono_type TEXT NULL` and backfills it: `UPDATE accounts SET mono_type = raw_payload->>'type' WHERE source_kind = 'mono.card'`.
- **`import_runs` migration** — single Alembic revision adds the table per D-08. No seeded data.
- **APScheduler tick logic** — one `tick()` job at 10 s that: (1) checks `scheduler_state.state`, returning early if `auth_failed`/`stopped`; (2) picks oldest `pending` `import_runs` row; (3) if none, picks the next active card whose last `live` run is oldest, enqueues a fresh `live` row and returns; (4) acquires the gate, fetches, upserts via `TransactionRepo.insert_many`, updates `import_runs` row to `done`/`error`; (5) on 401 from any HTTP call: set in-memory `scheduler.state = 'auth_failed'`, persist to `scheduler_state`, return.
- **Scheduler state persistence** — one-row `scheduler_state(id INTEGER PK CHECK (id=1), state TEXT, last_error TEXT, since TIMESTAMPTZ)` so `auth_failed` survives restarts.
- **Gate already covers 429 path** — no new code needed; surface the wait to status as `last_status='rate_limited'`.
- **Hold → cleared `description`/`mcc` mutation policy** — importer is free to populate `description`, `mcc`, `attributed_day` on **first insert** (when the row is newly created from a `hold:true` payload). They become immutable only after the row exists. Default for `attributed_day`: leave NULL — Phase 3 owns timezone semantics.
- **`POST /api/backfill` body** — accepts `{account_id?: int, months?: int = 12}`. Default = backfill all active cards 12 months.
- **Round-robin starvation** — "next card by oldest last-poll" naturally rotates without explicit cursor state.
- **Discovery refresh** — Phase 2 keeps Phase 1's one-shot model. No `POST /api/accounts/refresh` endpoint.
- **Tests** — Phase 1's testcontainers + httpx-mock harness extends naturally; six new test files identified.

### Deferred Ideas (OUT OF SCOPE)
- Activity-weighted polling
- Skip-after-N-empty backoff per account
- Manual `POST /api/accounts/refresh`
- `POST /api/scheduler/start` and `/stop` endpoints
- 401 auto-retry hourly
- Per-call retry policy with `tenacity`
- Per-chunk `prior_amount_minor` audit column
- Hold-history JSONB array on `transactions.raw_payload`
- `POST /api/transactions/pending` separate endpoint
- Top-N error feed in `/api/import/status`
- Periodic `client-info` re-discovery
- APScheduler v4 migration

</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ING-05 | Hold/pending transactions ingested with `hold` flag; excluded from totals; updated in-place when same `id` arrives with `hold=false` | D-10 + D-11 + D-12 + Standard Stack §SQLAlchemy upsert idiom + Code Examples §Hold→Cleared Upsert |
| ING-06 | Chunked, resumable backfill in ≤30-day windows; `last_cursor` persisted so a crashed backfill resumes exactly where it stopped | D-05/D-06/D-07/D-08/D-09 + Architecture Patterns §Backfill Chunker + Code Examples §Backfill Window Math + Common Pitfalls §3 |
| ING-08 | Polling status surfaced in UI: last poll timestamp, last error, 401/429 distinguished | D-14/D-15/D-17 + Code Examples §Status Endpoint Query + Architecture Patterns §401-vs-429 Branching |

</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Scheduler tick / round-robin pick | API/Backend (APScheduler in FastAPI process) | — | Locked by D-04. Single `--workers 1` Uvicorn → exactly one scheduler instance per process. |
| Rate-limit budget enforcement | API/Backend (`RateLimitGate` over Postgres `FOR UPDATE`) | Database (state row) | Phase 1 invariant. Phase 2 reuses, never duplicates. |
| Mono HTTP fetch (statement, client-info) | API/Backend (`MonobankImporter`) | — | Existing seam. Phase 2 only changes how it's called, not what it does internally. |
| Hold→cleared upsert | Database (Postgres `INSERT ... ON CONFLICT DO UPDATE`) | API/Backend (`TransactionRepo.insert_many`) | Atomic at SQL level — race-free. The repo is the only seam; Phase 4/5/6 must NOT replicate this logic. |
| `import_runs` audit + cursor | Database (table) | API/Backend (`ImportRunRepo`) | D-08 makes this the canonical run-state. APScheduler's job store is NOT used for this — schedule ≠ audit. |
| `scheduler_state` singleton | Database (one-row table) | API/Backend (`SchedulerStateRepo`) | D-15 mandates persistence so a restart with bad token does not flood Mono with 401s. |
| Status surface (`GET /api/import/status`) | API/Backend (FastAPI route) | — | Read-only join over `accounts × import_runs × scheduler_state`. No caching needed. |
| Live polling vs Backfill prioritization | API/Backend (tick logic) | — | Pure dequeue logic. D-06 makes backfill mutually-exclusive-per-account, so the tick picks `pending` rows in `created_at ASC` order naturally. |

**Tier-correctness sanity:** No browser/frontend tier in Phase 2 (UI lands in Phase 6). The "API/Backend" rows above are all single-process Python; there is no web-tier vs app-tier split. All Postgres state is owned by the same connection pool.

## Standard Stack

### Core (additions over Phase 1)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| APScheduler | 3.11.2 | In-process scheduler; `AsyncIOScheduler` runs in the FastAPI event loop | CLAUDE.md / `research/STACK.md` already pinned this; v4 is alpha (4.0.0a6) and changes the API drastically (`Scheduler` + `add_schedule` instead of `AsyncIOScheduler` + `add_job`) — stay on 3.11.2 [VERIFIED: PyPI 2026-05-10 — `pip index versions apscheduler` returned 3.11.2 stable, 4.0.0a6 alpha] |

**Already-in-stack libraries Phase 2 leans on:**

| Library | Version | What Phase 2 uses it for |
|---------|---------|--------------------------|
| FastAPI | 0.136.1 | `lifespan` for scheduler start/stop; new `routes_status.py` and reshaped `routes_import.py` and new `routes_backfill.py` |
| SQLAlchemy | 2.0.49 | New `ImportRun` and `SchedulerState` ORM models; `postgresql.insert(...).on_conflict_do_update(...)` for `TransactionRepo.insert_many` |
| Alembic | 1.18.4 | One revision: `0002_phase2_sync.py` adds `accounts.mono_type`, creates `import_runs`, creates `scheduler_state`, seeds the singleton row |
| psycopg | 3.3.4 | Unchanged — driver still routes async traffic |
| httpx | 0.28.1 | `MonobankImporter` already uses; Phase 2 catches `HTTPStatusError` and branches on `response.status_code` for 401 vs 429 |
| structlog | 25.5.0 | New log keys `import_run_id`, `mono_type`, `scheduler_state`, `last_polled_at` ride the existing JSON pipeline; redaction processor already handles them generically |

### Supporting (new for Phase 2)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| (none) | — | — | Phase 2 introduces zero new top-level dependencies — APScheduler 3.11.2 was already pinned in research/STACK.md, just absent from the current `pyproject.toml` because Phase 1 didn't need it. The single new dep line is: `"apscheduler==3.11.2"`. |

### Alternatives Considered (and rejected — locked by CONTEXT.md / CLAUDE.md)

| Instead of | Could Use | Tradeoff (rejected because) |
|------------|-----------|------------------------------|
| APScheduler 3.11 in-process | Celery + Redis | Two extra containers for one job every 65 s. CLAUDE.md "What NOT to Use" rules this out. |
| APScheduler 3.11 in-process | Arq + Redis | Same broker overhead. CLAUDE.md rules out. |
| APScheduler 3.11 in-process | systemd timer + CLI | Loses shared state (last cursor, scheduler_state singleton). Breaks "single docker compose up". |
| APScheduler 3.11 in-process | Plain `asyncio.create_task` loop | Loses misfire/coalesce semantics for free. CONTEXT.md D-03 mandates `max_instances=1, coalesce=True`. |
| `ON CONFLICT DO UPDATE` for hold→cleared | Two-phase: SELECT then UPDATE | Race window between SELECT and UPDATE. Atomic SQL is the right tool. |
| `scheduler_state` row table | In-memory only | Fails D-15: a restart with bad `.env` token re-floods Mono with 401s within seconds. |
| `import_runs` queue table | APScheduler `SQLAlchemyJobStore` | APScheduler's job store is for *schedules* (when to fire), not run *audit* / *cursor* (what each run did). Conflating the two leaks scheduler internals into the data model. |
| `tenacity` retries on the gate | (none) | Deferred to v1.5 per CONTEXT.md. The gate's 65 s wait already serializes; transient errors mark `import_runs.status='error'` and the next tick tries the next pending row. |

**Installation:**
```bash
uv add apscheduler==3.11.2
```

**Version verification:** [VERIFIED: PyPI 2026-05-10] `curl -s https://pypi.org/pypi/APScheduler/json | jq -r .info.version` → `3.11.2`. Most recent stable release in the 3.x line. The 4.x line (`4.0.0a6`) is in alpha and renames `AsyncIOScheduler` → `AsyncScheduler`, `add_job` → `add_schedule`, etc. — do not adopt.

## Architecture Patterns

### System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Container: app  (--workers 1)                       │
│                                                                               │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │                    FastAPI lifespan (one process)                   │     │
│   │                                                                     │     │
│   │  startup:                                                           │     │
│   │    1. configure structlog                                           │     │
│   │    2. init_engine() ← Phase 1 unchanged                              │     │
│   │    3. SchedulerRunner.recover_in_flight()  ← sweep stale runs       │     │
│   │    4. SchedulerRunner.read_state() → if 'auth_failed', skip start   │     │
│   │    5. AsyncIOScheduler.add_job(tick, IntervalTrigger(seconds=10),    │     │
│   │                                 max_instances=1, coalesce=True)     │     │
│   │    6. scheduler.start()                                              │     │
│   │  shutdown:                                                           │     │
│   │    7. scheduler.shutdown(wait=False)  ← finish in-flight tick       │     │
│   │    8. await importer.aclose()                                        │     │
│   │    9. engine.dispose()                                               │     │
│   └────────────────────────────────────────────────────────────────────┘     │
│            │                                                                  │
│            │  every 10 s, single instance                                     │
│            ▼                                                                  │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │  tick() — async, idempotent under coalesce                          │     │
│   │                                                                     │     │
│   │  1. read scheduler_state (cached in process; re-read only on 401    │     │
│   │     recovery path which doesn't exist in v1)                        │     │
│   │     if state ∈ {'auth_failed','stopped'} → return                   │     │
│   │  2. SELECT * FROM import_runs WHERE status='pending'                │     │
│   │     ORDER BY created_at ASC LIMIT 1                                 │     │
│   │     ─ if found: claim_run(run.id) → fetch → upsert → mark done/err  │     │
│   │     ─ else: pick_next_active_card() → enqueue 1 'live' row → return │     │
│   │  3. claim_run = UPDATE import_runs SET status='in_flight',          │     │
│   │       started_at=now(), attempts=attempts+1                         │     │
│   │       WHERE id=:id AND status='pending'                             │     │
│   │       RETURNING *  ← serializes via the row update; tick is single- │     │
│   │       instance so SKIP LOCKED is unnecessary in v1                  │     │
│   │  4. fetch = importer.fetch_statement(account, window_from, window_to)│     │
│   │     ↓ inside: gate.acquire(token) ← 65 s wait if needed             │     │
│   │     ↓        httpx.AsyncClient.get(...)                             │     │
│   │     ↓        raise_for_status() → may raise HTTPStatusError         │     │
│   │  5. upsert = TransactionRepo.insert_many(account_id, items)         │     │
│   │     returns (inserted_count, updated_count) via xmax-=-0 RETURNING  │     │
│   │  6. mark done = UPDATE import_runs SET status='done',               │     │
│   │       completed_at=now(), statement_count=N, inserted=I,            │     │
│   │       last_error=NULL                                               │     │
│   │     mark error = UPDATE import_runs SET status='error',             │     │
│   │       completed_at=now(), last_error=str(exc)                       │     │
│   │     ─ on 401: ALSO write scheduler_state SET state='auth_failed'    │     │
│   │       and tick will short-circuit on the next fire                  │     │
│   │     ─ on 429: log, set last_status='rate_limited' on the account-   │     │
│   │       latest row, leave scheduler_state alone                       │     │
│   └────────────────────────────────────────────────────────────────────┘     │
│            │                                                                  │
│            ▼                                                                  │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │  Existing Phase 1 components (REUSED unchanged unless noted)        │     │
│   │                                                                     │     │
│   │   RateLimitGate ──────── unchanged (FOR UPDATE 65s)                 │     │
│   │   MonobankImporter ───── extended: discover_accounts emits          │     │
│   │                          mono_type; fetch_statement unchanged       │     │
│   │   AccountRepo ────────── extended: list_pollable_cards()            │     │
│   │   TransactionRepo ────── changed: insert_many uses                  │     │
│   │                          on_conflict_do_update (D-10)               │     │
│   │   ImportService ──────── extended: orchestration moves to           │     │
│   │                          SchedulerRunner; ImportService becomes     │     │
│   │                          a per-run helper                           │     │
│   └────────────────────────────────────────────────────────────────────┘     │
│            │                                                                  │
│            ▼                                                                  │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │  HTTP API (FastAPI routes; --workers 1)                             │     │
│   │                                                                     │     │
│   │   POST /api/import           — RESHAPED (D-16): 202 + {enqueued:[]} │     │
│   │   POST /api/backfill         — NEW: 202 + {run_ids:[]}              │     │
│   │   GET  /api/import/status    — NEW (D-14)                           │     │
│   │   GET  /api/transactions     — extended: TransactionOut.hold        │     │
│   │   GET  /api/accounts         — unchanged                            │     │
│   │   GET  /api/health           — unchanged                            │     │
│   └────────────────────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────┬───────────────────────┘
                                                         │
                              ┌──────────────────────────▼─────────────────────┐
                              │  Postgres 17 (existing container)               │
                              │                                                 │
                              │  accounts            ← + mono_type column       │
                              │  transactions        ← unchanged schema; new    │
                              │                        upsert SET clause        │
                              │  mono_rate_state     ← unchanged                │
                              │  import_runs         ← NEW                      │
                              │  scheduler_state     ← NEW (1-row singleton)    │
                              └─────────────────────────────────────────────────┘
                                                         │
                                                         ▼
                                            api.monobank.ua/personal/
                                            (1 req/60s per token, hard)
```

**Read this:** the data path is `lifespan → scheduler.tick → ImportRunRepo.claim_pending → MonobankImporter.fetch (gate-acquired inside) → TransactionRepo.insert_many (DO UPDATE) → ImportRunRepo.mark_done`. Manual `POST /api/import` enqueues `import_runs` rows and returns 202; the tick picks them up. There is no synchronous Mono call from any HTTP handler in Phase 2.

### Recommended Project Structure (additions over Phase 1)

```
src/finance_bro/
├── api/
│   ├── routes_status.py        # NEW — GET /api/import/status (D-14)
│   ├── routes_backfill.py      # NEW — POST /api/backfill (D-07)
│   ├── routes_import.py        # CHANGED — 202 enqueue (D-16)
│   ├── schemas.py              # CHANGED — TransactionOut.hold, ImportEnqueuedOut, ImportStatusOut, BackfillEnqueuedOut
│   └── deps.py                 # CHANGED — get_scheduler_runner(), get_import_run_repo(), get_scheduler_state_repo()
├── db/
│   ├── models.py               # CHANGED — Account.mono_type; ImportRun, SchedulerState ORM
│   ├── account_repo.py         # CHANGED — list_pollable_cards()
│   ├── transaction_repo.py     # CHANGED — insert_many uses on_conflict_do_update + xmax detection
│   ├── import_run_repo.py      # NEW
│   └── scheduler_state_repo.py # NEW
├── importers/
│   └── monobank.py             # CHANGED — discover_accounts emits mono_type
├── scheduler/                  # NEW package
│   ├── __init__.py
│   ├── runner.py               # SchedulerRunner — owns tick(), recover_in_flight(), pick_next_run()
│   ├── window.py               # backfill 12-month, 30-day-chunk window math
│   └── errors.py               # MonoAuthError (401), MonoRateLimitError (429), other
├── services/
│   └── import_service.py       # CHANGED — single-run helper now: run_one(import_run_id) instead of run_one_card()
└── main.py                     # CHANGED — lifespan starts/stops scheduler
alembic/versions/
└── 0002_phase2_sync.py         # NEW
```

### Pattern 1: APScheduler 3.x AsyncIOScheduler in FastAPI lifespan

**What:** Single `AsyncIOScheduler` started inside the FastAPI `lifespan` startup phase. The scheduler shares the running asyncio event loop with FastAPI; jobs are coroutines.

**When to use:** Always for finance-bro's scheduler — D-04 is locked. Single `--workers 1` Uvicorn process means exactly one scheduler instance per container.

**Example:**
```python
# src/finance_bro/main.py
# Source: APScheduler 3.x docs https://apscheduler.readthedocs.io/en/3.x/userguide.html
#         + project's existing lifespan shape (Phase 1 main.py)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    init_engine()

    runner = SchedulerRunner(
        session_factory=get_session_factory(),
        importer_factory=lambda: MonobankImporter(settings.mono_token, RateLimitGate(get_session_factory())),
    )
    await runner.recover_in_flight()           # sweep stale 'in_flight' rows
    state = await runner.read_state()
    scheduler = AsyncIOScheduler()
    if state.state == "running":
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
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)     # let in-flight tick finish; don't wait for next slot
        await runner.aclose()                   # close shared httpx client if owned at runner level
```

**Notes verified against APScheduler 3.x docs:**
- `AsyncIOScheduler` is class `apscheduler.schedulers.asyncio.AsyncIOScheduler`. [CITED: https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html]
- `add_job` accepts `func, trigger, id=, max_instances=, coalesce=, misfire_grace_time=`. Defaults in 3.x: `max_instances=1`, `coalesce=False`, `misfire_grace_time=1` second. Override `coalesce=True` to collapse missed ticks; `misfire_grace_time=30` is generous for a 10s tick. [CITED: https://apscheduler.readthedocs.io/en/3.x/userguide.html#missed-job-executions-and-coalescing]
- `IntervalTrigger(seconds=10)` is the canonical 10s trigger. [CITED: https://apscheduler.readthedocs.io/en/3.x/modules/triggers/interval.html]
- Default jobstore is `MemoryJobStore` — no `SQLAlchemyJobStore` is needed because Phase 2 *does not* persist schedules across restarts. The schedule is fixed (one tick job) and recreated on every lifespan startup. Persisted state lives in `import_runs` + `scheduler_state`, not in the APScheduler job store. [CITED: APScheduler 3.x default-config behavior]
- `scheduler.shutdown(wait=False)` is correct here: an in-flight tick will be canceled gracefully (the asyncio task gets cancelled), but we do not wait for the next scheduled fire. Using `wait=True` would block lifespan shutdown by up to 10s on every container stop. [VERIFIED: APScheduler 3.x source — `shutdown(wait)` with `wait=False` returns after canceling in-flight executors]

### Pattern 2: `import_runs` claim-and-execute (single-consumer)

**What:** The tick is the only consumer of `import_runs`. Manual `POST /api/import` and `POST /api/backfill` are *producers only*. Because there's exactly one tick instance at a time (`max_instances=1`), the dequeue can be a plain `UPDATE ... RETURNING` — no `SELECT FOR UPDATE SKIP LOCKED`, no advisory locks.

**When to use:** Always for Phase 2's tick. If Phase 5+ ever introduces a second consumer (e.g., a parallel backfill worker), revisit and switch to `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`. v1 explicitly does not need it.

**Example:**
```python
# src/finance_bro/db/import_run_repo.py
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

class ImportRunRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def claim_next_pending(self) -> ImportRun | None:
        """Atomic claim: transitions one pending row to in_flight and returns it.
        Single tick consumer means we don't need SKIP LOCKED in v1."""
        result = await self._s.execute(
            text("""
                UPDATE import_runs
                   SET status = 'in_flight',
                       started_at = now(),
                       attempts = attempts + 1
                 WHERE id = (
                     SELECT id FROM import_runs
                      WHERE status = 'pending'
                      ORDER BY created_at ASC
                      LIMIT 1
                 )
                 RETURNING *
            """)
        )
        row = result.mappings().one_or_none()
        return ImportRun(**row) if row else None
```

**Why no SKIP LOCKED:** the tick is `max_instances=1`. Two ticks cannot overlap. Two manual `POST /api/import` calls will both *insert* `pending` rows (they don't dequeue), and the tick will work them off in `created_at ASC` order. [VERIFIED: APScheduler 3.x `max_instances=1` semantics — second fire is dropped if first hasn't returned]

### Pattern 3: Hold → cleared `INSERT ... ON CONFLICT DO UPDATE`

**What:** Switch `TransactionRepo.insert_many` from `on_conflict_do_nothing(...)` to `on_conflict_do_update(..., set_={...EXCLUDED...})`. Use the partial unique index `uq_transactions_account_source_tx` with its `index_where=NOT is_deleted` predicate. Detect insert vs update via the `xmax = 0` trick on `RETURNING`.

**When to use:** Always for Mono importer writes. Phase 2 makes this the canonical path; Phases 4–7 must NOT bypass it.

**Example:**
```python
# src/finance_bro/db/transaction_repo.py
# Source: SQLAlchemy 2.0 PostgreSQL dialect docs
#         https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert
#         + Postgres "xmax=0 detects insert vs update" idiom (well-known but undocumented in PG core)
from sqlalchemy import column, text, literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.db.models import Transaction
from finance_bro.importers.base import CanonicalTransaction


class TransactionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_many(
        self,
        account_id: int,
        items: list[CanonicalTransaction],
    ) -> tuple[int, int]:
        """Upsert canonical transactions idempotently.

        On conflict, only `hold`, `amount_minor`, `raw_payload` mutate (D-10).
        Returns (inserted, updated_in_place).

        The xmax=0 trick: PostgreSQL's `xmax` system column is 0 on freshly-
        inserted rows; on rows mutated by ON CONFLICT DO UPDATE, xmax is set
        to the current transaction id. This is widely used [stack overflow
        canonical answer; PG hackers list] though not documented in PG core.
        """
        if not items:
            return (0, 0)
        rows = [
            {
                "account_id": account_id,
                "source_tx_id": t.source_tx_id,
                "amount_minor": t.amount_minor,
                "currency": t.currency,
                "time": t.occurred_at,
                "raw_payload": t.raw,
                # On first insert, importer is allowed to populate description/mcc;
                # they become immutable after the row exists (Phase 2 Discretion bullet 8).
                "description": getattr(t, "description", None),
                "mcc": getattr(t, "mcc", None),
                "hold": getattr(t, "hold", False),
            }
            for t in items
        ]
        stmt = insert(Transaction).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "source_tx_id"],
            index_where=text("NOT is_deleted"),
            set_={
                "hold": stmt.excluded.hold,
                "amount_minor": stmt.excluded.amount_minor,
                "raw_payload": stmt.excluded.raw_payload,
            },
        ).returning(
            Transaction.id,
            literal_column("(xmax = 0)").label("inserted"),
        )
        result = await self._s.execute(stmt)
        rows_back = result.all()
        inserted = sum(1 for r in rows_back if r.inserted)
        updated = len(rows_back) - inserted
        return (inserted, updated)
```

**Notes:**
- The `index_where=text("NOT is_deleted")` is required because the unique index is partial. Without it, Postgres refuses to use the index for conflict detection. [CITED: SQLAlchemy 2.0 docs — `on_conflict_do_update(index_where=...)`]
- `stmt.excluded.<col>` references the EXCLUDED virtual table holding the proposed row. [CITED: SQLAlchemy 2.0 docs]
- `xmax = 0` returns `true` for newly-inserted rows and `false` for updated rows. This is the canonical idiom. [CITED: well-known PostgreSQL pattern; not in PG official docs but corroborated across dozens of SO answers and the PG hackers list. WebFetch confirmed PG 17 docs do not officially document it but the technique works.] Marked HIGH because it has been reliable for >15 years across PG versions.
- A no-op upsert (same hold, same amount) still produces an UPDATE in PostgreSQL — the row is rewritten, `xmax` is non-zero. To distinguish "actually-changed updates" from "no-op updates", use `WHERE` in `on_conflict_do_update`: `index_where=text("NOT is_deleted") AND ...` — but Phase 2 does NOT need this distinction (D-10 freezes the field set; an unchanged update is harmless). Leave the optimization for v1.5 if `transactions.updated_at` ever gets added.

### Pattern 4: 401 vs 429 branching at the importer boundary

**What:** Both 401 and 429 raise `httpx.HTTPStatusError` after `raise_for_status()`. Distinguish by status code. Translate to typed exceptions at the *importer* boundary, not at the scheduler boundary — keeps the importer's contract precise.

**When to use:** Phase 2's `MonobankImporter` MUST raise typed errors. The scheduler tick catches them and updates `scheduler_state` and/or `import_runs` accordingly.

**Example:**
```python
# src/finance_bro/scheduler/errors.py
class MonoAuthError(Exception):
    """Raised when Mono returns 401. Sticky — sets scheduler_state='auth_failed'."""

class MonoRateLimitError(Exception):
    """Raised when Mono returns 429. Transient — surfaced per-call only."""
    def __init__(self, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Mono 429 (Retry-After={retry_after_seconds})")

class MonoTransientError(Exception):
    """Raised on 5xx / connect-timeout / read-timeout. Per-call import_runs.error;
    the next tick tries the next pending row."""
```

```python
# src/finance_bro/importers/monobank.py — addition to fetch_statement / discover_accounts
import httpx
from finance_bro.scheduler.errors import MonoAuthError, MonoRateLimitError, MonoTransientError

# inside discover_accounts() and fetch_statement(), wrap the raise_for_status:
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
```

**Empirical observation to log:** Phase 1's SUMMARY observed zero 429s. Phase 2's `MonoRateLimitError` should log `retry_after_seconds` whenever a 429 actually arrives — this is one of the open questions from STATE.md ("Mono 429 response: includes `Retry-After`?"). Keep the log structured (`scheduler.tick.mono_429 retry_after=...`) so a single grep across logs answers the empirical question.

### Pattern 5: `scheduler_state` singleton row

**What:** A one-row table `scheduler_state(id INTEGER PK CHECK (id = 1), state TEXT, last_error TEXT, since TIMESTAMPTZ)` enforced by a check constraint that prevents inserting any other id. Seeded by the migration.

**When to use:** D-15's sticky `auth_failed` flag must survive container restarts.

**Example (migration shape):**
```python
# alembic/versions/0002_phase2_sync.py — relevant section
op.create_table(
    "scheduler_state",
    sa.Column("id", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'running'")),
    sa.Column("last_error", sa.Text, nullable=True),
    sa.Column("since", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.CheckConstraint("id = 1", name="ck_scheduler_state_singleton"),
    sa.CheckConstraint(
        "state IN ('running', 'stopped', 'auth_failed')",
        name="ck_scheduler_state_state",
    ),
    sa.PrimaryKeyConstraint("id"),
)
op.execute("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')")
```

**Reads:** the lifespan reads it once at startup. The tick reads it from a process-local cached snapshot; only the 401-handling path writes it. After write, the in-process cache is updated and the next tick short-circuits. No per-tick read traffic against this table. [Pattern justification: D-15 says state can only transition to `auth_failed` from a 401; `auth_failed` cannot recover without restart; therefore tick has no reason to re-read.]

### Pattern 6: Backfill 12-month, 30-day-chunk window math

**What:** Walk newest-first from `now()`. Each chunk window = `[now() - (n+1)*30d, now() - n*30d)` for n in 0..11. Total = 12 chunks, 360 days of coverage.

**When to use:** Auto-trigger on first scheduler tick after boot for any active card with <30 days of history (D-05). Manual trigger via `POST /api/backfill?account_id=X&months=12`.

**Example:**
```python
# src/finance_bro/scheduler/window.py
from datetime import datetime, timedelta, UTC
from collections.abc import Iterator

MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000  # 31d + 1h — Mono cap (Pitfall 5)
MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30        # Operating chunk size (1h+ headroom)

def backfill_chunks(now: datetime, months: int = 12) -> Iterator[tuple[datetime, datetime]]:
    """Yield (window_from, window_to) tuples in newest-first order.

    For months=12, yields 12 chunks covering [now - 360d, now] in 30d slices.
    All math in UTC seconds; never multiply by 1000 (Pitfall 5 sub-point).
    """
    chunks = months  # 1 month ≈ 30 days for Mono windows; we don't try to align to calendar months
    for n in range(chunks):
        window_to = now - timedelta(days=n * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        window_from = now - timedelta(days=(n + 1) * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        yield window_from, window_to
```

**Edge cases documented:**
- **Calendar drift:** 30-day chunks do NOT align to calendar months. The "12 months" in the user-facing label is shorthand for "360 days". A backfill on 2026-05-10 covers down to 2025-05-15 (360 days back), not 2025-05-10. This is acceptable per Phase 2's UX — Bohdan never sees the chunk boundaries.
- **DST:** UTC math via `timedelta(days=...)` is DST-blind. Mono accepts UNIX seconds, so DST is irrelevant at the API boundary. [VERIFIED: existing Phase 1 importer already uses `int(since.timestamp())`, no DST concerns]
- **Month boundary inside a chunk:** harmless — Mono returns all transactions inside the window regardless of which calendar month they fall in. The `(account_id, source_tx_id)` partial unique index makes overlap idempotent.
- **Future-dated transactions:** none — Mono settles transactions at posting time; `time` is never in the future.

### Pattern 7: Recovery sweep on lifespan startup

**What:** On every container start, sweep `import_runs.status='in_flight'` rows older than 5 minutes back to `'pending'`. The 5-min threshold is "longer than any plausible single Mono fetch under the gate".

**When to use:** Always — locked by ARCHITECTURE.md §13 ("Crash mid-backfill chunk: any `running` run older than 5 min is marked `failed`; backfill resumes from `last_cursor`"). Phase 2 implements this for the first time (Phase 1 had no `import_runs`).

**Example:**
```python
# src/finance_bro/scheduler/runner.py
class SchedulerRunner:
    async def recover_in_flight(self) -> int:
        """Sweep stale in_flight runs back to pending. Returns count swept."""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(text("""
                UPDATE import_runs
                   SET status = 'pending',
                       started_at = NULL
                 WHERE status = 'in_flight'
                   AND started_at < now() - interval '5 minutes'
                RETURNING id
            """))
            count = len(result.scalars().all())
        if count:
            log.info("scheduler.recover.in_flight_swept", count=count)
        return count
```

**Why not "newer than 5 min stays in_flight":** the lifespan only runs at container start. If the container is alive, a `--workers 1` Uvicorn means there is exactly one tick at a time, and the recover sweep runs *before* the scheduler starts (per Pattern 1's startup ordering). So any `in_flight` row at boot is by definition stale; the 5-min check is defensive against clock skew, not a correctness requirement. [Justification: `max_instances=1` + single worker proves no concurrent tick can be writing to `in_flight` at boot.]

### Anti-Patterns to Avoid

- **Adding a second Mono caller seam.** The `RateLimitGate` is the only path. If you find yourself writing a `time.sleep` in the scheduler, or a second timestamp tracker, stop — the gate already does it. [Source: CONTEXT.md "specifics" §"Gate ownership is invariant"]
- **Using `SQLAlchemyJobStore` for `import_runs`.** APScheduler's job store is for *scheduling* (when to fire); `import_runs` is for *audit + cursor* (what each run did). Conflating them leaks scheduler internals into the data model and prevents Phase 2's manual-import flow from inserting `pending` rows directly. Use APScheduler's default `MemoryJobStore` and `import_runs` as a regular table.
- **Letting any HTTP route synchronously await Mono.** Phase 1's `POST /api/import` did this; Phase 2 reshapes to 202. Tests must NOT regress to assert synchronous behavior.
- **Allowing the importer to update `description`, `mcc`, `category_*`, `is_user_locked`, `is_deleted` on conflict.** D-10 freezes those by omission from the SET clause. The on-conflict SET clause must contain ONLY `hold`, `amount_minor`, `raw_payload`. Any addition is a bug.
- **Re-fetching `client-info` on every tick.** It's one rate-limit slot. Phase 2 does NOT add a refresh endpoint; discovery is one-shot per container life.
- **Multiplying Mono timestamps by 1000.** Mono uses Unix seconds. Phase 1 already does this correctly; Phase 2's backfill window math must follow the same convention.
- **Hand-rolling a token bucket in the scheduler.** Already exists as `RateLimitGate`.
- **Using `SELECT FOR UPDATE SKIP LOCKED` for `import_runs` claim.** Single tick consumer + `max_instances=1` makes this unnecessary. Adds complexity for zero benefit. If a future phase introduces a second consumer, revisit.
- **Catching `httpx.HTTPStatusError` in the scheduler tick directly.** Wrap in typed exceptions (`MonoAuthError`, `MonoRateLimitError`, `MonoTransientError`) at the importer boundary so the tick reads as branching on intent, not on HTTP status codes.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| In-process job scheduling | Custom `asyncio.create_task` loop with timer | `APScheduler 3.11.2` `AsyncIOScheduler` | misfire/coalesce/max_instances semantics for free; lifespan integration is canonical |
| Rate-limit token bucket | New impl in scheduler | Existing `RateLimitGate` (Phase 1) | Single-owner invariant; persists across restarts |
| Hold→cleared in-place update | Two-phase SELECT then UPDATE in Python | `INSERT ... ON CONFLICT DO UPDATE SET ... = EXCLUDED.*` | Atomic, race-free, single round trip |
| Insert-vs-update detection in one query | Application-side counting via separate SELECT | `xmax = 0` in RETURNING | Single round trip; widely-used PG idiom |
| Singleton config row | Application-level "first-row" check | One-row table with `CHECK (id = 1)` | DB enforces; impossible to accidentally write a second row |
| 30-day chunk window math | Calendar-month arithmetic via `dateutil.relativedelta` | Plain `timedelta(days=30)` × N | Mono accepts UNIX seconds; calendar alignment doesn't help, just adds DST/locale risk |
| 401-vs-429 detection | Substring match on response body | `e.response.status_code` after `raise_for_status()` | Status codes are the contract; httpx exposes them cleanly |
| Stale `in_flight` recovery | Per-tick "is this row too old?" check | Single recovery sweep at lifespan startup | Single-worker + `max_instances=1` makes per-tick check redundant |

**Key insight:** Phase 2 introduces *zero* new external dependencies beyond `apscheduler==3.11.2`. Every other concern (rate limit, idempotency, log redaction, DB session, async HTTP) reuses Phase 1 infrastructure. The new code is glue (SchedulerRunner, ImportRunRepo, SchedulerStateRepo, status route) and one Alembic migration.

## Runtime State Inventory

> Phase 2 is additive (new tables, new column, expanded behavior), not a rename or migration. This section is included for completeness — most categories are "None".

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | New: 12-row `import_runs` per backfill enqueue (auto-triggered on first tick after boot for fresh installs); 1-row `scheduler_state`. Existing `transactions` rows: their `hold` column was added by Phase 1's migration as `DEFAULT FALSE` and Phase 1 never wrote to it — Phase 2 begins to write it. No migration of existing rows needed; the next poll naturally populates `hold` correctly via the upsert. | Code: alembic 0002 creates tables + adds `accounts.mono_type` + backfills it from `raw_payload->>'type'` for existing rows |
| Live service config | None — APScheduler runs in-process with `MemoryJobStore`. No external service config. No n8n / Datadog / Tailscale tags involved. | None |
| OS-registered state | None — Phase 2 introduces no host-level cron, systemd unit, or task-scheduler entries. APScheduler is in-process. | None |
| Secrets/env vars | `MONO_TOKEN` unchanged from Phase 1 (env-only). No new env vars introduced. `LOG_LEVEL` unchanged. `DATABASE_URL` unchanged. | None |
| Build artifacts | Add `apscheduler==3.11.2` to `pyproject.toml`. Re-run `uv sync` regenerates `uv.lock`. Docker image rebuild required (Dockerfile unchanged). | `uv sync` to update lockfile; `docker compose build` on the next deploy |

**Nothing found in category — explicit:** No stale n8n workflows, no Tailscale ACL tags, no Windows Task Scheduler, no SOPS keys to rename, no compiled binaries with old names. This is a backend-only Python+SQL change.

## Common Pitfalls

### Pitfall 1: APScheduler tick re-fires while a previous tick is still inside `gate.acquire()` waiting

**What goes wrong:** Tick fires every 10 s. The gate may make a tick wait up to 65 s. If `max_instances` is left at the APScheduler 3.x default (which is 1 — confirmed below), this is a non-issue. If a future change bumps it to 2+ "for parallelism", two ticks contend for the gate; one wins and waits, the other waits behind it; the rate-limit budget is not violated (gate prevents that), but the queue behavior gets weird (multiple ticks waiting for the same `import_runs` claim).

**Why it happens:** APScheduler 3.x `max_instances` defaults to `1`. [VERIFIED: APScheduler 3.x docs §"Limiting the number of concurrently executing instances of a job"]. CONTEXT.md D-03 explicitly sets `max_instances=1, coalesce=True` — keep it that way.

**How to avoid:** Always pass `max_instances=1, coalesce=True` to `add_job`. Lock this in the lifespan code with a comment referencing D-03.

**Warning signs:** Multiple `scheduler.tick.start` log lines within a 65 s window — suggests `max_instances` is wrong.

### Pitfall 2: Hold→cleared payload arriving with same hold and same amount produces a no-op UPDATE that is invisible to `xmax=0`

**What goes wrong:** When a backfill window overlaps with a previous live poll and Mono returns the exact same already-cleared payload, the upsert produces an UPDATE (xmax ≠ 0), counted as "updated_in_place" even though nothing changed. The `last_poll_inserted` and `last_poll_updated` numbers in `/api/import/status` overstate activity.

**Why it happens:** PostgreSQL's `INSERT ... ON CONFLICT DO UPDATE SET col = EXCLUDED.col` always rewrites the row, regardless of whether the new value differs. `xmax` reflects the rewrite, not the field-level change.

**How to avoid:** Two options, ordered by simplicity:
1. **Accept it.** Phase 2's status surface is informational, not a correctness boundary. An overstated "updated" count is harmless.
2. **(v1.5+ if needed)** Add a `WHERE` clause to the `on_conflict_do_update`: `where=text("transactions.hold IS DISTINCT FROM EXCLUDED.hold OR transactions.amount_minor IS DISTINCT FROM EXCLUDED.amount_minor")`. This makes no-op updates skip; `xmax=0` still works.

**Warning signs:** `last_poll_updated` consistently equals statement_count even when no holds exist in the window. Indicates every payload triggers a no-op UPDATE.

**Default for Phase 2:** option 1 (accept). Keeps the SQL boring; the upsert remains a one-shape statement.

### Pitfall 3: Backfill walking past Mono's historical retention horizon

**What goes wrong:** ROADMAP and CONTEXT both specify "12 months". Mono's retention horizon is undocumented — community libraries report 12 months as the practical wall, but it is not in the official API docs (Context7 / WebFetch confirmed Mono docs do not specify retention). A backfill chunk for month 11 or 12 may return `[]` (valid) OR may 4xx (over-the-edge).

**Why it happens:** Mono's `/personal/statement` is documented to accept `from`/`to` UNIX seconds with a 31d+1h max window. Retention is a server-side policy, not an API contract.

**How to avoid:** Treat any 4xx during a backfill chunk as `import_runs.status='error'`, not as "no data". Empty array is "no transactions in window"; 4xx is "API rejected the request". The status surface (D-14) shows `last_error` per-account so this is observable.

**Warning signs:** `import_runs.last_error` shows 4xx for chunks beyond ~10 months. This is the empirical confirmation of Mono's retention horizon — log it loudly and document the wall once observed (resolves STATE.md Open Question #2).

**Phase 2 should:** specifically log structured fields `import_run.failed account_id=X window_from=... window_to=... status_code=...` so a single grep tells Bohdan where the wall is.

### Pitfall 4: 401 not sticky, scheduler restarts on bad token, floods Mono with 401s

**What goes wrong:** Without `scheduler_state` persistence, the in-memory `auth_failed` flag is lost on container restart. The container restarts, the `.env` token is still bad, the scheduler starts polling, every call returns 401, scheduler trips again — but only after burning rate-limit slots and producing log noise.

**Why it happens:** D-15 mandates persistence; without it, `auth_failed` is fragile to OOM, a `docker compose restart`, or a homelab reboot.

**How to avoid:** Pattern 5 (`scheduler_state` singleton row) — persist on every transition to `auth_failed`. Lifespan reads it before scheduler.start() and skips start if already `auth_failed`.

**Warning signs:** A second 401 in the logs within seconds of the first. If the second 401's `import_run_id` is different from the first, the sticky bit was not persisted.

### Pitfall 5: `import_runs` row count grows unboundedly

**What goes wrong:** Live polls every ~4 minutes (across active cards), 24/7, produces ~360 rows/day. After a year that's ~130k rows — small for Postgres, but every `GET /api/import/status` does a `SELECT MAX(completed_at) per account` which slows linearly without indexing.

**Why it happens:** D-08 doesn't specify a TTL or an index. ROADMAP/CONTEXT.md Phase 7 covers backups but not pruning.

**How to avoid:** v1: index `(account_id, run_kind, completed_at DESC)` on `import_runs` to make the status-page query trivial. Pruning (`DELETE WHERE completed_at < now() - interval '90 days'`) is deferred to Phase 7's operational closures.

**Warning signs:** `/api/import/status` taking >100 ms after a few months of uptime.

**Phase 2 should:** include the index in the migration. Specifically: `CREATE INDEX ix_import_runs_account_kind_completed ON import_runs (account_id, run_kind, completed_at DESC NULLS LAST)`.

### Pitfall 6: `xmax = 0` returning unexpected values when transaction is rolled back

**What goes wrong:** If the transaction wrapping `insert_many` rolls back, `xmax = 0` evaluation has already been computed and may be returned in the result before rollback. Calling code thinks rows were inserted; they weren't.

**Why it happens:** SQLAlchemy returns the result rows from the cursor before commit; rollback later doesn't retroactively change what was returned.

**How to avoid:** `insert_many` is called inside `session.begin()` blocks (existing Phase 1 pattern in `ImportService.run_one_card`). The caller commits before reading the counts. If the transaction rolls back, the caller never reads the counts. This is the existing pattern; no change needed. Just don't introduce a "pre-compute counts before commit" antipattern.

**Warning signs:** `import_runs.inserted` non-zero on a row whose status is `error`. If you see this, the rollback path is broken — the error path must NOT write `inserted` from a rolled-back transaction.

### Pitfall 7: `accounts.mono_type` NULL for existing Phase 1 rows

**What goes wrong:** Existing accounts (Phase 1 deployed) have `mono_type = NULL` until backfilled. The poll-set query (`WHERE mono_type IN ('black','platinum','white')`) excludes them — scheduler polls nothing, looks broken.

**Why it happens:** Adding a column does not populate it.

**How to avoid:** The 0002 migration MUST `UPDATE accounts SET mono_type = raw_payload->>'type' WHERE source_kind = 'mono.card'` in the same revision. The `raw_payload` JSONB has `type` for cards (per Mono client-info schema, verified above). For non-cards, `mono_type` stays NULL — fail-closed against jars/FOPs (D-01).

**Warning signs:** `SELECT mono_type, count(*) FROM accounts WHERE source_kind='mono.card' GROUP BY mono_type` shows NULL count > 0 after migration. If yes, the UPDATE didn't fire.

### Pitfall 8: Scheduler shutdown blocks lifespan exit by 65 s

**What goes wrong:** Calling `scheduler.shutdown(wait=True)` waits for the in-flight tick to finish. If that tick is mid-`gate.acquire()` waiting for the 65 s slot, lifespan blocks for up to 65 s on every container stop.

**Why it happens:** APScheduler `shutdown(wait=True)` is the default; it waits for executors to drain.

**How to avoid:** Pattern 1 specifies `scheduler.shutdown(wait=False)`. The in-flight tick's asyncio task is canceled; SQLAlchemy releases its connection; the gate's `FOR UPDATE` lock is released by the rollback. On next container start, the recovery sweep (Pattern 7) cleans up the abandoned `in_flight` row.

**Warning signs:** `docker compose stop` taking > 30 s. Suggests `wait=True` was used.

### Pitfall 9: Tests using `freezegun` to drive APScheduler — does not work

**What goes wrong:** `freezegun` patches `datetime.datetime.now`, but APScheduler 3.x uses `time.monotonic()` internally for the scheduler clock. `freezegun` does not patch `time.monotonic`. Tests that try to "fast-forward" the scheduler clock will fail.

**Why it happens:** APScheduler decoupled scheduling time from wall-clock time deliberately for accuracy.

**How to avoid:** Don't drive the scheduler from tests via clock manipulation. Test the `tick()` function directly as a coroutine — it's just `async def tick()`, and tests can `await runner.tick()` repeatedly. The 10s `IntervalTrigger` is only relevant in integration / E2E tests; unit tests bypass APScheduler entirely.

**Warning signs:** A test that sets `freeze_time("2026-05-10 12:00:00")`, calls `scheduler.start()`, advances time, and asserts a job ran. Will not work; rewrite to call `runner.tick()` directly.

**Phase 2 test architecture:** all unit tests for tick logic use `await runner.tick()`. The `time-machine` package (a freezegun replacement that DOES patch `time.monotonic`) could be used for integration tests if APScheduler timing matters, but the recommendation is: don't bother. The 10 s tick interval is not part of the contract — only "tick runs eventually" is.

### Pitfall 10: `mono_type` extraction silently NULL for unexpected card types

**What goes wrong:** A future Mono card type (`iron`, `gold`, `yellow`, `platina`) is added to a user's account. Discovery extracts `type='iron'` into `mono_type`, the allowlist `mono_type IN ('black','platinum','white')` excludes it, scheduler ignores the new card, no error surfaces.

**Why it happens:** The allowlist is fail-closed by design (D-01). This is a feature, not a bug — but it means Bohdan would not realize a new card is unmonitored.

**How to avoid:** Phase 2 status surface should include unallowlisted card types in `accounts[]` with `last_status='ignored_by_allowlist'` (or similar). User can grep status to see "this card exists but isn't being polled".

**Warning signs:** Bohdan reports a card not appearing in transactions; `GET /api/accounts` shows it; `GET /api/import/status` does not list it.

**Phase 2 should:** include all `mono.card` accounts in the status response — the schema in D-14 already does this (it does not filter by allowlist). Include `mono_type` in the row so the user can see "ah, this is type='iron', not in the allowlist".

## Code Examples

### Example 1: Hold → Cleared Upsert (the central correctness pattern)

```python
# Source: Pattern 3 above; SQLAlchemy 2.0 docs
#         https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert

from sqlalchemy import literal_column, text
from sqlalchemy.dialects.postgresql import insert
from finance_bro.db.models import Transaction

stmt = insert(Transaction).values(rows)
stmt = stmt.on_conflict_do_update(
    index_elements=["account_id", "source_tx_id"],
    index_where=text("NOT is_deleted"),
    set_={
        "hold": stmt.excluded.hold,
        "amount_minor": stmt.excluded.amount_minor,
        "raw_payload": stmt.excluded.raw_payload,
    },
).returning(
    Transaction.id,
    literal_column("(xmax = 0)").label("inserted"),
)
result = await session.execute(stmt)
rows_back = result.all()
inserted_count = sum(1 for r in rows_back if r.inserted)
updated_count = len(rows_back) - inserted_count
```

### Example 2: APScheduler Lifespan Integration (the start/stop pattern)

```python
# Source: Pattern 1 above; APScheduler 3.x docs
#         https://apscheduler.readthedocs.io/en/3.x/userguide.html
#         https://fastapi.tiangolo.com/advanced/events/

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    init_engine()

    runner = SchedulerRunner(
        session_factory=get_session_factory(),
        token=settings.mono_token,
    )
    swept = await runner.recover_in_flight()
    state = await runner.read_state()
    log.info("scheduler.lifespan.startup", state=state.state, swept=swept)

    scheduler = AsyncIOScheduler()
    if state.state == "running":
        scheduler.add_job(
            runner.tick,
            IntervalTrigger(seconds=10),
            id="finance-bro-tick",
            max_instances=1,           # CONTEXT.md D-03
            coalesce=True,             # CONTEXT.md D-03
            misfire_grace_time=30,
        )
        scheduler.start()
    app.state.scheduler = scheduler
    app.state.runner = runner
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)  # see Pitfall 8
        await runner.aclose()
```

### Example 3: Tick — the dequeue / fetch / upsert / mark-done pipeline

```python
# Source: Pattern 1 + Pattern 2 + Pattern 4

import structlog
from finance_bro.scheduler.errors import MonoAuthError, MonoRateLimitError, MonoTransientError

log = structlog.get_logger()

class SchedulerRunner:
    async def tick(self) -> None:
        if self._cached_state.state != "running":
            return

        run = await self._claim_next_pending()
        if run is None:
            await self._enqueue_next_live_poll()
            return

        log.info("scheduler.tick.run.start",
                 import_run_id=run.id,
                 account_id=run.account_id,
                 run_kind=run.run_kind,
                 window_from=run.window_from.isoformat(),
                 window_to=run.window_to.isoformat())
        try:
            account = await self._account_repo_get(run.account_id)
            items = [t async for t in self._importer.fetch_statement(
                account.source_account_id, run.window_from, run.window_to)]
            inserted, updated = await self._upsert(run.account_id, items)
            await self._mark_done(run.id, statement_count=len(items),
                                  inserted=inserted, updated=updated)
            log.info("scheduler.tick.run.done",
                     import_run_id=run.id,
                     account_id=run.account_id,
                     statement_count=len(items),
                     inserted=inserted,
                     updated_in_place=updated)
        except MonoAuthError as e:
            await self._mark_error(run.id, error=str(e))
            await self._set_state("auth_failed", last_error=str(e))
            log.error("scheduler.tick.auth_failed", import_run_id=run.id)
            # The next tick will short-circuit on cached_state.state == 'auth_failed'.
        except MonoRateLimitError as e:
            await self._mark_error(run.id, error=str(e))
            log.warning("scheduler.tick.mono_429",
                        import_run_id=run.id,
                        retry_after=e.retry_after_seconds)
        except MonoTransientError as e:
            await self._mark_error(run.id, error=str(e))
            log.warning("scheduler.tick.transient", import_run_id=run.id, error=str(e))
        except Exception as e:  # noqa: BLE001 — defensive top-level
            await self._mark_error(run.id, error=repr(e))
            log.exception("scheduler.tick.unexpected", import_run_id=run.id)
```

### Example 4: `GET /api/import/status` query

```python
# Source: D-14 + Pattern 5

from sqlalchemy import text

STATUS_QUERY = text("""
    WITH last_live AS (
        SELECT DISTINCT ON (account_id) account_id, completed_at, status,
               last_error, inserted, statement_count
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
    SELECT a.id AS account_id, a.source_account_id, a.mono_type,
           ll.completed_at AS last_polled_at,
           ll.inserted AS last_poll_inserted,
           ll.statement_count AS last_poll_statement_count,
           ll.status AS last_status,
           ll.last_error,
           coalesce(bp.remaining, 0) AS backfill_remaining,
           coalesce(bt.total, 0) AS backfill_total
      FROM accounts a
      LEFT JOIN last_live ll ON ll.account_id = a.id
      LEFT JOIN backfill_pending bp ON bp.account_id = a.id
      LEFT JOIN backfill_total bt ON bt.account_id = a.id
     WHERE a.source_kind = 'mono.card'
     ORDER BY a.id ASC
""")
```

The `WITH last_live` CTE uses Postgres `DISTINCT ON` to grab the most recent `live` run per account in one pass; cheap with the `(account_id, run_kind, completed_at DESC)` index from Pitfall 5's mitigation.

### Example 5: Alembic 0002 migration shape

```python
# alembic/versions/0002_phase2_sync.py
revision: str = "0002"
down_revision: str = "0001"

def upgrade() -> None:
    # 1. accounts.mono_type
    op.add_column("accounts", sa.Column("mono_type", sa.Text, nullable=True))
    op.execute("""
        UPDATE accounts
           SET mono_type = raw_payload->>'type'
         WHERE source_kind = 'mono.card'
    """)

    # 2. scheduler_state singleton
    op.create_table(
        "scheduler_state",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'running'")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("since", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("id = 1", name="ck_scheduler_state_singleton"),
        sa.CheckConstraint(
            "state IN ('running','stopped','auth_failed')",
            name="ck_scheduler_state_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')")

    # 3. import_runs
    op.create_table(
        "import_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger,
                  sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_kind", sa.Text, nullable=False),
        sa.Column("window_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_to", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("statement_count", sa.Integer, nullable=True),
        sa.Column("inserted", sa.Integer, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("run_kind IN ('backfill','live')", name="ck_import_runs_run_kind"),
        sa.CheckConstraint(
            "status IN ('pending','in_flight','done','error')",
            name="ck_import_runs_status",
        ),
    )
    op.create_index(
        "ix_import_runs_account_kind_completed",
        "import_runs",
        ["account_id", "run_kind"],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_import_runs_status_created",
        "import_runs",
        ["status", "created_at"],
        postgresql_using="btree",
    )

def downgrade() -> None:
    op.drop_index("ix_import_runs_status_created", table_name="import_runs")
    op.drop_index("ix_import_runs_account_kind_completed", table_name="import_runs")
    op.drop_table("import_runs")
    op.drop_table("scheduler_state")
    op.drop_column("accounts", "mono_type")
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `psycopg2` driver, `postgresql+psycopg2://` URL | `psycopg` v3 driver, `postgresql+psycopg://` URL | Phase 1 already migrated | None for Phase 2; just don't regress |
| `requests` (sync) for HTTP | `httpx.AsyncClient` | Phase 1 | None; reuse |
| `INSERT ... ON CONFLICT DO NOTHING` for first-write idempotency | `INSERT ... ON CONFLICT DO UPDATE SET <only mutable fields> = EXCLUDED.<col>` | Phase 2 (this) | Hold→cleared transitions in-place |
| APScheduler 3.10.x BlockingScheduler / AsyncIOScheduler | APScheduler 3.11.2 — same shape, bugfixes | 3.11.2 stable as of 2025-12 | None — 3.11 is API-compatible with 3.10 for this usage |
| APScheduler 3.x | APScheduler 4.x (alpha) — `Scheduler` + `add_schedule` instead of `AsyncIOScheduler` + `add_job` | 4.0 GA pending | DO NOT MIGRATE — alpha; rewrite cost; 3.x is supported indefinitely |
| `MERGE` SQL standard for upsert | `INSERT ... ON CONFLICT` Postgres-specific | PG 15+ supports MERGE; Postgres community still prefers ON CONFLICT for INSERT-OR-UPDATE single-table cases (better perf, simpler) | Stick with ON CONFLICT |

**Deprecated/outdated:**
- APScheduler 3.x's `executor='asyncio'` + `BackgroundScheduler` for asyncio apps — use `AsyncIOScheduler` directly (already chosen).
- `freezegun` for time-control in scheduler tests — use direct `await runner.tick()` (see Pitfall 9).
- Hand-rolled rate limiters — `RateLimitGate` already exists.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Mono retention horizon is ~12 months | Pitfall 3 | If Mono actually keeps less (e.g. 6 mo), backfill chunks 7–12 will produce 4xx errors. Phase 2 handles this gracefully (status='error' per chunk) so risk is "noisy log entries", not "data corruption". [ASSUMED — community libraries cite 12mo; not in official Mono docs; will be empirically confirmed in Phase 2 logs] |
| A2 | Mono `accounts[].type` for cards is one of `{black, platinum, white, eAid, iron, fop, yellow, platina, ...}` | D-01, Pitfall 10 | If a card is returned with a `type` outside the allowlist that Phase 2 doesn't anticipate, scheduler skips it (fail-closed by design). Empirically observed in Phase 1: `eAid, black, platinum, white` (4 of these). [ASSUMED — Mono docs do not enumerate the full set of `type` values; Phase 1 saw 4, community libs cite a few more; widening the allowlist is a one-line config change if needed] |
| A3 | Mono 429 response includes a `Retry-After` header (or does not) | Pattern 4, Pitfall §empirical | If Mono returns 429 without `Retry-After`, `MonoRateLimitError.retry_after_seconds = None` is correct; the gate's 65 s wait covers the next slot anyway. [ASSUMED — Phase 1 saw zero 429s; STATE.md flags this as an open question to resolve empirically in Phase 2 by logging the headers when one arrives] |
| A4 | `xmax = 0` reliably distinguishes inserts from updates in PostgreSQL 17 with `INSERT ... ON CONFLICT DO UPDATE` | Pattern 3, Pitfall 6 | If wrong, `inserted_count` and `updated_count` from `TransactionRepo.insert_many` would be inverted or noisy. [ASSUMED but HIGH-confidence — used in production by countless projects for >15 years across all PG major versions; Postgres core docs don't formally document it but the underlying MVCC semantics are stable] |
| A5 | Mono `statementItem.id` is per-account scope | Phase 1 D-04 invariant inherited; D-08 backfill assumes idempotency holds | If wrong (an `id` collides across accounts), `(account_id, source_tx_id)` partial unique index is still correct. [ASSUMED — Phase 1 didn't observe a collision; Phase 2's wider rotation should empirically confirm or break] |
| A6 | APScheduler 3.x `AsyncIOScheduler.shutdown(wait=False)` cancels in-flight tick coroutine cleanly without leaking DB connections | Pitfall 8 | If wrong, lifespan exit could leak a Postgres connection per stop/start cycle. [ASSUMED but HIGH-confidence — APScheduler 3.x calls `executor.shutdown(wait)` which for `AsyncIOExecutor` cancels the asyncio task; SQLAlchemy `AsyncSession.__aexit__` is invoked on cancellation and rolls back cleanly] |
| A7 | The 5-minute `in_flight` recovery threshold is "long enough that no live tick was in-flight, short enough that a real crash is recovered quickly" | Pattern 7 | If Mono ever takes > 5 min to respond (network partition + slow gate + slow recovery), a tick mid-flight could be marked 'pending' by a recovery sweep that fires shortly after, causing a duplicate execution. [ASSUMED — recovery sweep only fires at lifespan startup, not during steady-state, so this is structurally impossible unless the container is in a crashloop. Setting to 5 min for defensive margin] |

**If this table is empty:** N/A — there are 7 assumptions, mostly about Mono behavior that will be empirically confirmed during Phase 2 execution. None block planning.

## Open Questions

1. **Mono 429 Retry-After header shape**
   - What we know: Phase 1 observed zero 429s. The gate's 65 s wait should make 429 nearly impossible. Mono docs (Context7-fetched) document 429 as a status code but not the response headers.
   - What's unclear: When (if ever) Phase 2's wider rotation produces a 429, will the response include `Retry-After: <seconds>`?
   - Recommendation: Pattern 4 reads the `Retry-After` header opportunistically (`int(retry) if retry and retry.isdigit() else None`). If absent, `MonoRateLimitError.retry_after_seconds = None` and the gate's 65 s wait covers the next slot. Log the header value (or absence) on every 429 so Bohdan's logs answer the question empirically. [Resolves STATE.md Open Question]

2. **Mono historical retention horizon**
   - What we know: Community libraries cite 12 months. Mono docs (Context7-fetched) do not document retention.
   - What's unclear: Will backfill chunk 11 or 12 (months 11–12 ago) return `[]` (no data) or 4xx (over-the-edge)?
   - Recommendation: Treat 4xx as `status='error'` per chunk (D-08 + Pitfall 3). The status surface shows it. Bohdan's first 12-month backfill resolves the question; document the wall once observed. [Resolves STATE.md Open Question]

3. **Mono `statementItem.id` per-account vs global uniqueness**
   - What we know: Phase 1 polled one card and saw no collisions. Composite key `(account_id, source_tx_id)` is defensive regardless.
   - What's unclear: Does Mono ever emit the same `id` for different accounts (e.g. on jar transfers showing both legs)?
   - Recommendation: Phase 2's wider rotation across multiple cards is the natural empirical test. If a collision is observed, `(account_id, source_tx_id)` partial unique index correctly accepts both. Log structured `tx.upsert account_id=X source_tx_id=Y inserted=BOOL` so a single grep catches collisions. [Resolves STATE.md Open Question]

4. **Where exactly to keep the in-process `cached_state` for `scheduler_state`**
   - What we know: Pattern 5 specifies "process-local cached snapshot, write-only on 401, lifespan reads at startup". This is sufficient for v1.
   - What's unclear: If a future feature (manual scheduler restart endpoint) lands, the cache invalidation story gets harder.
   - Recommendation: Use a simple `dataclass` field on `SchedulerRunner` for v1 (`self._cached_state: SchedulerState`). Don't overcomplicate. If a manual-restart endpoint ever lands, refactor to "always re-read from DB at tick entry" — a one-line change.

5. **Test harness for the scheduler tick**
   - What we know: APScheduler-driven testing is awkward (Pitfall 9). The recommended approach is direct `await runner.tick()`.
   - What's unclear: Should integration tests bring up `AsyncIOScheduler` and let it tick, or also call `tick()` directly?
   - Recommendation: All Phase 2 tests should call `tick()` directly. If a Phase 2.5+ ever needs to validate the APScheduler IntervalTrigger config, write one smoke test using the real scheduler with a 100ms tick interval. For Phase 2: don't bother.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.13 | Backend | ✓ (Phase 1 verified) | per pyproject.toml | — |
| PostgreSQL 17 | All persistence | ✓ (Phase 1 verified — testcontainers + bind-mount in compose.yml) | 17-bookworm | — |
| `apscheduler` package | New for Phase 2 | ✗ (not yet in pyproject.toml; pinned in research/STACK.md) | will be 3.11.2 | None — install via `uv add apscheduler==3.11.2` |
| `httpx` | Mono fetch | ✓ | 0.28.1 | — |
| Docker / docker-compose | Deploy + tests (testcontainers) | ✓ (Phase 1 verified) | — | — |
| Internet → api.monobank.ua | Live polling | ✓ (Phase 1 SC#2 confirmed) | — | None for steady-state; tests mock via `respx` |

**Missing dependencies with no fallback:** None blocking. `apscheduler` is a one-command add.
**Missing dependencies with fallback:** None.

## Validation Architecture

> Nyquist validation enabled (config.workflow.nyquist_validation = true). This section is consumed by `gsd-validation-derivation` to produce VALIDATION.md.

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.x with pytest-asyncio 1.3 (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) — already configured |
| Quick run command | `uv run pytest tests/test_scheduler_round_robin.py -x` (per-task during execution) |
| Full suite command | `uv run pytest -x` (per-wave-merge and phase gate) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ING-05 | Hold transaction (`hold:true`) ingested with hold flag in DB and TransactionOut | unit | `uv run pytest tests/test_hold_cleared_upsert.py::test_hold_inserted_with_flag -x` | ❌ Wave 0 |
| ING-05 | Cleared payload (`hold:false`) with same `(account_id, source_tx_id)` UPDATEs in place — single row, mutated `hold`/`amount_minor`/`raw_payload`, frozen `is_user_locked`/`category_*` | unit | `uv run pytest tests/test_hold_cleared_upsert.py::test_cleared_updates_in_place -x` | ❌ Wave 0 |
| ING-05 | `TransactionOut.hold: bool` field present in `GET /api/transactions` response | unit | `uv run pytest tests/test_transactions_route.py::test_hold_field_in_response -x` | ❌ Wave 0 (extends existing file) |
| ING-06 | 12 backfill rows enqueued newest-first on first tick after boot for fresh card | unit | `uv run pytest tests/test_backfill_enqueue.py::test_twelve_chunks_newest_first -x` | ❌ Wave 0 |
| ING-06 | Killed mid-chunk: `in_flight` row swept back to `pending` on lifespan startup | integration | `uv run pytest tests/test_backfill_resumability.py::test_recover_in_flight_on_restart -x` | ❌ Wave 0 |
| ING-06 | Backfill chunks walk newest-first, persist per-chunk completion, resume from where stopped | unit | `uv run pytest tests/test_backfill_resumability.py::test_resume_picks_remaining_chunks -x` | ❌ Wave 0 |
| ING-06 | 30-day window math: 12 chunks × 30 days, all UTC seconds, no ms multiplication | unit | `uv run pytest tests/test_backfill_window_math.py -x` | ❌ Wave 0 |
| ING-06 | 4xx response inside backfill chunk → `import_runs.status='error'`, not silent skip | unit | `uv run pytest tests/test_backfill_resumability.py::test_4xx_marks_error_not_skip -x` | ❌ Wave 0 |
| ING-08 | `GET /api/import/status` returns the full D-14 shape | unit | `uv run pytest tests/test_import_status_shape.py::test_status_response_shape -x` | ❌ Wave 0 |
| ING-08 | Mono 401 → `scheduler_state.state='auth_failed'`, persisted across simulated restart | integration | `uv run pytest tests/test_401_stops_scheduler.py::test_401_persists_across_restart -x` | ❌ Wave 0 |
| ING-08 | Mono 429 → per-call `accounts[i].last_status='rate_limited'`, scheduler keeps running | unit | `uv run pytest tests/test_429_does_not_stop.py -x` | ❌ Wave 0 |
| ING-08 | `accounts[].last_polled_at` reflects last successful `live` run | unit | `uv run pytest tests/test_import_status_shape.py::test_last_polled_at_per_account -x` | ❌ Wave 0 |
| SC#1 | Round-robin across 3 active cards visits each within 3 ticks (mocked gate) | unit | `uv run pytest tests/test_scheduler_round_robin.py::test_three_cards_visited_three_ticks -x` | ❌ Wave 0 |
| SC#1 | Allowlist excludes eAid: tick never picks an `eAid` card | unit | `uv run pytest tests/test_scheduler_round_robin.py::test_eaid_skipped -x` | ❌ Wave 0 |
| SC#2 | 12-month backfill on fresh install enqueues 12 chunks per card and consumes them across ticks | integration | `uv run pytest tests/test_backfill_resumability.py::test_full_12_month_walk -x` | ❌ Wave 0 |
| SC#3 | Hold→cleared end-to-end: insert with hold:true, fixture re-fetch with hold:false, single row remains, fields per D-10 | integration | `uv run pytest tests/test_hold_cleared_upsert.py::test_e2e_hold_then_cleared -x` | ❌ Wave 0 |
| SC#4 | Status surface distinguishes 401 (banner state) from 429 (transient) | integration | `uv run pytest tests/test_import_status_shape.py::test_401_vs_429_distinguished -x` | ❌ Wave 0 |
| D-16 | `POST /api/import` returns 202 with `{enqueued: [{account_id, run_id}]}`, NOT a synchronous body | unit | `uv run pytest tests/test_force_poll_endpoint.py::test_returns_202_enqueued -x` | ❌ Wave 0 (REPLACES existing `tests/test_import_route.py` synchronous-body assertions) |
| Phase 1 invariants preserved | Composite idempotency on `(account_id, source_tx_id) WHERE NOT is_deleted` still holds; `is_user_locked` not overwritten by upsert | unit | `uv run pytest tests/test_partial_unique_index.py tests/test_idempotency.py -x` | ✅ existing |
| Phase 1 invariants preserved | RateLimitGate still owns the 65s cadence | unit | `uv run pytest tests/test_rate_limit_gate.py -x` | ✅ existing |
| Phase 1 invariants preserved | Log redaction still hides token / X-Token / amount values at INFO+ | unit | `uv run pytest tests/test_log_redaction.py -x` | ✅ existing |

### Sampling Rate

- **Per task commit:** `uv run pytest <focused-test-file> -x` — runs only the tests for the touched code in <5s.
- **Per wave merge:** `uv run pytest -x` — full suite must be green.
- **Phase gate:** Full suite green, plus a manual real-Mono smoke (analogous to Phase 1's 01-04 manual verification): start the container with a real `MONO_TOKEN`, observe `/api/import/status` populates after ~10s, observe a `live` `import_runs` row completes within 65s, and observe `GET /api/transactions` reflects new rows if any landed in the polling window.

### Wave 0 Gaps

- [ ] `tests/test_scheduler_round_robin.py` — covers SC#1 + D-01/D-02 allowlist & order
- [ ] `tests/test_backfill_enqueue.py` — covers ING-06 enqueue logic (D-05, D-08)
- [ ] `tests/test_backfill_resumability.py` — covers ING-06 + SC#2 resume + 4xx-as-error
- [ ] `tests/test_backfill_window_math.py` — covers `MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30` arithmetic
- [ ] `tests/test_hold_cleared_upsert.py` — covers ING-05 + SC#3 + D-10 frozen-fields invariant
- [ ] `tests/test_import_status_shape.py` — covers ING-08 + SC#4 + D-14
- [ ] `tests/test_401_stops_scheduler.py` — covers ING-08 + SC#4 sticky-401 (D-15)
- [ ] `tests/test_429_does_not_stop.py` — covers ING-08 + D-15 transient-429
- [ ] `tests/test_force_poll_endpoint.py` — covers D-16 (replaces synchronous-body assertions in `test_import_route.py`)
- [ ] **MODIFY** `tests/test_import_route.py` — remove synchronous-body assertions (`statement_count`, `inserted`, `skipped_duplicates`); replace with 202 + `{enqueued: [...]}` shape per D-16
- [ ] **MODIFY** `tests/test_transactions_route.py` — add assertion that `TransactionOut.hold` is present and reflects the DB value
- [ ] Framework install: `uv add apscheduler==3.11.2` — adds the only new top-level dep; if absent, the SchedulerRunner module won't import
- [ ] Test fixture: `tests/fixtures/statement_with_hold.json` — Mono `statementItem` payload with `hold: true` (extends existing fixture set)
- [ ] Test fixture: `tests/fixtures/statement_cleared_followup.json` — same `id` as above with `hold: false` and possibly different amount

## Project Constraints (from CLAUDE.md)

These are pulled from CLAUDE.md and constrain Phase 2 implementation in addition to CONTEXT.md decisions:

| Constraint | Source | Phase 2 Compliance |
|------------|--------|---------------------|
| **Privacy: no third-party cloud** | CLAUDE.md "Constraints" | ✓ APScheduler is in-process; no Redis/Celery/external broker |
| **Tech stack: Python backend** | CLAUDE.md "Constraints" | ✓ All Phase 2 code is Python |
| **Single `docker compose up`** | CLAUDE.md "Constraints" | ✓ No new services; APScheduler runs in the same `app` container |
| **Mono 1 req / 60s per token** | CLAUDE.md "Constraints" | ✓ `RateLimitGate` is the sole gate; D-03 explicitly defers cadence to it |
| **Single-user, no multi-tenancy** | CLAUDE.md "Constraints" | ✓ No `user_id` columns added; no auth |
| **Network-gated, no app-level auth** | CLAUDE.md "Constraints" | ✓ Same Tailscale/LAN trust boundary as Phase 1; `routes_status.py` mounts at `/api/*` with no auth |
| **APScheduler 3.11.2 in-process AsyncIOScheduler, single FastAPI worker** | CLAUDE.md "TL;DR Stack" | ✓ Pattern 1 follows verbatim |
| **psycopg 3 (`postgresql+psycopg://` URL), NOT psycopg2** | CLAUDE.md "Version Compatibility Notes" | ✓ Phase 1 already migrated; Phase 2 reuses |
| **Run with one Uvicorn worker** | CLAUDE.md "Why APScheduler over Celery" | ✓ Phase 1's `--workers 1` in compose.yml is preserved |
| **No `requests` (sync) for Mono** | CLAUDE.md "What NOT to Use" | ✓ httpx already in use |
| **No `psycopg2` / `psycopg2-binary`** | CLAUDE.md "What NOT to Use" | ✓ |
| **No Celery / RabbitMQ / Redis broker** | CLAUDE.md "What NOT to Use" | ✓ APScheduler in-process |
| **No floats for money** | CLAUDE.md "What NOT to Use" | ✓ `amount_minor BIGINT` invariant from Phase 1 preserved |
| **Money: minor units BIGINT + ISO-4217 alpha CHAR(3)** | CLAUDE.md "Money / Decimal Handling" | ✓ Phase 1 schema; Phase 2 doesn't touch |
| **`PRAGMA journal_mode = WAL` etc.** | CLAUDE.md (SQLite section) | N/A — Postgres 17 in use |
| **All Mono time math in seconds, not ms** | CLAUDE.md / PITFALLS.md #5 | ✓ `int(since.timestamp())` invariant preserved in `MonobankImporter`; backfill window math uses `timedelta(days=30)` (seconds-based) |

## Sources

### Primary (HIGH confidence)
- **APScheduler 3.x docs:** https://apscheduler.readthedocs.io/en/3.x/userguide.html — AsyncIOScheduler, `add_job` parameters, `max_instances`, `coalesce`, `misfire_grace_time`, `MemoryJobStore` default, `shutdown(wait)` semantics. [WebFetched 2026-05-10; partial — section on AsyncIOScheduler-specific code was sparse but flag semantics confirmed]
- **Context7 `/agronholm/apscheduler`:** v4-leaning documentation; cross-checked v3 patterns against the [3.x migration page](https://apscheduler.readthedocs.io/en/3.x/migration.html).
- **Context7 `/websites/api_monobank_ua`:** Mono Personal API endpoint specs — `/personal/statement/{account}/{from}/{to}` (1 req / 60s, 31d+1h max window), `/personal/client-info` (`accounts[].type` field is canonical, sample value `"black"` confirmed in OpenAPI spec response sample), 429 documented as a status code, full `StatementItem` schema with `hold` boolean field. [Fetched 2026-05-10]
- **PyPI APScheduler:** https://pypi.org/pypi/APScheduler/json — version 3.11.2 confirmed as latest stable (4.x is alpha). [Fetched 2026-05-10]
- **SQLAlchemy 2.0 PostgreSQL dialect:** https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert — `postgresql.insert(...).on_conflict_do_update(index_elements=, index_where=, set_=)` and `stmt.excluded.<col>` patterns. [WebFetched 2026-05-10]
- **PostgreSQL 17 INSERT docs:** https://www.postgresql.org/docs/17/sql-insert.html — RETURNING clause behavior with ON CONFLICT DO UPDATE confirmed; `xmax` system column is a documented MVCC column though the "xmax = 0 means inserted" idiom is not in PG core docs. [WebFetched 2026-05-10]
- **`.planning/phases/01-first-real-transaction/01-04-SUMMARY.md`** — empirical observations (Mono types `eAid, black, platinum, white`; zero 429s; rate gate held cleanly); the eAid landmine that drove D-01.
- **`.planning/research/PITFALLS.md` #4 / #5** — rate limit + naive retry loops; 31-day window backfill bugs; constants (`MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000`); time math in seconds.
- **`.planning/research/ARCHITECTURE.md` §13** — failure modes (crash mid-backfill: `running` > 5 min → `failed` → resume from `last_cursor`).
- **`.planning/research/STACK.md`** — APScheduler 3.11 already pinned; `add_job(...max_instances=1, coalesce=True, misfire_grace_time=120)` example.
- **Phase 1 Code:** `src/finance_bro/main.py`, `db/models.py`, `db/transaction_repo.py`, `importers/monobank.py`, `importers/rate_limit.py`, `services/import_service.py`, `api/routes_import.py`, `api/schemas.py` — all read for the seam locations Phase 2 extends.

### Secondary (MEDIUM confidence)
- `siomochkin/monobank-open-api-documentation` (community mirror, cited in research/STACK.md) — corroborates 1-req-per-60s rate limit and 31d+1h window. WebFetch returned 404 (repo may have moved); not blocking — Mono official docs via Context7 confirmed the same numbers.
- "xmax = 0" trick for distinguishing inserted vs updated rows in `INSERT ... ON CONFLICT DO UPDATE RETURNING` — widely-used Postgres idiom; corroborated by years of Stack Overflow and PG hackers list discussions; not in PG official docs but stable across all PG major versions including 17. Marked HIGH-confidence in body, MEDIUM here because of the "not officially documented" caveat.

### Tertiary (LOW confidence — flagged for empirical confirmation in Phase 2)
- Mono historical retention horizon "12 months" — community claim; will be confirmed by Phase 2 backfill logs.
- Mono 429 includes `Retry-After` header — Phase 1 saw zero 429s; Phase 2's wider rotation may surface one; pattern reads the header opportunistically.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — APScheduler 3.11.2 verified live on PyPI; SA 2.0 upsert idiom verified against official docs; psycopg/httpx unchanged from Phase 1.
- Architecture (lifespan + tick + claim + upsert + recovery): HIGH — every pattern is either canonical (APScheduler lifespan, SA on_conflict_do_update) or a direct application of the locked decisions in CONTEXT.md.
- Pitfalls: HIGH for ones grounded in Phase 1 empirics (eAid, rate gate behavior); MEDIUM for ones that depend on Mono behavior we haven't directly observed (429 shape, 12-month wall) — flagged in Assumptions Log.
- Backfill chunk math: HIGH — grounded in PITFALLS.md #5 constants; pure arithmetic.
- Validation Architecture: HIGH — all tests map to specific REQ/SC IDs and locked decisions.

**Research date:** 2026-05-10
**Valid until:** 2026-06-10 (30 days — APScheduler 3.x is stable; Mono Personal API hasn't broken backwards compat in years; SA 2.0 is at 2.0.49 LTS line)

---

## RESEARCH COMPLETE
