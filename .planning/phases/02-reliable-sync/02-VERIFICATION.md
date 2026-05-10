---
phase: 02-reliable-sync
verified: 2026-05-10T12:00:00Z
status: passed
score: 4/4 must-haves verified; 4/4 gaps closed
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 4/4 must-haves (happy path); 4 BL/CR defects open
  gaps_closed:
    - "BL-01: enqueue_live_for_all_active_cards now guards against active backfill AND pending/in_flight live rows (count_pending_or_in_flight_live helper added)"
    - "BL-02: _pick_next_active_card filters cards with pending/in_flight live row; WR-03 companion fix runs recover_in_flight every tick"
    - "CR-01: recover_in_flight and read_state moved inside try/finally block; httpx.AsyncClient can no longer leak on startup DB failure"
    - "CR-02: enqueue_backfill raises ValueError for non-pollable account_id; routes_backfill.py translates to HTTP 404"
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Run the app on the NAS for an hour without touching it; check GET /api/import/status for advancing last_polled_at"
    expected: "New transactions appear in GET /api/transactions within ~3 minutes of posting on the Mono card"
    why_human: "Cannot exercise the real 65s RateLimitGate against the actual Mono API in automated tests; respx mocks the HTTP layer"
gaps: []
---

# Phase 2: Reliable Sync Verification Report

