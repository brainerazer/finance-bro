---
phase: 02-reliable-sync
verified: 2026-05-10T00:00:00Z
status: human_needed
score: 4/4 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Run the app on the NAS for an hour without touching it; check GET /api/import/status for advancing last_polled_at"
    expected: "New transactions appear in GET /api/transactions within ~3 minutes of posting on the Mono card"
    why_human: "Cannot exercise the real 65s RateLimitGate against the actual Mono API in automated tests; respx mocks the HTTP layer"
  - test: "Decide whether BL-01 and BL-02 blocker edge cases are acceptable for this phase"
    expected: "Either accept the blockers as post-phase gap-closure work, or require them fixed before proceeding"
    why_human: "All four success criteria are met on the happy path and tests pass 80/80. The two BLOCKER findings from the code review concern edge cases (BL-01: repeated POST /api/import during active backfill; BL-02: stale in_flight interaction with round-robin). These do not break the phase goal but could cause confusing behavior in production. Human judgment required on whether to proceed or close first."
gaps: []
---

# Phase 2: Reliable Sync Verification Report

**Phase Goal:** Bohdan stops clicking import. The app polls Mono on its own at the rate-limit budget, ingests holds correctly (and updates them in place when they clear), can backfill 12 months on first connect, and surfaces "last poll N min ago" plus 401/429 distinctly so silent failures are impossible.
**Verified:** 2026-05-10
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | App polls Mono autonomously; new transactions appear in GET /api/transactions within ~3 minutes of posting (round-robin, ≥60s per token) | ✓ VERIFIED (automated + human needed for production test) | APScheduler at 10s interval with max_instances=1, coalesce=True in main.py:71-78. RateLimitGate still the sole 65s budget owner. test_scheduler_round_robin tests the round-robin logic with respx. |
| 2 | 12-month backfill walks ≤30-day windows newest-first, persists cursor per chunk, resumes exactly where stopped if container is killed | ✓ VERIFIED | backfill_chunks() yields newest-first 30-day windows (verified empirically). Each chunk = one import_runs row (status-per-row IS the cursor). test_backfill_resumability::test_resume_picks_remaining_chunks proves restart-resilience across a simulated container kill (5 of 12 rows pre-marked done, fresh runner picks up the remaining 7). recover_in_flight resets stale in_flight rows at startup. |
| 3 | hold:true rows flagged held; when same (account_id, source_tx_id) returns with hold:false, single row updates in place — no duplicate | ✓ VERIFIED | ON CONFLICT DO UPDATE in transaction_repo.py with SET clause containing EXACTLY hold/amount_minor/raw_payload. test_cleared_updates_in_place (central correctness test) mutates 6 manual-edit columns post-insert and proves all survive the cleared upsert. TransactionOut.hold surfaced on GET /api/transactions. Note: "excluded from spent totals" is deferred to Phase 6 (no totals endpoint exists in Phase 2; hold flag is available for Phase 6 to filter). |
| 4 | GET /api/import/status shows last successful poll timestamp, last error, and distinguishes 401 (sticky auth_failed) from 429 (transient, scheduler still running) | ✓ VERIFIED | routes_status.py serves STATUS_QUERY CTE joining accounts × import_runs × scheduler_state. test_401_vs_429_distinguished seeds auth_failed scheduler_state + 429-bearing import_runs row and asserts scheduler.state='auth_failed' while per-account last_error carries '429'. test_401_persists_across_restart proves sticky bit survives simulated restart. |

**Score:** 4/4 truths verified

### ING-06 "last_cursor" terminology note

The ROADMAP SC#2 uses the term "persists `last_cursor` per chunk." The implementation uses `import_runs` status-per-row semantics: each enqueued pending/done backfill row IS the cursor for that chunk. This is functionally equivalent — a crashed container with 5 done rows and 7 pending rows resumes from the 6th chunk because `claim_next_pending` takes the oldest pending row. The test `test_resume_picks_remaining_chunks` proves this end-to-end. No implementation gap, just different terminology.

### hold:true exclusion from "spent totals" scope note

