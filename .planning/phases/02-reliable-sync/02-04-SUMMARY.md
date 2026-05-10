---
phase: 02-reliable-sync
plan: 04
subsystem: api-surface
tags: [phase-02, status-surface, force-poll, backfill-route, d-16-reshape, d-14, d-07]

# Dependency graph
requires:
  - phase: 02-reliable-sync
    plan: 01
    provides: import_runs / scheduler_state schemas, ImportRunRepo (last_live_per_account), SchedulerStateRepo, conftest TRUNCATE extension, accounts.mono_type column
  - phase: 02-reliable-sync
    plan: 03
    provides: SchedulerRunner.enqueue_live_for_all_active_cards / enqueue_backfill, app.state.runner mounted in lifespan, get_scheduler_runner dep in api/deps.py
  - phase: 01-first-real-transaction
    provides: AccountOut.mono_type / TransactionOut.hold (extended in 02-02), conftest+testcontainers harness
provides:
  - "GET /api/import/status — D-14 single-document JSON: scheduler{state, since, last_error}, accounts[]{account_id, source_account_id, mono_type, last_polled_at, last_poll_inserted, last_poll_updated, last_poll_statement_count, last_status, last_error, backfill_remaining, backfill_total}, backfill{state, runs_remaining, runs_total, eta_seconds}"
  - "POST /api/import (D-16 RESHAPE) — 202 + ImportEnqueuedOut(enqueued=[{account_id, run_id}]); Phase 1 synchronous body (statement_count/inserted/skipped_duplicates/polled_account_id) GONE; 409 NoCardAccountFound path GONE"
  - "POST /api/backfill (D-07) — 202 + BackfillEnqueueOut(run_ids=[...]); body: BackfillEnqueueIn(account_id?, months=12 ge=1 le=36)"
  - "8 new Pydantic models in schemas.py: SchedulerStatusOut / AccountStatusOut / BackfillStatusOut / ImportStatusOut / ImportEnqueueRowOut / ImportEnqueuedOut / BackfillEnqueueIn / BackfillEnqueueOut"
  - "ImportResultOut (Phase 1) preserved in schemas.py — still used by ImportService.run_one_card; no longer referenced by routes_import.py (D-16 reshape removed it from the route)"
  - "main.py mounts 6 routers total: health, accounts, transactions, import, status, backfill"
  - "Phase 2 vertical slice complete: every Phase 2 success criterion (SC#1 auto-poll / SC#2 12-month resumable backfill / SC#3 hold→cleared in-place / SC#4 401/429 distinct) is now observable via the API"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Verbatim CTE for status-page aggregation (RESEARCH.md §4): WITH last_live + backfill_pending + backfill_total + outer SELECT against accounts; DISTINCT ON (account_id) on last_live. v1 surfaces last_poll_updated as constant 0 (D-14 deferred split — TODO v1.5 add updated_in_place column to import_runs)."
    - "Async-by-default mutation routes: POST /api/import + POST /api/backfill both return 202 + run_ids and let the scheduler tick consume them (no HTTP socket held). Mirrors the 'enqueue + drain' pattern from RESEARCH.md Pattern 1."
    - "Route reshape with semantic preservation: Phase 1's 409 NoCardAccountFound is replaced by 202 + {enqueued: []}. Steady-state truth is more useful than a misleading conflict — the route now reflects the runner's view (which discovers accounts in tick(), not at the route boundary)."
    - "Conftest client fixture extended to TRUNCATE BOTH before AND after to prevent explicit-id INSERTs in client tests from poisoning sibling tests using engine/session_factory directly (mirror of 02-03's per-test pattern at the conftest level)."
    - "Test pipeline migration: existing tests that called /api/import expecting synchronous body (test_idempotency, test_transactions_route._seed) updated to drive runner.tick() directly via client._transport.app.state.runner. APP_DISABLE_SCHEDULER=1 in tests means the scheduler is built but not auto-firing; manual tick drives end-to-end behavior deterministically."

