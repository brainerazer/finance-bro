---
phase: 04-categorized-spending
plan: 04
subsystem: categorization-history-sweep
tags: [categorizer, rules-engine, staleness-token, lost-update, engine-reuse, api]

requires:
  - phase: 04-01
    provides: "pure categorizer engine (categorize_rows, compile_rules, RowView, RulePredicate, SKIP)"
  - phase: 04-03
    provides: "TransactionRepo.fetch_for_categorize/apply_categories + compile_rules adapter (mirrored verbatim)"
provides:
  - "TransactionRepo.fetch_all_for_categorize (account-wide NOT is_user_locked / NOT is_deleted) + count_locked"
  - "RulesHistoryService.preview/commit — stateless re-run of the PURE engine + sha256 staleness token (D-13)"
  - "StaleRunError — commit raises it on token mismatch; the route maps it to HTTP 409 (re-preview)"
  - "CategoryChange / RunPreviewOut / RunCommitIn / RunCommitOut DTOs"
  - "POST /api/rules/run/preview + POST /api/rules/run/commit endpoints (first-card scoped, D-04)"
  - "get_rules_history_service provider"
affects:
  - "Future UI slice (run-rules button + diff preview modal) consumes these two endpoints"

tech-stack:
  added: []
  patterns:
    - "preview→commit staleness handshake: _compute is the single source of truth; commit re-runs it and compares sha256 tokens before any write (never blind-applies a preview-time diff)"
    - "sha256 token over (priority-ordered rules signature, sorted current row→category state) via stdlib hashlib+json — any rule edit or non-locked row change flips it"
    - "account-wide sweep reuses fetch_all_for_categorize + compile_rules + categorize_rows + apply_categories verbatim — no second categorization path (D-11)"
    - "defense-in-depth lock skip: fetch_all_for_categorize filters NOT is_user_locked in SQL AND the engine returns SKIP"

key-files:
  created:
    - src/finance_bro/services/rules_history.py
    - tests/test_history_preview_commit.py
  modified:
    - src/finance_bro/db/transaction_repo.py
    - src/finance_bro/api/schemas.py
    - src/finance_bro/api/routes_rules.py
    - src/finance_bro/api/deps.py

key-decisions:
  - "skipped_locked_count comes from a dedicated TransactionRepo.count_locked query (NOT inferred from the read) — the account-wide read already excludes locked rows in SQL, so a separate count(*) is the clean source (stated per plan's 'pick one and state it')"
  - "preview/commit endpoints are scoped to the FIRST card via AccountRepo.get_first_card (mirrors routes_transactions, D-04 single-card v1 model); 404 when no card exists, distinct from an empty diff"
  - "the commit DTO carries ONLY the token (RunCommitIn.token); the account is resolved server-side the same way preview resolves it — no client-supplied account_id"
  - "StaleRunError lives in services/rules_history.py (pure-ish service exception); the route is the single place it becomes HTTP 409 (D-13)"

requirements-completed: [CAT-05, CAT-04]

duration: 28min
completed: 2026-05-30
---

# Phase 4 Plan 04: Run-Rules-Over-History Summary

**`POST /api/rules/run/preview` sweeps the PURE Plan 01 engine over EVERY non-locked transaction and returns the full old→new diff plus a sha256 staleness token; `POST /api/rules/run/commit` recomputes that token from current state and applies the diff only on a match — returning HTTP 409 "stale — re-preview" on mismatch and changing nothing — while a user-locked row is provably never swept or written (CAT-05 end-to-end, CAT-04 re-asserted on the account-wide path).**

## Performance

- **Duration:** ~28 min
- **Completed:** 2026-05-30
- **Tasks:** 2 (Task 1 `tdd="true"` RED→GREEN; Task 2 `type="auto"`)
- **Files:** 6 (2 created, 4 modified)

## Accomplishments

- `TransactionRepo.fetch_all_for_categorize(account_id)` — the account-wide counterpart to Plan 03's touched read: same column set + RowView build, dropping the `source_tx_id = ANY(...)` filter, keeping `NOT is_user_locked AND NOT is_deleted` (D-14 / Pitfall 1). Plus `count_locked(account_id)` for the `skipped_locked_count`.
- `RulesHistoryService` — `_compute` (the single source of truth for preview AND commit) loads the ordered rules + all non-locked rows in a session, then runs the PURE engine OUTSIDE the session (mirrors `import_service` Step 4b), derives the diff (only rows whose new category differs from current), and computes the sha256 token over the rules signature + sorted row→category state. `preview` returns counts + per-row `changes[]` + token; `commit` re-runs `_compute`, compares the recomputed token to the submitted one, and applies via `apply_categories` ONLY on match, else raises `StaleRunError` (D-13 / Pitfall 4 — never blind-applies).
- `POST /api/rules/run/preview` (200, `RunPreviewOut`) + `POST /api/rules/run/commit` (200 `RunCommitOut`, **409** on stale via `StaleRunError`→`HTTPException`). Both scoped to the first card (D-04); 404 when no card exists. `get_rules_history_service` provider mirrors `get_import_service`.
- CAT-05 proven end-to-end: a mixed account (NULL-grocery → Groceries, Transport→Groceries overwrite, no-match → NULL untouched, locked-manual → untouched) yields `changed_count=2`, `overwritten_count=1`, `skipped_locked_count=1`; commit on the matching token applies and stamps `category_source='rule'`; a stale/garbage token returns 409 and changes nothing; the locked row is never in `changes` and unchanged after commit.