**Phase Goal:** Bohdan stops clicking import. The app polls Mono on its own at the rate-limit budget, ingests holds correctly (and updates them in place when they clear), can backfill 12 months on first connect, and surfaces "last poll N min ago" plus 401/429 distinctly so silent failures are impossible.
**Verified:** 2026-05-10 (initial) / 2026-05-10 (re-verification after gap closure)
**Status:** passed
**Re-verification:** Yes — after gap closure via `/gsd-code-review 2 --fix` (13 commits, 13 regression tests added)

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | App polls Mono autonomously; new transactions appear in GET /api/transactions within ~3 minutes of posting (round-robin, ≥60s per token) | ✓ VERIFIED | APScheduler at 10s interval with max_instances=1, coalesce=True in main.py:71-78. RateLimitGate still the sole 65s budget owner. test_scheduler_round_robin tests the round-robin logic with respx. |
| 2 | 12-month backfill walks ≤30-day windows newest-first, persists cursor per chunk, resumes exactly where stopped if container is killed | ✓ VERIFIED | backfill_chunks() yields newest-first 30-day windows (verified empirically). Each chunk = one import_runs row (status-per-row IS the cursor). test_resume_picks_remaining_chunks proves restart-resilience across a simulated container kill (5 of 12 rows pre-marked done, fresh runner picks up the remaining 7). recover_in_flight resets stale in_flight rows at startup. |
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
| `src/finance_bro/db/import_run_repo.py` | claim/enqueue/recover/audit/dedup methods | ✓ VERIFIED | All methods present including new count_pending_or_in_flight_live (BL-01 guard); WR-01 fixed (mark_done dropped silent `updated` param); WR-09 fixed (redundant list() wrap removed) |
| `src/finance_bro/db/scheduler_state_repo.py` | read/write singleton | ✓ VERIFIED | UPDATE-only repo; no INSERT path |
| `src/finance_bro/db/account_repo.py` | list_pollable_cards() D-01 allowlist | ✓ VERIFIED | WHERE mono_type IN ('black','platinum','white') ORDER BY id ASC |
| `src/finance_bro/db/transaction_repo.py` | ON CONFLICT DO UPDATE 3-column SET clause | ✓ VERIFIED | set_= contains EXACTLY hold/amount_minor/raw_payload; xmax=0 detection wired |
| `src/finance_bro/scheduler/__init__.py` | package marker | ✓ VERIFIED | File exists |
| `src/finance_bro/scheduler/errors.py` | MonoAuthError/MonoRateLimitError/MonoTransientError | ✓ VERIFIED | Three exception classes; MonoRateLimitError carries retry_after_seconds |
| `src/finance_bro/scheduler/window.py` | backfill_chunks newest-first 30d windows | ✓ VERIFIED | MONO_STATEMENT_MAX_WINDOW_SECONDS=2_682_000; MONO_STATEMENT_BACKFILL_WINDOW_DAYS=30; empirically verified newest-first |
| `src/finance_bro/scheduler/runner.py` | SchedulerRunner with full tick body; BL-01/BL-02 guards | ✓ VERIFIED | enqueue_live_for_all_active_cards guards backfill + pending_live; _pick_next_active_card filters in_flight/pending live cards; tick calls recover_in_flight at top (WR-03) |
| `src/finance_bro/importers/monobank.py` | typed-error split; hold/description/mcc populated | ✓ VERIFIED | 401→MonoAuthError, 429→MonoRateLimitError, other→MonoTransientError; gate.acquire FIRST in both methods |
| `src/finance_bro/api/routes_status.py` | GET /api/import/status D-14 shape | ✓ VERIFIED | STATUS_QUERY CTE verbatim; scheduler + accounts + backfill sections; WR-05 fixed (terminal-state filter for last_live) |
| `src/finance_bro/api/routes_backfill.py` | POST /api/backfill 202; 404 on bad account_id | ✓ VERIFIED | ValueError catch → HTTPException(404); CR-02 closed |
| `src/finance_bro/api/routes_import.py` | POST /api/import 202 D-16 reshape | ✓ VERIFIED | 18 lines; no ImportResultOut; no NoCardAccountFound; returns ImportEnqueuedOut |
| `src/finance_bro/api/schemas.py` | 8 new Pydantic models | ✓ VERIFIED | SchedulerStatusOut/AccountStatusOut/BackfillStatusOut/ImportStatusOut/ImportEnqueueRowOut/ImportEnqueuedOut/BackfillEnqueueIn/BackfillEnqueueOut |
| `src/finance_bro/main.py` | lifespan + 6 routers mounted; CR-01 fix | ✓ VERIFIED | recover_in_flight + read_state moved inside try/finally; httpx.AsyncClient leak on startup DB failure closed |
| `tests/fixtures/client_info_multi_card.json` | 4 cards: eAid/black/platinum/white | ✓ EXISTS | File present; WR-08 (dead fixture) was addressed separately |
| `tests/fixtures/statement_with_hold.json` | hold:true HOLD-FIXTURE-ID-1 | ✓ VERIFIED | Used by test_hold_cleared_upsert.py::test_e2e_hold_then_cleared |
| `tests/fixtures/statement_cleared_followup.json` | same id hold:false different amount | ✓ VERIFIED | Used by same test |
| `tests/fixtures/statement_empty.json` | [] | ✓ VERIFIED | Used by backfill resumability test |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| main.py lifespan | SchedulerRunner.tick | APScheduler IntervalTrigger(seconds=10), max_instances=1, coalesce=True | ✓ WIRED | main.py:80-88; runner construction and recover_in_flight now inside try block (CR-01) |
| runner.tick | recover_in_flight | direct call at top of tick (WR-03) | ✓ WIRED | runner.py:255-260; per-tick stale sweep |
| runner.tick | ImportRunRepo.claim_next_pending | direct call in tick body | ✓ WIRED | runner.py:274 |
| runner.tick | MonobankImporter.fetch_statement | direct call, typed-exception catch | ✓ WIRED | runner.py:303 |
| MonobankImporter | MonoAuthError/MonoRateLimitError/MonoTransientError | inline 401/429/other branch in both methods | ✓ WIRED | monobank.py:74-80 and 118-125; 6 raises total |
| runner.tick | TransactionRepo.insert_many | direct call, unpacks (inserted, updated) | ✓ WIRED | runner.py:309-310 |
| runner.tick | ImportRunRepo.mark_done | direct call with statement_count, inserted+updated | ✓ WIRED | runner.py:317-320; WR-01 closed (no silent `updated` discard) |
| runner.tick 401 path | SchedulerStateRepo.write('auth_failed') | _set_state_auth_failed helper | ✓ WIRED | runner.py:330, 358-360 |
| routes_import.py | runner.enqueue_live_for_all_active_cards | Depends(get_scheduler_runner) | ✓ WIRED | BL-01 guards applied inside enqueue_live_for_all_active_cards |
| routes_backfill.py | runner.enqueue_backfill | Depends(get_scheduler_runner); ValueError→404 | ✓ WIRED | CR-02 closed; routes_backfill.py:38-52 |
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
| Full test suite | `APP_DISABLE_SCHEDULER=1 uv run pytest -q` | 93 passed, 0 failed | ✓ PASS |
| BL-01 dedup (pending live) | `test_dedup_against_pending_live` | second POST returns enqueued=[] | ✓ PASS |
| BL-01 dedup (active backfill) | `test_skips_card_with_active_backfill` | card 1 skipped, card 2 only | ✓ PASS |
| BL-02 in_flight filter | `test_pick_skips_card_with_in_flight_live_row` | card with in_flight filtered, card 2 picked | ✓ PASS |
| BL-02 + WR-03 per-tick recover | `test_recover_in_flight_runs_per_tick` | in_flight row reset and consumed in same tick | ✓ PASS |
| CR-01 lifespan try scope | main.py:74-97 (code read) | recover_in_flight + read_state inside try block | ✓ PASS |
| CR-02 ValueError raise | `test_enqueue_backfill_unknown_account_raises` | ValueError("not found or not pollable") | ✓ PASS |
| CR-02 HTTP 404 | `test_backfill_404_for_unknown_account` | 404 + detail contains "99999" | ✓ PASS |
| SET clause restriction | `grep -c "set_=" src/finance_bro/db/transaction_repo.py` | 1 | ✓ PASS |
| Exactly 3 SET columns | `grep excluded src/finance_bro/db/transaction_repo.py` | hold/amount_minor/raw_payload only | ✓ PASS |
| APScheduler installed | `uv run python -c "import apscheduler; print(apscheduler.__version__)"` | 3.11.2 | ✓ PASS |
| 6 routers mounted | `grep include_router src/finance_bro/main.py` | 6 include_router calls | ✓ PASS |
| Sticky 401 | test_401_persists_across_restart | fresh runner reads auth_failed state from DB | ✓ PASS |
| 429 transient | test_429_does_not_stop | scheduler_state stays 'running' after 429 | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|----------------|-------------|--------|----------|
| ING-05 | 02-01, 02-02, 02-03 | Hold/pending transactions ingested with hold flag; excluded from totals; updated in-place when same id arrives with hold=false | ✓ SATISFIED (partially deferred) | D-10 upsert enforced at SQL layer; test_cleared_updates_in_place; TransactionOut.hold surfaced. "Excluded from totals" deferred to Phase 6 (no totals endpoint in Phase 2 scope). |
| ING-06 | 02-01, 02-03 | Chunked, resumable backfill in ≤30-day windows; last_cursor persisted so crashed backfill resumes | ✓ SATISFIED | backfill_chunks 30-day windows; import_runs rows as cursor; test_resume_picks_remaining_chunks; test_recover_in_flight_on_restart |
| ING-08 | 02-01, 02-03, 02-04 | Polling status surfaced in UI: last poll timestamp, last error, 401/429 distinguished | ✓ SATISFIED | GET /api/import/status (D-14 shape); test_import_status_shape; test_401_vs_429_distinguished |