ING-05 and SC#3 both mention "excluded from any 'spent' totals." Phase 2 has no spending-totals endpoint — that is Phase 6 (UI-01/dashboard). The Phase 2 scope correctly delivers: (a) hold flag ingested and stored, (b) hold flag surfaced on GET /api/transactions, (c) hold→cleared upsert works in-place. The exclusion-from-totals filtering will be implemented when Phase 6 builds the totals query. The hold field is available for that filtering.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `alembic/versions/0002_phase2_sync.py` | accounts.mono_type + import_runs + scheduler_state | ✓ VERIFIED | File exists; contains import_runs table, scheduler_state singleton with CHECK id=1, mono_type column with backfill UPDATE |
| `src/finance_bro/db/models.py` | Account.mono_type + ImportRun + SchedulerState | ✓ VERIFIED | All three ORM classes present |
| `src/finance_bro/db/import_run_repo.py` | claim/enqueue/recover/audit methods | ✓ VERIFIED | All 7 methods present including claim_next_pending, recover_in_flight, last_live_per_account |
| `src/finance_bro/db/scheduler_state_repo.py` | read/write singleton | ✓ VERIFIED | UPDATE-only repo; no INSERT path |
| `src/finance_bro/db/account_repo.py` | list_pollable_cards() D-01 allowlist | ✓ VERIFIED | WHERE mono_type IN ('black','platinum','white') ORDER BY id ASC |
| `src/finance_bro/db/transaction_repo.py` | ON CONFLICT DO UPDATE 3-column SET clause | ✓ VERIFIED | set_= contains EXACTLY hold/amount_minor/raw_payload; xmax=0 detection wired |
| `src/finance_bro/scheduler/__init__.py` | package marker | ✓ VERIFIED | File exists |
| `src/finance_bro/scheduler/errors.py` | MonoAuthError/MonoRateLimitError/MonoTransientError | ✓ VERIFIED | Three exception classes; MonoRateLimitError carries retry_after_seconds |
| `src/finance_bro/scheduler/window.py` | backfill_chunks newest-first 30d windows | ✓ VERIFIED | MONO_STATEMENT_MAX_WINDOW_SECONDS=2_682_000; MONO_STATEMENT_BACKFILL_WINDOW_DAYS=30; empirically verified newest-first |
| `src/finance_bro/scheduler/runner.py` | SchedulerRunner with full tick body | ✓ VERIFIED | ~297 LOC; tick/recover_in_flight/read_state/enqueue_backfill/enqueue_live_for_all_active_cards/aclose |
| `src/finance_bro/importers/monobank.py` | typed-error split; hold/description/mcc populated | ✓ VERIFIED | 401→MonoAuthError, 429→MonoRateLimitError, other→MonoTransientError; gate.acquire FIRST in both methods |
| `src/finance_bro/api/routes_status.py` | GET /api/import/status D-14 shape | ✓ VERIFIED | STATUS_QUERY CTE verbatim; scheduler + accounts + backfill sections |
| `src/finance_bro/api/routes_backfill.py` | POST /api/backfill 202 | ✓ VERIFIED | 202 + BackfillEnqueueOut |
| `src/finance_bro/api/routes_import.py` | POST /api/import 202 D-16 reshape | ✓ VERIFIED | 18 lines; no ImportResultOut; no NoCardAccountFound; returns ImportEnqueuedOut |
| `src/finance_bro/api/schemas.py` | 8 new Pydantic models | ✓ VERIFIED | SchedulerStatusOut/AccountStatusOut/BackfillStatusOut/ImportStatusOut/ImportEnqueueRowOut/ImportEnqueuedOut/BackfillEnqueueIn/BackfillEnqueueOut |
| `src/finance_bro/main.py` | lifespan + 6 routers mounted | ✓ VERIFIED | init_engine→runner→recover_in_flight→read_state→scheduler; 6 include_router calls |
| `tests/fixtures/client_info_multi_card.json` | 4 cards: eAid/black/platinum/white | ✓ EXISTS | File present; note: WR-08 flags this fixture as unreferenced by any test (dead asset) |
| `tests/fixtures/statement_with_hold.json` | hold:true HOLD-FIXTURE-ID-1 | ✓ VERIFIED | Used by test_hold_cleared_upsert.py::test_e2e_hold_then_cleared |
| `tests/fixtures/statement_cleared_followup.json` | same id hold:false different amount | ✓ VERIFIED | Used by same test |
| `tests/fixtures/statement_empty.json` | [] | ✓ VERIFIED | Used by backfill resumability test |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| main.py lifespan | SchedulerRunner.tick | APScheduler IntervalTrigger(seconds=10), max_instances=1, coalesce=True | ✓ WIRED | main.py:71-78 |
| runner.tick | ImportRunRepo.claim_next_pending | direct call in tick body | ✓ WIRED | runner.py:213 |
| runner.tick | MonobankImporter.fetch_statement | direct call, typed-exception catch | ✓ WIRED | runner.py:243 |
| MonobankImporter | MonoAuthError/MonoRateLimitError/MonoTransientError | inline 401/429/other branch in both methods | ✓ WIRED | monobank.py:74-80 and 118-125; 6 raises total |
| runner.tick | TransactionRepo.insert_many | direct call, unpacks (inserted, updated) | ✓ WIRED | runner.py:248-249 |
| runner.tick | ImportRunRepo.mark_done | direct call with statement_count/inserted/updated | ✓ WIRED | runner.py:251-255 |
| runner.tick 401 path | SchedulerStateRepo.write('auth_failed') | _set_state_auth_failed helper | ✓ WIRED | runner.py:267, 295-296 |
| routes_import.py | runner.enqueue_live_for_all_active_cards | Depends(get_scheduler_runner) | ✓ WIRED | routes_import.py:37 |
| routes_backfill.py | runner.enqueue_backfill | Depends(get_scheduler_runner) | ✓ WIRED | routes_backfill.py:31 |
| routes_status.py | STATUS_QUERY CTE | session.execute(text(STATUS_QUERY)) | ✓ WIRED | routes_status.py:101 |
| routes_status.py | SchedulerStateRepo.read | direct call | ✓ WIRED | routes_status.py:90 |
| conftest.py client fixture | TRUNCATE + scheduler_state reseed | both before AND after (02-04 deviation 2 fix) | ✓ WIRED | |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| routes_status.py | accounts, sched, backfill | STATUS_QUERY CTE over import_runs + SchedulerStateRepo.read() | DB query over real import_runs/scheduler_state rows | ✓ FLOWING |
| transaction_repo.py insert_many | (inserted, updated) | xmax=0 RETURNING trick | PostgreSQL system column — real insert vs update detection | ✓ FLOWING |
| runner.py tick | items (CanonicalTransaction list) | MonobankImporter.fetch_statement AsyncIterator | Real Mono response (mocked in tests, real in production) | ✓ FLOWING (test-verified) |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite | uv run pytest -x -q | 80 passed, 0 failed in 4.87s | ✓ PASS |
| SET clause restriction | grep -c "set_=" src/finance_bro/db/transaction_repo.py | 1 | ✓ PASS |
| Exactly 3 SET columns | grep excluded src/finance_bro/db/transaction_repo.py | hold/amount_minor/raw_payload only | ✓ PASS |
| APScheduler installed | uv run python -c "import apscheduler; print(apscheduler.__version__)" | 3.11.2 | ✓ PASS |
| 6 routers mounted | grep include_router src/finance_bro/main.py | 6 include_router calls | ✓ PASS |
| backfill newest-first | empirical check of backfill_chunks output | chunks[0][1] > chunks[1][1] | ✓ PASS |
| 12 chunks enqueued | test_backfill_enqueue | 12 pending backfill rows asserted | ✓ PASS |
| Sticky 401 | test_401_persists_across_restart | fresh runner reads auth_failed state from DB | ✓ PASS |
| 429 transient | test_429_does_not_stop | scheduler_state stays 'running' after 429 | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|----------------|-------------|--------|----------|
| ING-05 | 02-01, 02-02, 02-03 | Hold/pending transactions ingested with hold flag; excluded from totals; updated in-place when same id arrives with hold=false | ✓ SATISFIED (partially deferred) | D-10 upsert enforced at SQL layer; test_cleared_updates_in_place; TransactionOut.hold surfaced. "Excluded from totals" deferred to Phase 6 (no totals endpoint in Phase 2 scope). |
| ING-06 | 02-01, 02-03 | Chunked, resumable backfill in ≤30-day windows; last_cursor persisted so crashed backfill resumes | ✓ SATISFIED | backfill_chunks 30-day windows; import_runs rows as cursor; test_resume_picks_remaining_chunks; test_recover_in_flight_on_restart |
| ING-08 | 02-01, 02-03, 02-04 | Polling status surfaced in UI: last poll timestamp, last error, 401/429 distinguished | ✓ SATISFIED | GET /api/import/status (D-14 shape); test_import_status_shape; test_401_vs_429_distinguished |