key-files:
  created:
    - src/finance_bro/api/routes_status.py
    - src/finance_bro/api/routes_backfill.py
    - tests/test_import_status_shape.py
    - tests/test_force_poll_endpoint.py
  modified:
    - src/finance_bro/api/schemas.py
    - src/finance_bro/api/routes_import.py
    - src/finance_bro/main.py
    - tests/test_import_route.py
    - tests/test_idempotency.py
    - tests/test_transactions_route.py
    - tests/conftest.py

key-decisions:
  - "ImportResultOut Phase-1 schema is KEPT in schemas.py even though routes_import.py no longer references it. Reason: ImportService.run_one_card still returns it — Phase 1's manual-import pathway (used by test_idempotency's tick-driven seed and by 02-02 hold-cleared upsert tests) keeps the dataclass alive. Removing it would expand scope (have to refactor ImportService) for no benefit. Documented in plan_scope step 1."
  - "Option A from plan: rewrote tests/test_import_route.py to assert ONLY the 202+enqueued shape (3 tests instead of 4); end-to-end 'tick fetches and inserts' behavior is already covered by tests/test_scheduler_round_robin.py + tests/test_force_poll_endpoint.py side-effect assertions. Don't double-test."
  - "test_idempotency.py rewritten to drive runner.tick() directly after each POST /api/import. The user-visible single-row invariant (`len(r.json()) == 2`) — which IS what SC#3 promises — is unchanged. ON CONFLICT DO UPDATE (Phase 2 02-02) means the second tick touches both rows via UPDATE rather than skipping them via DO NOTHING; that distinction is already proven by test_hold_cleared_upsert::test_cleared_updates_in_place."
  - "test_transactions_route.py _seed helper rewritten to drive 2 ticks instead of relying on synchronous /api/import. Tick 1 is cold-boot discovery (no live row yet — the runner enqueues one); Tick 2 claims the live row and consumes it (fetches + upserts). End-to-end the same as Phase 1 (rows land in DB before /api/transactions is queried) but explicit about the async path."
  - "main.py docstring updated to drop the 'Plan 02-04 will mount the status surface...' forward reference (it's THIS plan now). The Phase-2 routers (status, backfill) are noted alongside the Phase-1 routers (health, accounts, transactions, import) — 6 routers total at /api/*."
  - "conftest client fixture: TRUNCATE BOTH before AND after the test. Caught when running Phase 1 regression suite — test_money_invariants does INSERT INTO accounts (...) WITHOUT explicit id, the sequence resets to 1 (RESTART IDENTITY), but a leftover id=1 row from test_idempotency's earlier client test caused a pkey collision. Same deviation class 02-03 documented for its session_factory-using tests; this is the corollary at the conftest level."
  - "STATUS_QUERY surfaces last_poll_updated as constant 0 (D-14 v1 simplification). The DB stores inserted+updated combined in import_runs.inserted; v1.5 may add a separate updated_in_place column. Not a correctness boundary — the status surface is informational, not a contract."
  - "Validation layer for BackfillEnqueueIn.months = Field(default=12, ge=1, le=36) — bounds the operator's worst-case enqueue (T-02-12 mitigation in plan threat_model: ~432 rows for 12 cards × 36 months, well under the index's O(log n) capacity). 12 is the v1 default per ING-06; 36 is the upper bound in case Mono retention proves longer than 12 months (CONTEXT.md Open Question 2 — empirical resolution post-deploy)."

patterns-established:
  - "Async-route-with-202: every Phase 2 mutation route (POST /api/import D-16, POST /api/backfill D-07) returns 202 immediately with run_ids and lets the scheduler tick consume the work. Routes never call importer methods directly — the runner mediates."
  - "Test-driven runner ticks: tests that need 'work flows through the system' inject `runner.tick()` calls between client.post() and observation. Mirror of 02-03's _make_runner pattern, adapted for the conftest's `client` fixture (use `client._transport.app.state.runner`)."
  - "DISTINCT ON CTE join for status pages: WITH last_live AS (SELECT DISTINCT ON (account_id) ... ORDER BY account_id, completed_at DESC NULLS LAST) — single-pass over import_runs filtered by run_kind, no subquery in the outer SELECT. The (account_id, run_kind) btree index from 02-01 keeps the DISTINCT ON cheap."
  - "Pitfall 10 surfacing: include allowlist-filtered cards (eAid) in status responses with mono_type populated so the user can see WHY a card isn't being polled. Filtering is for the scheduler, not for the user-visible state."