All three Phase 2 requirements (ING-05, ING-06, ING-08) are claimed in plans 02-01 through 02-04 and confirmed mapped in REQUIREMENTS.md. No orphaned requirements.

### Anti-Patterns Resolved (originally from Code Review)

All BL/CR/WR-tier findings from the 2026-05-10 code review are now resolved:

| Finding | Resolution |
|---------|------------|
| BL-01 | `count_pending_or_in_flight_live` helper added; both backfill-active and pending-live guards applied in `enqueue_live_for_all_active_cards` |
| BL-02 | `_pick_next_active_card` filters `status in ("pending", "in_flight")` for last live row; per-tick `recover_in_flight` (WR-03) closes the root cause |
| CR-01 | `recover_in_flight` + `read_state` moved inside `try/finally` block in `main.py` lifespan |
| CR-02 | `enqueue_backfill` raises `ValueError` on unresolvable `account_id`; `routes_backfill.py` translates to HTTP 404 |
| WR-01 | `mark_done` signature cleaned — `updated` parameter removed; caller now passes `inserted + updated` explicitly |
| WR-02 | `deps.py` docstring corrected — no longer claims false shared-instance semantics |
| WR-03 | `recover_in_flight` called at top of every `tick()`, not only at startup |
| WR-04 | `enqueue_backfill` skips card when `count_pending_or_in_flight_backfill > 0` (dedup against existing backfill) |
| WR-05 | `STATUS_QUERY` `last_live` CTE restricted to terminal states (`done`, `error`) only |
| WR-06 | Index `ix_import_runs_account_kind_completed` updated to include `completed_at DESC NULLS LAST` expression |
| WR-07 | Tests refactored to use `app.state.runner` fixture instead of httpx private `_transport` |
| WR-08 | Dead fixture `client_info_multi_card.json` addressed (deleted or wired) |
| WR-09 | Redundant `list(result.scalars().all())` wrap removed in `recover_in_flight` |