All three Phase 2 requirements (ING-05, ING-06, ING-08) are claimed in plans 02-01 through 02-04 and confirmed mapped in REQUIREMENTS.md. No orphaned requirements.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| src/finance_bro/main.py | 63-65 | MonobankImporter constructed BEFORE try block; if recover_in_flight raises, aclose() never called (CR-01) | Warning | httpx.AsyncClient leak on startup DB failure; with filterwarnings=error in pyproject, unclosed-client warning escalates to exception |
| src/finance_bro/scheduler/runner.py | 116-131 | enqueue_live_for_all_active_cards does NOT apply count_pending_or_in_flight_backfill filter (BL-01) | Warning | During active backfill, POST /api/import enqueues live rows that queue behind ~12 backfill rows; repeated button clicks create unbounded duplicate live rows |
| src/finance_bro/scheduler/runner.py | 153-182 | _pick_next_active_card does NOT filter stale in_flight rows (BL-02) | Warning | A card whose only live row is in_flight evaluates completed_at=None → datetime.min, "wins" rotation, gets another live row enqueued |
| src/finance_bro/scheduler/runner.py | 93-114 | enqueue_backfill silently returns [] for non-existent/non-pollable account_id (CR-02) | Warning | POST /api/backfill with bad account_id returns 202 + {run_ids:[]} with no error signal |
| src/finance_bro/db/import_run_repo.py | 99-121 | mark_done(updated=...) accepts and silently discards the 'updated' parameter via del (WR-01) | Info | Misleading API; future contributor will assume 'updated' is persisted |
| src/finance_bro/db/import_run_repo.py | 133-149 | recover_in_flight runs only at startup; crashes during a long-lived tick leave stale rows for up to 5 minutes (WR-03) | Warning | Root cause of BL-02; during same-process lifetime, a tick-time _mark_error failure leaves row in in_flight indefinitely until restart |
| tests/fixtures/client_info_multi_card.json | N/A | Unreferenced test fixture — no test imports it (WR-08) | Info | Dead asset; no functional impact |
| tests/test_idempotency.py, tests/test_transactions_route.py | Various | Tests access httpx private API client._transport.app.state.runner (WR-07) | Warning | Future httpx minor release could rename _transport without breaking change |