## Task Commits

1. **Task 1 (RED): failing CAT-05 preview/commit + staleness tests** — `992c9ce` (test)
2. **Task 1 (GREEN): RulesHistoryService + fetch_all_for_categorize + DTOs** — `5bbde3f` (feat)
3. **Task 2: preview/commit endpoints + provider + route tests (409 on stale)** — `dd22ddd` (feat)

## Files Created/Modified

- `src/finance_bro/services/rules_history.py` (created) — `RulesHistoryService` (`preview`/`commit`/`_compute`), `StaleRunError`, `_compute_token` (stdlib sha256/json).
- `tests/test_history_preview_commit.py` (created) — 5 tests: 4 service-level (`<behavior>` cases) + 1 route-level (preview 200 + shape, commit 200 applies, stale 409 changes nothing, locked untouched).
- `src/finance_bro/db/transaction_repo.py` — added `fetch_all_for_categorize` + `count_locked`.
- `src/finance_bro/api/schemas.py` — added `CategoryChange`, `RunPreviewOut`, `RunCommitIn`, `RunCommitOut`.
- `src/finance_bro/api/routes_rules.py` — added the two `/api/rules/run/*` endpoints (no new router; reuses the Plan 02 router already wired in `main.py`).
- `src/finance_bro/api/deps.py` — added `get_rules_history_service`.

## Deviations from Plan

None — the plan executed as written.

The plan left two micro-choices to discretion, both resolved and noted in the frontmatter `key-decisions`:
- **`skipped_locked_count` source:** a dedicated `count_locked` query (the account-wide read already excludes locked rows in SQL, so counting them separately is the clean source).
- **Account selection for the endpoints:** the first card via `AccountRepo.get_first_card` (mirrors `routes_transactions`, D-04), with a 404 when no card exists — so the commit DTO carries only the token.

No Rule 1/2/3 auto-fixes were needed; no Rule 4 architectural decisions; no authentication gates. Phase 4 installs zero packages (token uses stdlib `hashlib`/`json` only — T-4-SC accept, vacuously satisfied).

## Verification

- Plan headline: `uv run pytest tests/test_history_preview_commit.py -x -q` → **5 passed** (4 service-level + 1 route-level).
- Full suite: `uv run pytest -q` → **157 passed, 0 failed** (Phases 1-3 and Plans 04-01..04-03 unaffected — this plan adds one read method + a count, one service, two endpoints, and DTOs).
- Acceptance greps all pass:
  - `NOT is_user_locked` present in `fetch_all_for_categorize` (transaction_repo.py).
  - `rules_history.py` imports + calls `engine.categorize_rows`; no `eval`/`exec`/`re.compile`/`import re` in the file (D-11 — no second matching path).
  - No f-string SQL in `transaction_repo.py`.
  - Both `run/preview` + `run/commit` endpoints present; `get_rules_history_service` provider present; `status_code=409` stale mapping present.
- `ruff check` + `ruff format --check` clean on all new/modified source + tests.
- `basedpyright` clean (0 errors) on `services/rules_history.py`, `api/routes_rules.py`, `api/deps.py`. The 7 pre-existing `reportUnknownVariableType` errors in `transaction_repo.insert_many` (lines 119-134) are confirmed pre-existing (documented in 04-02/04-03 SUMMARYs) and out of scope — my new `fetch_all_for_categorize`/`count_locked` methods are clean.

## TDD Gate Compliance

Task 1 followed RED → GREEN:
- RED `992c9ce` (`test(04-04): ...`): 4 service-level tests committed against the not-yet-existing `finance_bro.services.rules_history` module (`ModuleNotFoundError`-level RED confirmed).
- GREEN `5bbde3f` (`feat(04-04): ...`): the service + repo read methods + DTOs that turn them green.
No separate REFACTOR commit was needed. Task 2 is a non-TDD `type="auto"` task.

## Known Stubs

None. The history sweep is fully wired and proven by live integration tests (service-level + route-level). The plan is `UI hint: no` — backend/API only by design; a future UI slice consumes these two endpoints.

## Threat Flags

None. All security-relevant surface introduced is covered by the plan's `<threat_model>` mitigations and proven by the new tests:
- **T-4-stale** — commit recomputes + compares the sha256 token; the 409-on-stale route test asserts NO rows change.
- **T-4-lock** — `fetch_all_for_categorize` filters `NOT is_user_locked` in SQL AND the engine SKIPs; the locked-row-unchanged assertion holds at both service and route level.
- **T-4-eval** — no new evaluation path (engine reused verbatim; grep confirms no matching logic in `rules_history.py`).
- **T-4-sqli** — parameterized `text()` only; grep forbids f-string SQL.

## Self-Check: PASSED

- Created files present on disk: `src/finance_bro/services/rules_history.py`, `tests/test_history_preview_commit.py`.
- Commits present in git history: `992c9ce`, `5bbde3f`, `dd22ddd`.

---
*Phase: 04-categorized-spending*
*Completed: 2026-05-30*