requirements-completed: [ING-05, ING-06, ING-08]

# Metrics
duration: ~18 min
completed: 2026-05-10
---

# Phase 02 Plan 04: Status Surface + D-16 Reshape Summary

**Bohdan can now hit GET /api/import/status and see one JSON document that answers every Phase-2 success-criterion question: scheduler running? per-card last poll N min ago? last error per card? backfill in progress? token still valid? POST /api/import is reshaped to 202+enqueued (D-16); POST /api/backfill (D-07) is the operator endpoint for the 12-month resumable backfill. main.py mounts 6 routers; Phase 2 vertical slice complete.**

## Performance

- **Duration:** ~18 min
- **Started:** 2026-05-10 (Task 1 commit `db1ef0d`)
- **Completed:** 2026-05-10 (Task 2 commit `32debb7`)
- **Tasks:** 2
- **Files touched:** 11 (4 created, 7 modified across `src/`, `tests/`)
- **Tests:** full suite green — 80 passed, 0 failed; 7 new tests across 2 new test files; 1 modified test file (test_import_route reduced from 4 → 3 tests after Phase 1 shape removal)

## Accomplishments

- **`schemas.py` ships 8 new Pydantic models** for the Phase 2 API surface, all additive (Phase 1's `ImportResultOut` is preserved for ImportService.run_one_card back-compat). Three nested status models (Scheduler/Account/Backfill) compose into ImportStatusOut; two enqueue-output models for the 202 responses; one input model with `Field(ge=1, le=36)` bounds checking on the months arg.
- **`GET /api/import/status` lands as the single read endpoint** Bohdan needs. Verbatim STATUS_QUERY CTE from RESEARCH.md §4, with `last_poll_updated` surfaced as constant `0` (v1 D-14 simplification — the DB stores inserted+updated combined). The route fetches the scheduler_state singleton via SchedulerStateRepo, runs the CTE join via `session.execute(text(STATUS_QUERY))`, builds AccountStatusOut rows, computes the aggregate BackfillStatusOut from per-account counts, and returns ImportStatusOut. ALL `mono.card` accounts surface — Pitfall 10: an eAid card is visible with `last_polled_at: null, mono_type: "eAid"` so the user can see why it's not being polled.
- **D-16 reshape lands cleanly.** `POST /api/import` is now 18 lines: takes a `Depends(get_scheduler_runner)` → calls `runner.enqueue_live_for_all_active_cards()` → returns `202` + `ImportEnqueuedOut(enqueued=[{account_id, run_id}])`. Phase 1's full body (`ImportService.run_one_card` + `NoCardAccountFound` 409 + structlog with `polled_account_id`/`statement_count`/`inserted`/`skipped_duplicates`) is GONE. Empty accounts table → 202 + `{enqueued: []}` instead of 409 (steady-state truth, not misleading conflict).
- **`POST /api/backfill` (D-07) — 22-line operator endpoint.** Takes `BackfillEnqueueIn` body (default `account_id=None, months=12`) and a `Depends(get_scheduler_runner)`; calls `runner.enqueue_backfill(account_id, months)`; returns 202 + `BackfillEnqueueOut(run_ids=[...])`. Pydantic bounds `months` to `1..36` so the worst-case enqueue is ~432 rows for 12 cards × 36 months — well within the (status, created_at) btree index's O(log n) capacity (T-02-12 plan threat_model mitigation).
- **`main.py` mounts both new routers** alongside the four Phase-1 routers. The lifespan logic itself is untouched (02-03 owns it); just two new `app.include_router(...)` calls + the imports.
- **7 new tests across 2 new test files**, all green:
  - `tests/test_import_status_shape.py` (4): full D-14 schema validation, per-account `last_polled_at` correctness across multiple runs per card, 401-vs-429 distinction (SC#4), idle-backfill state aggregation.
  - `tests/test_force_poll_endpoint.py` (3): 202+enqueued shape with side-effect verification (rows in `import_runs` are pending live), allowlist filters eAid (D-01 + D-16 — only black/platinum/white enqueue), empty-cards path returns `{enqueued: []}` not 409.
- **3 existing tests updated for the D-16 reshape:**
  - `tests/test_import_route.py` rewrote (4 tests → 3) to assert only the 202+enqueued shape; end-to-end "tick fetches and inserts" already covered by `test_scheduler_round_robin`.
  - `tests/test_idempotency.py` drives `runner.tick()` after each POST /api/import; SC#3 single-row invariant (`len(r.json()) == 2`) unchanged.
  - `tests/test_transactions_route.py` `_seed` helper drives 2 ticks (cold-boot discovery + live consume) instead of synchronous /api/import.
- **`tests/conftest.py` `client` fixture extended to TRUNCATE both before AND after** so explicit-id INSERTs in client tests don't poison sibling tests using engine/session_factory directly (caught running the Phase 1 regression — `test_money_invariants::test_amount_minor_is_bigint_signed` failed with `accounts_pkey` collision because a leftover id=1 row from a prior client test survived into the test_money_invariants run).

## Task Commits

1. **Task 1: schemas + status route + D-16 force-poll reshape + test_import_route rewrite + test_idempotency / test_transactions_route migration to runner.tick** — `db1ef0d` (feat)
2. **Task 2: backfill route + 7 new tests + main.py mount + conftest TRUNCATE-after** — `32debb7` (feat)

## Files Created/Modified

### Created (4)

- `src/finance_bro/api/routes_status.py` — `GET /api/import/status` returning ImportStatusOut. Verbatim RESEARCH.md §4 STATUS_QUERY CTE; last_poll_updated surfaced as constant 0 in v1.
- `src/finance_bro/api/routes_backfill.py` — `POST /api/backfill` returning 202 + BackfillEnqueueOut via `runner.enqueue_backfill(account_id?, months=12)`.
- `tests/test_import_status_shape.py` — 4 tests covering D-14 / ING-08 / SC#4 / Pitfall 10.
- `tests/test_force_poll_endpoint.py` — 3 tests covering D-16 (202 shape, allowlist filters eAid, empty-cards path).

### Modified (7)

- `src/finance_bro/api/schemas.py` — 8 new Pydantic models appended below ImportResultOut (kept for back-compat).
- `src/finance_bro/api/routes_import.py` — fully rewritten for D-16: 18 lines, returns 202 + ImportEnqueuedOut; no ImportResultOut / NoCardAccountFound / ImportService imports remaining.
- `src/finance_bro/main.py` — imports + mounts routes_status + routes_backfill; docstring updated to drop the "Plan 02-04 will mount..." forward reference.
- `tests/test_import_route.py` — 3 tests asserting only the 202+enqueued shape; end-to-end fetch+insert behavior covered elsewhere.
- `tests/test_idempotency.py` — drives `runner.tick()` after each POST /api/import; SC#3 invariant unchanged (`len(r.json()) == 2`).
- `tests/test_transactions_route.py` — `_seed` helper drives 2 ticks instead of relying on synchronous /api/import.
- `tests/conftest.py` — `client` fixture truncates BOTH before AND after each test; explicit code comment documents the rationale (prevents explicit-id INSERTs in client tests from poisoning engine/session_factory tests in other files).

## Decisions Made

See `key-decisions:` in frontmatter for the full list. Highlights:

- **D-16 reshape is breaking but the test surface absorbs it cleanly.** `tests/test_import_route.py`, `tests/test_idempotency.py`, and `tests/test_transactions_route.py` all needed updates because their original setup relied on the synchronous body shape. Each fix is small and surgical; each preserves the user-facing invariant the test was actually verifying (idempotency: `len(r.json()) == 2`; transactions route shape: `assert len(rows) == 2`).
- **`ImportResultOut` and `ImportService.run_one_card` are preserved.** Removing them would cascade into 02-02 / 02-03 territory (the upsert+adapter chain). The plan explicitly noted this as a Discretion bullet; the route just stops referencing the schema. v1.5 cleanup if desired.
- **Conftest TRUNCATE-after at the fixture level (not per-test-file).** 02-01 / 02-02 / 02-03 documented per-file `_truncate` autouse fixtures for tests that share global state; this is the same idea at the `client` fixture boundary so every client test gets clean teardown without the per-file boilerplate. The 5 wave-3 test files that already have per-file truncate-before-AND-after fixtures are unaffected (their fixtures don't conflict with the conftest's).
- **Idle-backfill case explicitly tested.** `test_status_idle_backfill_with_no_pending` covers the v1 most-common state (after the 12-chunk backfill drains, all subsequent live polls find idle). Without this test, the `running` ↔ `idle` toggle could regress silently.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — cascading-test-update from D-16 reshape] `tests/test_idempotency.py` and `tests/test_transactions_route.py` broke**

- **Found during:** Task 1 full-suite run after the route reshape.
- **Issue:** `tests/test_idempotency.py::test_second_import_is_noop` and `tests/test_transactions_route.py::test_response_shape` / `test_ordering_time_desc` all relied on POST /api/import doing the actual fetch+insert synchronously and returning the Phase 1 body. With D-16 the route only enqueues, so the respx mocks for `/personal/client-info` and `/personal/statement/...` were never hit, and the assertion `RESPX: some routes were not called!` fired.
- **Fix:** Both tests now drive `runner.tick()` directly via `client._transport.app.state.runner.tick()` after the POST. APP_DISABLE_SCHEDULER=1 means the scheduler doesn't auto-fire in tests, so manual tick drives the end-to-end behavior. Added `assert_all_called=False` to the respx context where appropriate (the redirect path no longer uses every mock for every test).
- **Files modified:** `tests/test_idempotency.py`, `tests/test_transactions_route.py`.
- **Verification:** Re-ran each file individually + the full suite — all green. `len(r.json()) == 2` invariant preserved in test_idempotency; transaction shape assertions preserved in test_transactions_route.
- **Committed in:** `db1ef0d` (Task 1).

**2. [Rule 1 — explicit-id INSERT leaks past client fixture boundary] `test_money_invariants::test_amount_minor_is_bigint_signed` failed with pkey collision when run after a sibling test_idempotency client test**

- **Found during:** Plan-level verification step 8 (Phase 1 regression).
- **Issue:** Same deviation class 02-03 SUMMARY documented (Deviation 1) for session_factory-using tests, but at the conftest `client` fixture level. The `client` fixture TRUNCATE-BEFORE'd accounts and re-seeded scheduler_state; my new test_force_poll_endpoint / test_import_status_shape (and the rewritten test_idempotency) INSERT explicit `id=1` for shape determinism. Without TRUNCATE-AFTER, the leftover id=1 row survived; the next test using engine/session_factory directly (test_money_invariants does `INSERT INTO accounts (...)` without an explicit id) saw the auto-increment sequence reset to 1 (RESTART IDENTITY) and tried to insert id=1 → `accounts_pkey` collision.
- **Fix:** Extended `tests/conftest.py` `client` fixture to TRUNCATE both before AND after the test, with the same SQL + reseed for scheduler_state. Documented the rationale inline in the fixture's docstring referencing 02-03's parallel deviation.
- **Files modified:** `tests/conftest.py`, `tests/test_idempotency.py` (reverted unnecessary explicit-id workaround once the conftest fix landed).
- **Verification:** Re-ran the full Phase 1 regression suite (`uv run pytest tests/test_health.py tests/test_no_auth.py tests/test_partial_unique_index.py tests/test_log_redaction.py tests/test_idempotency.py tests/test_money_invariants.py tests/test_schema_invariants.py tests/test_settings.py -x`) → 21 passed; full suite (80 passed). Specifically reproduced the original failure ordering (test_idempotency client → test_money_invariants engine) and confirmed it now passes.
- **Committed in:** `32debb7` (Task 2).

**3. [Rule 1 — false-positive grep gate] `<verification>` step 7's `(scheduler|accounts|backfill).*ImportStatusOut` returns 0**

- **Found during:** Plan-level verification step 7 (`grep -E "(scheduler|accounts|backfill).*ImportStatusOut" src/finance_bro/api/schemas.py | wc -l` ≥ 3).
- **Issue:** The grep pattern requires both a section keyword AND `ImportStatusOut` on the SAME LINE. In schemas.py the layout is:
  ```python
  class ImportStatusOut(BaseModel):
      scheduler: SchedulerStatusOut
      accounts: list[AccountStatusOut]
      backfill: BackfillStatusOut
  ```
  Three nested fields on three separate lines under one class declaration. The grep expression therefore returns 0 — the actual structural invariant ("ImportStatusOut has scheduler+accounts+backfill") is satisfied, but the literal-text check is faulty.
- **Fix:** None required — the architectural invariant the gate was trying to verify is satisfied (verified by direct inspection: lines 88-91 of schemas.py contain the exact 3-field nesting). Same deviation class 02-01 / 02-02 / 02-03 logged for similar plan-checker grep-gate false positives.
- **Files modified:** none.
- **Note for future plans:** Plan-level grep gates that span multiple-line constructs (e.g. Python class bodies) need a multiline regex (`-Pz`) or a different verification (e.g. an `import` + `assert hasattr` check). Same recommendation 02-03 SUMMARY made.

---

**Total deviations:** 3 (2× Rule 1 cascading-test-fix from the D-16 reshape, 1× Rule 1 grep-gate false-positive documentation). No architectural decisions, no Rule 4 escalations.
**Impact on plan:** None on the shipped artifacts. The cascading-test fixes are the natural consequence of a documented breaking change (D-16); the conftest TRUNCATE-after extension makes the test isolation contract more explicit and eliminates a class of cross-test pkey collisions for future plans.

## Issues Encountered

- The grep-gate false-positive class showed up FOUR times in Phase 2 plans now (02-01 deviation 2, 02-02 deviation 1, 02-03 deviation 3, 02-04 deviation 3). The plan-checker's grep-gates should evolve toward AST-aware checks or multiline patterns; literal-text regexes against class bodies and docstrings have a high false-positive rate.
- The D-16 reshape's blast radius reached 3 existing tests (test_idempotency, test_transactions_route × 2). Each fix was small but the iteration cost was real — running the full suite, observing the new failures, mapping each to the synchronous→async change, rewriting against `runner.tick()`. Worth flagging in PATTERNS.md when 02-04 wraps: "any test that called /api/import expecting synchronous body must drive runner.tick() after POST in Phase 2+."

## Empirical Observations

- **`runner.tick()` driving from a `client` fixture is reliable.** `client._transport.app.state.runner` returns the same SchedulerRunner instance the lifespan built; calling `.tick()` exercises the full claim_next_pending → fetch_statement → insert_many → mark_done pipeline. No event-loop ownership conflicts.
- **`assert_all_called=False` on respx is the right choice for tests that drive ticks.** The cold-boot discovery path skips the `/personal/client-info` mock when accounts are pre-seeded; the live-fetch path skips the `/personal/statement/...` mock when the seeded run window is empty. Forcing all-called would be a flake source.
- **`literal_column("(xmax = 0)")` from 02-02 + `import_runs.inserted` from 02-01 still flow cleanly into the runner's `mark_done(inserted=...)` call.** The runner's structlog event reports `inserted` and `updated_in_place` separately; the status surface coalesces them into `last_poll_inserted` (with `last_poll_updated=0` constant in v1). v1.5 may split them.
- **Pitfall 10 is observable end-to-end.** With an eAid card seeded, `GET /api/import/status` shows `accounts[i].mono_type == "eAid"` and `last_polled_at == null` — the user can SEE why it's not being polled, instead of being mystified by a missing card. Tested by `test_status_response_shape`.
- **The CTE join is fast enough on test data.** All 4 status-shape tests complete in <0.5s collectively; the (account_id, run_kind) btree index from 02-01 keeps the DISTINCT ON cheap. Production verification once Bohdan's NAS has months of import_runs data.

## Phase 2 Closure — Success Criteria Status

All four Phase 2 success criteria are now observable through the API (end-to-end testable on a real `docker compose up`):

- **SC#1 (auto-poll):** `GET /api/import/status` shows `accounts[i].last_polled_at` advancing without user action; the scheduler tick (10s) + RateLimitGate (65s) drives it. Verified by `test_scheduler_round_robin` (02-03) + `test_last_polled_at_per_account` (this plan).
- **SC#2 (12-month resumable backfill):** `POST /api/backfill` enqueues 12 chunks per active card; `GET /api/import/status` shows `backfill.runs_remaining` decreasing as ticks consume them; restart resilience proven by `test_backfill_resumability::test_recover_in_flight_resets_stale` (02-03). 
- **SC#3 (hold→cleared in-place):** ON CONFLICT DO UPDATE upsert (02-02) + runner's fetch path (02-03) produces a single row that `GET /api/transactions` returns with `hold` reflecting the latest payload. Proven by `test_hold_cleared_upsert::test_cleared_updates_in_place` (02-02 — central correctness test).
- **SC#4 (401/429 distinct):** `GET /api/import/status` renders `scheduler.state='auth_failed'` for 401 and leaves it `'running'` for 429 with the per-account `last_error` carrying the 429 detail. Proven by `test_401_vs_429_distinguished` (this plan) + `test_401_persists_across_restart` (02-03) + `test_429_does_not_stop` (02-03).

## Open Questions Carried Into Production

These flagged in CONTEXT.md / RESEARCH.md and inherited from 02-03 SUMMARY are NOT resolved by this plan — they require empirical observation against the real Mono API once Bohdan starts running the app on his NAS. Documented here for completeness:

1. **`statementItem.id` global vs per-account uniqueness** (CONTEXT.md Open Question 1). Production check: `SELECT source_tx_id, count(DISTINCT account_id) FROM transactions GROUP BY source_tx_id HAVING count(DISTINCT account_id) > 1` should return 0 rows after a few weeks of polling.
2. **Mono historical retention horizon** (CONTEXT.md Open Question 3). Status surface will reveal this naturally: the deepest backfill chunk's `last_status='error'` with a 4xx in `last_error` would mark the retention boundary.
3. **Mono 429 Retry-After header presence** (CONTEXT.md Open Question 5). Both code paths handle the header being absent; observability via structlog `scheduler.tick.mono_429.retry_after` shows what Mono actually sends.

## Threat Flags

None. The new endpoints (GET /api/import/status, POST /api/backfill) sit at the same trust boundary (DEP-02 — Tailscale/LAN) as Phase 1's routes. No new auth paths, no new file-access patterns, no new persistent fields. The threat model in the plan's `<threat_model>` is fully mitigated:

- T-02-01 (Status JSON could leak Mono payload via last_error): runner's `_mark_error` only writes typed-error strings (`"Mono 401"`, `"429 (Retry-After=60)"`, `"Mono 500"`). No raw Mono response bodies. Verified by inspecting `scheduler/runner.py::_mark_error` call sites — every string is either `repr(exc)` or a hand-built status-only message.
- T-02-12 (DoS via unauthenticated POST /api/backfill): `months: int = Field(default=12, ge=1, le=36)` Pydantic-bounded; trust boundary is Tailscale/LAN.
- T-02-13 (Tampering via POST /api/import returning success-shape but doing nothing): intentional — `{enqueued: []}` is the steady-state truth, more useful than Phase 1's misleading 409.
- T-02-14 (Repudiation via empty scheduler_state): defensive fallback to `state='running'` in routes_status.py if SchedulerStateRepo.read() returns None; migration 0002 seeds the row, conftest re-seeds post-truncate, fallback covers a future migration that drops the seed.

## Next Plan Readiness

- **Phase 2 is COMPLETE.** All four success criteria are observable via the API. ING-05 / ING-06 / ING-08 are deliverable. The four route handlers (routes_status, routes_backfill, routes_import, routes_transactions) coexist; main.py mounts 6 routers total at `/api/*`.
- **Phase 3 (FX rates) is unblocked.** The status surface, backfill path, and live-poll path are independent of FX — Phase 3 will add `/api/fx_rates` reads + the NBU daily-fetch APScheduler job. The existing scheduler patterns from 02-03 are directly reusable.
- **Phase 6 (UI) inputs are all in place.** The dashboard will consume `/api/accounts` (mono_type), `/api/transactions` (hold), `/api/import/status` (the full D-14 shape — banner state, per-card last-poll-N-min-ago, backfill progress bar). The ImportEnqueuedOut + BackfillEnqueueOut shapes are ready for the "Refresh now" / "Backfill 12 months" buttons.

## Self-Check: PASSED

Verified files exist on disk:
- `src/finance_bro/api/routes_status.py` — FOUND
- `src/finance_bro/api/routes_backfill.py` — FOUND
- `tests/test_import_status_shape.py` — FOUND
- `tests/test_force_poll_endpoint.py` — FOUND
- `src/finance_bro/api/schemas.py` — FOUND (modified, contains the 8 new models)
- `src/finance_bro/api/routes_import.py` — FOUND (modified, D-16 reshape — 202 + ImportEnqueuedOut)
- `src/finance_bro/main.py` — FOUND (modified, mounts routes_status + routes_backfill)
- `tests/test_import_route.py` — FOUND (modified, 3 tests asserting 202+enqueued shape)
- `tests/test_idempotency.py` — FOUND (modified, drives runner.tick after POST)
- `tests/test_transactions_route.py` — FOUND (modified, _seed drives ticks)
- `tests/conftest.py` — FOUND (modified, client fixture truncates BOTH before AND after)

Verified commits exist in git log:
- `db1ef0d` — FOUND (feat: status route + D-16 force-poll reshape + status/enqueue/backfill schemas)
- `32debb7` — FOUND (feat: backfill route + status/force-poll/backfill tests + main.py mount)

Verified plan-level invariants (per `<verification>` section):
- Full pytest suite: 80 passed, 0 failed.
- `grep -c "HTTP_202_ACCEPTED" src/finance_bro/api/routes_import.py src/finance_bro/api/routes_backfill.py` returns 1 each (both new routes use 202).
- `grep -q "ImportResultOut" src/finance_bro/api/routes_import.py` is FALSE (D-16 done).
- `grep -q "NoCardAccountFound" src/finance_bro/api/routes_import.py` is FALSE.
- `grep -RE "(routes_status|routes_backfill)" src/finance_bro/main.py | wc -l` = 4 (≥ 2).
- `grep -q "DISTINCT ON (account_id)" src/finance_bro/api/routes_status.py` matches.
- `grep -E "(scheduler|accounts|backfill).*ImportStatusOut" src/finance_bro/api/schemas.py | wc -l` returns 0 — false-positive (deviation 3); the architectural invariant is satisfied (lines 88-91 contain the 3-field nesting).
- Phase 1 regression suite: 21 passed (test_health/test_no_auth/test_partial_unique_index/test_log_redaction/test_idempotency/test_money_invariants/test_schema_invariants/test_settings).
- Phase 2 cumulative: 41 passed.

---
*Phase: 02-reliable-sync*
*Plan: 04 (status-surface)*
*Completed: 2026-05-10*