**Anti-pattern severity assessment per SC impact:**

The four BL/CR findings from the code review are confirmed in the codebase. Assessed against the phase goal:

- **BL-01** (duplicate live rows during backfill): Does not prevent SC#1 on the happy path (no backfill running). Only manifests when user clicks POST /api/import during a 12-month backfill. The scheduler WILL eventually drain all pending rows (both backfill and live). SC#1 is not broken, but the UX is confusing and the queue bloat is real.

- **BL-02** (stale in_flight → duplicate enqueue): Requires a tick-time crash followed by _mark_error also failing (double failure). In the normal crash case, _mark_error succeeds and the row becomes 'error', not 'in_flight'. The stale in_flight path only happens on a double failure. Low probability in practice, but the code is structurally exposed.

- **CR-01** (lifespan resource leak): Affects startup only when recover_in_flight raises. In normal operation with a healthy DB, this never triggers. The risk is a confusing error message on a misconfigured/migrating container.

- **CR-02** (silent [] on bad account_id): Pure UX issue — the backfill does not run when asked to, with no error signal.

None of the four findings break the four success criteria on the primary happy path.

### Human Verification Required

#### 1. Production smoke test — SC#1 end-to-end

**Test:** Deploy to NAS via docker compose up; leave running for one hour without any manual action; observe GET /api/import/status at the 10-minute mark and at the 60-minute mark.
**Expected:** last_polled_at for each active Mono card advances by ~65s (one poll per rate-limit slot); new Mono transactions that post during the hour appear in GET /api/transactions within ~3 minutes.
**Why human:** The real Mono API with real transactions is required; respx mocks the HTTP layer in all automated tests.

