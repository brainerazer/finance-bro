# Phase 2: Reliable Sync - Context

**Gathered:** 2026-05-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Make the Mono importer trustworthy without manual intervention. Phase 1 ships a manual `POST /api/import` against the lowest-id card; Phase 2 turns that into automatic, multi-card round-robin polling at the rate-limit budget, plus a 12-month resumable backfill on first connect, plus in-place hold→cleared upsert, plus a status surface that distinguishes 401 (token revoked) from 429 (rate-limit) so silent failures are impossible.

This phase OWNS: APScheduler integration in the FastAPI lifespan; the `import_runs` table for backfill cursoring + run audit; the `mono_type` column on `accounts` (extracted from raw_payload at discovery); switching `TransactionRepo.insert_many` from `ON CONFLICT DO NOTHING` to `ON CONFLICT DO UPDATE` for the hold→cleared transition; the `GET /api/import/status` endpoint; reshaping `POST /api/import` to "force-poll all active cards" semantics; adding `hold: bool` to `TransactionOut`.

This phase does NOT TOUCH: FX rates / UAH rollup (Phase 3), categorization or rules (Phase 4), transfer/refund reconciliation (Phase 5), the dashboard or transaction-feed UI (Phase 6), backups or CSV import/export (Phase 7). The Phase 1 invariants are immutable: composite idempotency on `(account_id, source_tx_id) WHERE NOT is_deleted`, single token-bucket gate (Postgres `FOR UPDATE`, 65s cadence), log redaction at INFO+, BIGINT minor units + ISO-4217 alpha currency, env-only token (no DB row, no encryption).

</domain>

<decisions>
## Implementation Decisions

### Polling scope & round-robin