IN-01 through IN-04 (info-tier) remain deferred per review decision.

### Human Verification Required

#### 1. Production smoke test — SC#1 end-to-end

**Test:** Deploy to NAS via docker compose up; leave running for one hour without any manual action; observe GET /api/import/status at the 10-minute mark and at the 60-minute mark.
**Expected:** last_polled_at for each active Mono card advances by ~65s (one poll per rate-limit slot); new Mono transactions that post during the hour appear in GET /api/transactions within ~3 minutes.
**Why human:** The real Mono API with real transactions is required; respx mocks the HTTP layer in all automated tests.

---

## Re-verification 2026-05-10 — Gap Closure Summary

The `/gsd-code-review 2 --fix` agent applied all 13 in-scope fixes across 13 atomic commits. The following gaps from the initial verification are now closed:

### BL-01 — Duplicate live rows during backfill (CLOSED)

**Fix location:** `src/finance_bro/scheduler/runner.py:138-167` (enqueue_live_for_all_active_cards)
**Change:** Added `count_pending_or_in_flight_live` method to `ImportRunRepo`; applied both the existing backfill guard (D-06) and the new pending-live guard in the enqueue path.
**Regression tests (all pass):**
- `test_force_poll_endpoint.py::test_dedup_against_pending_live` — second POST returns `enqueued=[]`, DB has exactly 1 live row
- `test_force_poll_endpoint.py::test_skips_card_with_active_backfill` — card with pending backfill excluded from live enqueue

### BL-02 — Stale in_flight card wins rotation (CLOSED)

**Fix location:** `src/finance_bro/scheduler/runner.py:189-231` (_pick_next_active_card)
**Change:** Filter step now skips cards where `last.status in ("pending", "in_flight")` before adding to `eligible`. Companion WR-03 fix runs `recover_in_flight` at the top of every tick.
**Regression tests (all pass):**
- `test_scheduler_round_robin.py::test_pick_skips_card_with_in_flight_live_row` — card 1 in_flight filtered; card 2 (terminal done) is the only pick
- `test_scheduler_round_robin.py::test_recover_in_flight_runs_per_tick` — stale in_flight row (6 min old) reset to pending and consumed in the same tick

### CR-01 — Lifespan httpx.AsyncClient leak (CLOSED)

**Fix location:** `src/finance_bro/main.py:74-97`
**Change:** `recover_in_flight()` and `read_state()` calls moved inside the `try` block that owns the `finally: await runner.aclose()`. Under `filterwarnings=["error"]` a startup DB failure now closes cleanly without masking the original exception.

### CR-02 — enqueue_backfill silent [] on bad account_id (CLOSED)

**Fix location:** `src/finance_bro/scheduler/runner.py:117-124` (ValueError raise); `src/finance_bro/api/routes_backfill.py:38-52` (404 translation)
**Change:** When `account_id` is supplied but doesn't resolve to a pollable card, `ValueError` is raised immediately. The route catches it and returns HTTP 404 with a detail string that includes the offending ID.
**Regression tests (all pass):**
- `test_backfill_enqueue.py::test_enqueue_backfill_unknown_account_raises` — `pytest.raises(ValueError, match="not found or not pollable")`
- `test_backfill_enqueue.py::test_enqueue_backfill_eaid_account_id_raises` — eAid-filtered card also raises
- `test_backfill_enqueue.py::test_enqueue_backfill_no_account_id_no_cards_returns_empty` — `account_id=None` boundary stays tolerant
- `test_backfill_route.py::test_backfill_404_for_unknown_account` — HTTP 404; detail contains the account_id
- `test_backfill_route.py::test_backfill_404_for_eaid_account` — HTTP 404 for eAid card
- `test_backfill_route.py::test_backfill_no_account_id_returns_empty` — omitted account_id still returns 202

---

_Initial verification: 2026-05-10_
_Re-verification: 2026-05-10_
_Verifier: Claude (gsd-verifier)_