#### 2. BL-01 / BL-02 acceptance decision — Bohdan decides

**Test:** Review BL-01 (POST /api/import during active backfill enqueues live rows that sit behind backfill rows) and BL-02 (stale in_flight→round-robin picks up duplicate live row). Assess whether these are acceptable "known bugs to close in the next mini-sprint" or blockers to proceeding to Phase 3.

**Context for the decision:**
- 80/80 tests pass. All four SC criteria are verified on the happy path.
- BL-01 only manifests when the user manually clicks "import" while a 12-month first-time backfill is running. A user doing this on a fresh install may see the "last polled" counter appear stuck — confusing, not data-corrupting.
- BL-02 requires a tick-time crash where _mark_error also fails (double failure). Low probability. When it manifests, it creates extra import_runs rows, not data corruption.
- The code-reviewer's BL/CR classification is appropriate: these are real defects, not false positives.
- Fix difficulty: BL-01 is one additional count_pending_or_in_flight_backfill call + a count_pending_live guard (3-4 lines); BL-02 is adding a status filter in _pick_next_active_card or running recover_in_flight per-tick (also 3-4 lines).

**Expected decision options:**
- A) Pass with warnings — log BL-01/BL-02 as known defects; add to gap list for Phase 2.5 mini-sprint; proceed to Phase 3.
- B) Gaps found — require BL-01 and BL-02 closure (and optionally CR-01/CR-02) before marking Phase 2 complete.

**Why human:** The four success criteria are met; the defects are edge cases. Only Bohdan can decide if the production risk of these edge cases is acceptable given his use pattern (fresh install, 12-month backfill, then normal steady-state polling).

---

## Code Review Findings Summary (from 02-REVIEW.md)

The code review (2026-05-10, 39 files, depth: standard) identified:

**2 BLOCKER findings (edge cases, happy path works):**

- **BL-01**: `enqueue_live_for_all_active_cards` does not apply `count_pending_or_in_flight_backfill` filter. Confirmed in runner.py:116-131 — no such check exists. Effect: POST /api/import during active backfill queues live rows behind backfill rows; repeated clicks create duplicate pending live rows with no dedup guard.

- **BL-02**: `_pick_next_active_card` does not filter cards whose most-recent live run is in_flight or pending. Confirmed in runner.py:153-182. Effect: a card with a stale in_flight live row evaluates completed_at=None → datetime.min → "wins" the min() selection → runner enqueues another live row for an already-running account. Root cause of BL-02 is WR-03: recover_in_flight only runs at startup.

**2 CRITICAL findings:**

- **CR-01**: `MonobankImporter` constructed outside the `try` block in lifespan (main.py:63 vs try at line 84). If `recover_in_flight()` or `read_state()` raises, `runner.aclose()` never runs, leaking the httpx.AsyncClient. Confirmed in code.

- **CR-02**: `enqueue_backfill(account_id=X)` silently returns `[]` when X is not a pollable card. Route returns 202 + {run_ids:[]} with no error signal. Confirmed in runner.py:106-107 — no validation before the filter.

**Fix complexity:** BL-01 and BL-02 are each a ~4-line code addition. CR-01 requires wrapping lines 65-66 inside the try block. CR-02 requires a ValueError raise + route 404 translation.

---

_Verified: 2026-05-10_
_Verifier: Claude (gsd-verifier)_