- **D-01 (poll-set):** Only `accounts` rows with `source_kind = mono.card` AND `mono_type ∈ {black, platinum, white}` enter the poll rotation. Concretely: persist Mono's `type` field as a new top-level `accounts.mono_type TEXT NULL` column (extracted from `raw_payload.type` at discovery time — currently it's only inside the JSON blob). The allowlist excludes the eAid charity card (Phase 1's empirical landmine) and is **fail-closed**: a future Mono card type (e.g. `iron`) does NOT auto-poll until the allowlist is widened. Jars (`source_kind = mono.jar`) and FOPs (`source_kind = mono.fop`) are persisted on discovery (Phase 1 D-05 stays) but the scheduler skips them entirely; they still appear in `GET /api/accounts`.
- **D-02 (order):** Round-robin order is `ORDER BY id ASC` over the allowlisted set. Deterministic, no extra state, new accounts join at the tail naturally as discovery upserts them. No activity-weighting, no skip-after-N-empty backoff in v1 — the allowlist is enough to keep the rotation tight.
- **D-03 (cadence):** The scheduler does **not** own the rate-limit timing — the Phase 1 `RateLimitGate` (65s, Postgres `FOR UPDATE`) does. APScheduler fires a single `poll_next_account` job at a tighter interval (10s) with `max_instances=1, coalesce=True`; the gate naturally serializes everything to one Mono call per 65s. With N active cards, a given card is polled every `N × 65s` (e.g., 4 cards → ~4.3 min, well inside SC#1's "~3 min" target for whichever card just got new activity, since that activity will land on the very next slot regardless of card position).
- **D-04 (lifecycle):** APScheduler's `AsyncIOScheduler` starts inside the FastAPI `lifespan` startup phase, after `init_engine()`. It stops on `lifespan` shutdown and on a sticky `auth_failed` state (D-15). No manual start/stop endpoint in v1 — `docker compose up -d` is the only way to (re-)start polling.

### Backfill orchestration

- **D-05 (trigger):** Backfill is **auto-triggered** on the first scheduler tick after boot. Trigger condition: an active card has fewer than ~30 days of historical transactions in the DB (treat the first ever-poll as "no history"). The scheduler enqueues 12 chunked `import_runs` rows for that card before any normal-poll rows. Manual trigger via `POST /api/backfill?account_id=X` is supported as an escape hatch but is NOT part of the happy path — Bohdan should never have to think about backfill.
- **D-06 (gate sharing):** While any `import_runs` row for an account has `status IN ('pending', 'in_flight')` AND `run_kind = 'backfill'`, the scheduler **skips normal polling for that account** (other accounts continue normally). One logical owner of the backfill queue at a time, per account. The gate still enforces the global 65s cadence so total throughput is unchanged; this just means we don't interleave a 'live' poll between backfill chunks for the same card.
- **D-07 (execution):** Backfill runs as APScheduler jobs (one job per `import_runs` row), not as a foreground HTTP call. `POST /api/backfill` (when used manually) returns `202 Accepted` with `{run_ids: [...]}` immediately. Progress is observable via `GET /api/import/status` (D-14). No HTTP socket is held for ~13 min.
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
  Backfill enqueues 12 `pending` rows with `run_kind='backfill'` walking newest-first in 30-day chunks (constant `MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30` to leave 1h headroom inside the 31d+1h Mono cap). Resume = `SELECT WHERE status != 'done' ORDER BY window_from DESC`. A killed-mid-chunk row stays `in_flight`; on restart it gets re-run; idempotent because the `(account_id, source_tx_id) WHERE NOT is_deleted` partial unique index swallows duplicates. Live polls also write a row (`run_kind='live'`, single ~now-65s..now window) which is what `/api/import/status` reads.
- **D-09 (window walk):** Newest-first per ROADMAP.md SC#2 ("walks ≤30-day windows newest-first"). The first chunk Bohdan sees populated is "this month"; the deepest one finishes last. If the user kills mid-backfill at month 6, the last 6 months are already visible in `GET /api/transactions`.

### Hold → cleared upsert semantics

- **D-10 (mutable fields):** `TransactionRepo.insert_many` switches from `ON CONFLICT DO NOTHING` to `ON CONFLICT (account_id, source_tx_id) WHERE NOT is_deleted DO UPDATE SET hold = EXCLUDED.hold, amount_minor = EXCLUDED.amount_minor, raw_payload = EXCLUDED.raw_payload`. **Only those three fields mutate.** `currency`, `time`, `account_id`, `source_tx_id`, `created_at` are frozen by omission. `is_user_locked`, `category_id`, `category_source`, `is_deleted`, `description`, `mcc`, `attributed_day` are left alone — Phase 1's Pitfall-10 promise that the importer never overwrites manual edits stays a hard invariant. The `inserted` count returned by `insert_many` becomes "rows affected" (inserts + updates); a new field `updated_in_place` tracks the upsert count. `skipped_duplicates` becomes effectively zero for hold→cleared transitions but still meaningful for already-final cleared rows that arrive again on backfill overlap (those become `UPDATE … WHERE hold IS DISTINCT FROM EXCLUDED.hold OR amount_minor IS DISTINCT FROM EXCLUDED.amount_minor` — i.e., row-not-changed counts as "skipped"; see Plan-stage decision on whether to filter at SQL or in Python).
- **D-11 (raw_payload):** The cleared payload **overwrites** the hold payload. No history table, no JSONB array. `import_runs` carries enough audit (which fetch returned this shape) to debug Mono quirks. The `transactions.raw_payload` is always "the latest Mono shape we saw"; the `hold` flag tells us if it's still pending.
- **D-12 (API shape):** `TransactionOut` gains a `hold: bool` field. `GET /api/transactions` returns ALL rows (cleared + held) in time-desc order; the client filters if needed. SC#3's "flagged as held" is satisfied by the `hold: true` field. SC#3's "excluded from spent totals" is a Phase 6 dashboard concern (UI-01) — Phase 2 just guarantees the schema flag is honest. Phase 1's existing `TransactionOut` shape is otherwise preserved (additive change, no breaking field renames).
- **D-13 (no audit columns):** No `prior_amount_minor`, no separate hold-history table. If a real Mono quirk surfaces, `import_runs` + re-fetch is the answer. YAGNI applies until a Phase 6 detail-drawer feature explicitly needs the delta.

### Sync status surface

- **D-14 (status shape):** `GET /api/import/status` returns:
  ```json
  {
    "scheduler": {
      "state": "running" | "auth_failed" | "stopped",
      "since": "2026-05-10T16:30:00Z",
      "last_error": null | "..."
    },
    "accounts": [
      {
        "account_id": 1,
        "source_account_id": "...",
        "mono_type": "black",
        "last_polled_at": "...",
        "last_poll_inserted": 3,
        "last_poll_updated": 0,
        "last_status": "ok" | "error" | "rate_limited",
        "last_error": null | "..."
      },
      ...
    ],
    "backfill": {
      "state": "idle" | "running",
      "runs_remaining": 0,
      "runs_total": 0,
      "eta_seconds": null
    }
  }
  ```
  Backed by joins on `accounts × import_runs` (last `live` row per account → last_polled_at; count of `pending`+`in_flight` `backfill` rows → backfill state). Cheap to compute; no caching needed in v1.
- **D-15 (401 vs 429):** **401** sets `scheduler.state = 'auth_failed'`, sets `scheduler.last_error = 'Mono token rejected (401)'`, **stops the APScheduler job permanently** until app restart (matches D-04's lifecycle). The token can only be rotated via `.env` + `docker compose up -d` (Phase 1 D-01) so retrying-without-restart is theatre. **429** is per-call: log it, set `accounts[i].last_status = 'rate_limited'`, do not stop the scheduler — the gate's `FOR UPDATE` already enforces the next-slot wait, and the next scheduled tick naturally lands in the next slot. 429 should be rare-to-impossible given the gate, but if observed (clock drift, manual-import races) it is treated as transient.
- **D-16 (manual import):** `POST /api/import` is **kept** but its semantics change: it enqueues an immediate live-poll for **every active card** (D-01's allowlisted set) by inserting `import_runs` rows with `status='pending', run_kind='live', window_from=last_polled_at-1h, window_to=now`. Returns `202 Accepted` with `{enqueued: [{account_id, run_id}, ...]}`. The actual fetch happens on the next scheduler tick (≤10s) and through the gate (≤65s further if the bucket is held). Phase 1's synchronous-blocking semantics are gone — the manual button is now an async hint, not a synchronous fetch.
- **D-17 (error history depth):** Status response carries last-error per account + last-error per scheduler only. The full history lives in `import_runs.last_error` and is reachable via psql for debugging. No "last 5 errors" array in the JSON response — keeps the payload bounded.

### Claude's Discretion

These framings the user did not select; Claude exercises judgment within the framing already established by Phase 1, the roadmap, and PROJECT.md/research:

- **Scheduler tick interval** — the APScheduler job fires every 10s with `max_instances=1, coalesce=True`. The actual rate-limiting comes from the gate (65s); the 10s tick is just "responsive enough that a freshly enqueued live-poll runs within 10s, not a full minute". Could be 5s or 15s without changing the contract; 10s is a Goldilocks default.
- **`mono_type` extraction** — at the `MonobankImporter.discover_accounts` boundary (where `numeric_to_alpha(currencyCode)` already lives), pull `acc.get("type")` for cards and stash it on `CanonicalAccount`. Jars don't have `type`; FOPs use the existing `mono.fop` source_kind; `mono_type` is NULL for those.
- **`accounts.mono_type` column migration** — single Alembic revision adds `accounts.mono_type TEXT NULL` and backfills it from `raw_payload->>'type'` for existing rows in the same migration (`UPDATE accounts SET mono_type = raw_payload->>'type' WHERE source_kind = 'mono.card'`). Indexed only if profiling shows the allowlist filter is slow — at single-user scale (~5 rows) it isn't.
- **`import_runs` migration** — single Alembic revision adds the table per D-08. No seeded data; the scheduler creates rows as it runs.
- **APScheduler job structure** — one `tick()` job at 10s interval that:
  1. Checks `scheduler.state` (in-memory or one-row config table — see below)
  2. If `auth_failed` or `stopped`, returns
  3. Picks the next `import_runs` row (`status='pending' ORDER BY created_at ASC LIMIT 1`)
  4. If none, picks the next active card whose last `live` `import_runs.completed_at` is oldest, enqueues a fresh `live` row, returns
  5. Acquires the gate, fetches, upserts via `TransactionRepo.insert_many`, updates `import_runs` row to `done`/`error`
  6. On 401 from any HTTP call: set in-memory `scheduler.state = 'auth_failed'`, persist to a small config table or singleton row, return
- **Scheduler state persistence** — one-row `scheduler_state(id INTEGER PK CHECK (id=1), state TEXT, last_error TEXT, since TIMESTAMPTZ)` so `auth_failed` survives restarts (otherwise a crashed-and-restarted container would happily re-poll with a known-bad token). New Alembic revision in the same migration as `import_runs`.
- **Gate already covers 429 path** — no new code needed; the gate's existing wait-then-write is enough. We just need to surface the wait to status (D-14's `last_status='rate_limited'`).
- **Hold → cleared `description`/`mcc` mutation policy** — even though D-10 freezes `description` and `mcc` on upsert, the importer is already free to populate them on **first insert** (when the row is newly created from a `hold:true` payload). Phase 2's importer should populate `description`, `mcc`, `attributed_day` on insert (forward-looking columns from Phase 1 are nullable; we're allowed to fill them). They become immutable only after the row exists. Phase 3 handles `attributed_day` fully; Phase 2 can either populate it provisionally (UTC-of-`time`) or leave NULL. **Default: leave NULL** — Phase 3 owns the timezone semantics; populating it now risks Phase 3 having to re-derive every row.
- **`POST /api/backfill` body** — accepts `{account_id?: int, months?: int = 12}` (both optional). Default = backfill all active cards 12 months. Phase 2 doesn't expose this in any UI; it's a debug/operator endpoint.
- **Round-robin starvation** — the "next card by oldest last-poll" picker (Discretion bullet 5 step 4) naturally rotates without explicit cursor state. New active cards (e.g., user gets a new black card) join the rotation on next discovery and pick up at the tail because their `last_polled_at` is NULL.
- **Discovery refresh** — Phase 1 D-06 made discovery one-shot. Phase 2 keeps that for the auto-path. A `POST /api/accounts/refresh` endpoint is **not** added in Phase 2; if a new card appears in Bohdan's life, manual restart re-discovers (lazy first-import path still triggers). Adding the refresh endpoint is a Phase 2.5 / pre-Phase-6 concern.
- **Tests** — Phase 1's testcontainers + httpx-mock harness extends naturally:
  - `test_scheduler_round_robin.py` — N accounts, fake gate, assert cards picked in id-asc order, eAid skipped.
  - `test_backfill_resumability.py` — start backfill, kill mid-chunk, restart, assert exactly remaining chunks run.
  - `test_hold_cleared_upsert.py` — insert `hold:true` row, re-call importer with `hold:false` + different amount, assert single row, mutated fields, frozen `is_user_locked`.
  - `test_import_status_shape.py` — assert all four states (running, auth_failed, rate_limited, backfill running) render correctly.
  - `test_401_stops_scheduler.py` — fake Mono returns 401, assert scheduler transitions to `auth_failed` and persists across simulated restart.
  - `test_force_poll_endpoint.py` — `POST /api/import` enqueues N `live` rows, returns 202, scheduler picks them up.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents (researcher, planner, executor) MUST read these before planning or implementing.**

### Project framing
- `.planning/PROJECT.md` — Core Value, constraints, key decisions, in-scope vs out-of-scope. Confirms: privacy, single-user, network-gated, no webhooks.
- `.planning/REQUIREMENTS.md` — v1 REQ-IDs and per-phase mapping. Phase 2 owns: **ING-05** (holds), **ING-06** (chunked resumable backfill), **ING-08** (status surface).
- `.planning/ROADMAP.md` — Phase 2 section: goal, success criteria, requirements, notes/risks (Pitfall 5 31-day window, Pitfall 23 401/429, hold→cleared idempotency).
- `.planning/STATE.md` — accumulated decisions and open questions (Mono `id` per-account scope, FOP token branching, 429 `Retry-After` shape — to resolve empirically in Phase 2).
- `CLAUDE.md` — full stack table, version compatibility (APScheduler 3.11.2 in-process AsyncIOScheduler, single FastAPI worker), what NOT to use (Celery, sync requests).

### Phase 1 (Phase 2's foundation — must not be broken)
- `.planning/phases/01-first-real-transaction/01-CONTEXT.md` — D-01 (env-only token), D-02 (POST /api/import contract — Phase 2 modifies this), D-04..D-06 (account discovery model — Phase 2 keeps), D-08 (sync HTTP shape — Phase 2 changes to async), D-10 (TransactionOut shape — Phase 2 adds `hold`).
- `.planning/phases/01-first-real-transaction/01-04-SUMMARY.md` — empirical observations: 5 cards (eAid + 2 black + platinum + white), no FOP seen, no 429s observed, eAid-as-first-card landmine that drove D-01.
- `src/finance_bro/db/models.py` — current schema. Phase 2 adds: `accounts.mono_type`, new `import_runs` table, new `scheduler_state` table.
- `src/finance_bro/db/transaction_repo.py` — `insert_many` switches DO NOTHING → DO UPDATE for D-10.
- `src/finance_bro/importers/monobank.py` — `discover_accounts` extracts `type` field; `fetch_statement` unchanged in shape.
- `src/finance_bro/importers/rate_limit.py` — `RateLimitGate` is reused unchanged. Every Phase 2 caller (scheduler, backfill, manual) routes through it.
- `src/finance_bro/services/import_service.py` — current `run_one_card` is replaced/extended with backfill-aware orchestration; the lazy-discovery branch is preserved for the cold-boot case.
- `src/finance_bro/api/routes_import.py` — `POST /api/import` semantics change per D-16; `GET /api/import/status` is new.
- `src/finance_bro/api/schemas.py` — `TransactionOut` adds `hold: bool`; new `ImportStatusOut` schema.
- `src/finance_bro/main.py` — `lifespan` adds APScheduler start/stop.
- `alembic/versions/0001_walking_skeleton.py` — Phase 1's first migration. Phase 2 adds new revision(s) on top.

### Research (HIGH confidence, dated 2026-05-10)
- `.planning/research/SUMMARY.md` — TL;DR; conflicts already resolved.
- `.planning/research/STACK.md` — APScheduler 3.11.2 confirmed; httpx 0.28.1; psycopg 3.3.4. Phase 2 introduces no new top-level dependency; APScheduler is already in `pyproject.toml` from Phase 1's stack pinning.
- `.planning/research/ARCHITECTURE.md` — modular monolith shape; `import_runs` table is mentioned as a canonical entity name (use it verbatim, not `imports` or `import_jobs`).
- `.planning/research/FEATURES.md` — Mono `statementItem.id`, `time` field, `hold` field shapes.
- `.planning/research/PITFALLS.md` — Phase 2 landmines: **#3 (hold→cleared upsert)**, **#4 (rate limit + naive retry loops)**, **#5 (31-day window backfill bugs — `MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000`, 30d chunks not 31d, all time math in seconds not ms)**, #11 (SQLite-on-NFS — already ruled out by Postgres).

### External (no auth required, fetch on demand)
- Monobank Open API: https://api.monobank.ua/docs/index.html — `/personal/statement/{account}/{from}/{to}` window, `hold` field, 401 vs 429.
- siomochkin/monobank-open-api-documentation: rate-limit confirmation (1 req/60s shared).
- APScheduler 3.x AsyncIOScheduler docs: https://apscheduler.readthedocs.io/en/3.x/userguide.html — `add_job(..., max_instances=1, coalesce=True)`, lifespan integration with FastAPI.
- httpx error model: 401 vs 429 are both `httpx.HTTPStatusError` after `raise_for_status()`; status code branching needed at the importer/service boundary.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`RateLimitGate`** (`src/finance_bro/importers/rate_limit.py`) — Postgres-backed `FOR UPDATE` 65s gate. Phase 2 reuses unchanged. Every fetch call (scheduler tick, backfill chunk, force-poll, discovery-refresh) routes through `gate.acquire(token)`. Single instance per process via `get_rate_gate()` dependency.
- **`MonobankImporter.discover_accounts`** — Phase 2 extends to extract `type` for cards into `CanonicalAccount.mono_type`. Otherwise unchanged.
- **`MonobankImporter.fetch_statement`** — unchanged in shape; Phase 2 calls it for both backfill chunks (12-month newest-first walk) and live polls (last-65s..now window). Note: returns an `AsyncIterator[CanonicalTransaction]`, so backfill is naturally streamable.
- **`TransactionRepo.insert_many`** — Phase 2 modifies the `ON CONFLICT` clause from `DO NOTHING` to `DO UPDATE SET hold=EXCLUDED.hold, amount_minor=EXCLUDED.amount_minor, raw_payload=EXCLUDED.raw_payload`. Returns adapted to expose both inserted and updated counts.
- **`AccountRepo.upsert_many`** — unchanged for Phase 2 (already idempotent on `uq_accounts_source`); `mono_type` added to the inserted dict.
- **Phase 1 testcontainers harness** (`tests/conftest.py` and friends) — Phase 2 piggybacks on it for all DB-bound tests. APScheduler tests use a fake clock (`apscheduler.schedulers.background.BlockingScheduler` with manual `wakeup` is fine for unit tests; integration tests bring up the real `AsyncIOScheduler` and tick once via a controlled job).
- **`structlog` redaction** (`src/finance_bro/core/logging.py`) — covers `import_runs.last_error` writes via the same processor; no new redaction work for Phase 2 unless a new log key is introduced (avoid logging full Mono payloads).

### Established Patterns
- **Importer port** (`ImporterProtocol`) at `src/finance_bro/importers/base.py` — Phase 2 adds no new importer; the protocol stands.
- **Repository pattern** — Phase 2 adds two new repos: `ImportRunRepo` and `SchedulerStateRepo`, both following the existing pattern (single `AsyncSession` constructor, `select`/`insert`/`update` methods, no SQLA leakage out of `db/`).
- **`Money(Decimal, currency)` value object at the application edge** — unchanged. Phase 2 doesn't add money-shaped fields.
- **`/api/*` mount with no prefix or middleware** (Phase 1 main.py) — Phase 2 mounts a fifth router, `routes_status.py`, the same way. No auth, no CORS — DEP-02 is the trust boundary.
- **Single FastAPI worker (`--workers 1`)** — REQUIRED for in-process APScheduler. Multiple workers would each instantiate a scheduler and race the gate. Compose CMD already pins `--workers 1` (Phase 1 verification).

### Integration Points
- **`lifespan` extension** — `src/finance_bro/main.py` `lifespan()` is the seam: after `init_engine()`, instantiate the APScheduler, register the `tick` job, `scheduler.start()`. On shutdown, `scheduler.shutdown(wait=False)` BEFORE the engine teardown so any in-flight gate transaction completes against a live engine.
- **Scheduler ↔ ImportService ↔ Importer wiring** — the scheduler tick instantiates an `ImportService` per tick from the same DI factory the API routes use (`get_session_factory()` + `get_importer()` analogues), or shares a process-wide service object. Recommendation for Plan: create a thin `SchedulerRunner` class that owns the tick logic and is injected with `(session_factory, importer, gate)` at lifespan startup.
- **`import_runs` <-> `transactions` relationship** — `import_runs` is an audit/cursor table. It does NOT FK to `transactions` (would create a 1-to-many with no useful join shape). Statement counts are stored as integers on the `import_runs` row.
- **`POST /api/import` reshape** — Phase 1's synchronous body returns `ImportResultOut` with concrete inserted/skipped counts. Phase 2's reshape returns `{enqueued: [{account_id, run_id}]}`. **This is a breaking change for the Phase 1 smoke-test** (`test_import_route.py`) — those tests need updating to assert the 202 + enqueued shape and follow up with a status poll.

</code_context>

<specifics>
## Specific Ideas

- **Phase 1's eAid landmine drives D-01.** The empirical observation in `01-04-SUMMARY.md` ("First-by-id account selection picked the eAid card on first run — empty 31-day statement window … Phase 2 should make 'which card to poll' configurable or skip eAid/inactive types") is the direct ancestor of the `mono_type` allowlist. Do NOT fall back to `source_kind = mono.card` filtering alone — that brings the eAid problem back.
- **30-day chunks not 31.** ROADMAP.md Pitfall 5: "chunk to ≤30 days, walk newest-first, treat any 4xx as error not absence. Constant-named `MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000`." Backfill code must define both `MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000` (the cap) and `MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30` (the operating chunk size, leaving 1h+ headroom). 4xx during a chunk → mark `import_runs.status = 'error'`, do NOT silently skip.
- **All Mono time math in seconds, not ms.** Phase 1 already does this in `MonobankImporter.fetch_statement` (`int(since.timestamp())`). Phase 2's backfill chunker uses the same idiom; never multiply by 1000. (Pitfall 5 sub-point.)
- **401 path is sticky and persistent.** Persist `scheduler_state.state='auth_failed'` to disk so a restart with the same bad `.env` token doesn't immediately re-flood Mono with 401s. The first call after restart must read `scheduler_state` BEFORE deciding to start polling. This is the "Mono support / token deactivation" guardrail from PITFALLS.md Pitfall 4.
- **Gate ownership is invariant.** The single `RateLimitGate` instance per process is the ONLY allowed Mono caller seam. Phase 2 introduces no second timer, no second `time.sleep`, no second tracked timestamp. If `import_runs` execution wants to know "when can I run", it asks the gate; it does not duplicate the logic.
- **Hold→cleared is the dominant correctness test.** SC#3 is testable end-to-end with two fixture payloads (same `id`, `hold:true` → `hold:false`, possibly different `amount`). After both runs: exactly one row in `transactions`, `hold = false`, `amount_minor` = cleared value, `raw_payload` = cleared payload, and (critical) `is_user_locked` / `category_*` untouched.
- **Open Questions to resolve empirically in Phase 2** (per STATE.md):
  1. Mono `statementItem.id` global vs per-account uniqueness — Phase 1 didn't observe a collision; Phase 2's wider polling rotation should empirically confirm or break the per-account scoping. Composite key remains the defensive contract regardless.
  2. Mono historical retention horizon — does the 12-month backfill walk into a Mono 4xx wall sooner than 12 months? Observe the first chunk that returns empty / errors during backfill on a real account.
  3. Mono 429 response shape — does the gate avoid 429s entirely (Phase 1 saw zero), or does the wider rotation surface them? If 429s do arrive, does the response include `Retry-After`? Capture it in `import_runs.last_error`.

</specifics>

<deferred>
## Deferred Ideas

- **Activity-weighted polling** — poll the user's main card more often than dormant ones. Considered, rejected for v1: the allowlist already trims the rotation enough that the simplest scheme (id-asc round-robin) meets SC#1. Revisit in v1.5 if a concrete cadence complaint emerges.
- **Skip-after-N-empty backoff per account** — adaptive cadence for cards that never return new transactions. YAGNI for v1; the allowlist handles the only known-empty case (eAid). Could be revisited if a fourth Mono card type ever sits dormant.
- **Manual `POST /api/accounts/refresh`** — re-fetch `client-info` to discover newly issued cards. Restart suffices for v1; explicit refresh is a Phase 2.5 / pre-Phase-6 concern.
- **`POST /api/scheduler/start` and `/stop` endpoints** — manual scheduler lifecycle controls. Not needed: D-04 makes start automatic and `auth_failed` is the only stop state in v1, recoverable only via .env edit + restart per D-15.
- **401 auto-retry hourly** — pinging `/personal/client-info` periodically to detect token recovery without restart. Moot under env-only token model (D-01 from Phase 1) — token cannot recover without a container restart.
- **Per-call retry policy with `tenacity`** — exponential backoff on transient httpx errors (network timeouts, 5xx). The gate makes this less urgent (next slot is 65s away anyway). Phase 2 can leave 5xx as `import_runs.status='error'` and let the next scheduler tick try again on the next slot. Add tenacity in v1.5 if churn becomes painful.
- **Per-chunk `prior_amount_minor` audit column** — debug history for hold→cleared deltas. YAGNI; `import_runs` covers it.
- **Hold-history JSONB array on `transactions.raw_payload`** — full audit of hold and cleared payloads. YAGNI; the current cleared payload is the source of truth.
- **`POST /api/transactions/pending` separate endpoint** — split holds onto their own URL. Combined endpoint with `hold` field is enough for Phase 2 and Phase 6.
- **Top-N error feed in `/api/import/status`** — last 5 errors per account for richer debugging in the UI. Phase 6 can join `import_runs` directly if it wants this; Phase 2 keeps the status JSON bounded.
- **Periodic `client-info` re-discovery** — re-poll account list on a slow schedule (daily?) to pick up new cards/jars without a restart. Coupled with the manual-refresh endpoint above; both deferred.
- **APScheduler v4 migration** — when v4 hits stable. Phase 2 pins 3.11.2 per CLAUDE.md and research/STACK.md.

### Reviewed Todos (not folded)

None — Phase 2's scope was clear from the roadmap; no STATE.md TODOs landed in this discussion.

</deferred>

---

*Phase: 02-reliable-sync*
*Context gathered: 2026-05-10*
