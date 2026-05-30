---
phase: 04-categorized-spending
plan: 03
subsystem: categorization-import
tags: [categorizer, import, lock-invariant, engine-reuse, rules-engine]

requires:
  - phase: 04-01
    provides: "pure categorizer engine (categorize_rows, RowView, RulePredicate, SKIP) + seeded categories/rules"
  - phase: 04-02
    provides: "RuleRepo.list_active_ordered (priority ASC, id ASC), CategoryRepo, transactions read path with category fields"
provides:
  - "TransactionRepo.fetch_for_categorize (NOT is_user_locked, NOT is_deleted, source_tx_id = ANY) -> list[RowView]"
  - "TransactionRepo.apply_categories (category_id + category_source='rule'; NULL-on-no-match, never touches is_user_locked)"
  - "categorizer.engine.compile_rules — ORM-rule -> CompiledRule adapter (RulePredicate.model_validate at the boundary)"
  - "import_service Step 4b: auto-categorize touched non-locked rows after insert_many (D-10/D-11)"
  - "CAT-04 lock-invariant proven on the import path (re-import never clobbers a manual lock)"
affects:
  - "Plan 04 (run-over-history) reuses compile_rules + categorize_rows + apply_categories verbatim"

tech-stack:
  added: []
  patterns:
    - "compile_rules adapts ORM rows (JSON predicate) into CompiledRule via RulePredicate.model_validate — the ONE place at-rest JSON becomes the AST for evaluation"
    - "RuleRowLike Protocol keeps the engine SQLAlchemy-free; callers cast list[Rule] at the ORM boundary (Mapped[int]->int)"
    - "defense-in-depth lock skip: fetch_for_categorize filters NOT is_user_locked in SQL AND the engine returns SKIP"
    - "autouse account/transaction TRUNCATE isolation for session_factory tests that drive run_one_card (polls lowest-id card)"

key-files:
  created:
    - tests/test_categorize_on_import.py
    - tests/test_lock_invariant.py
  modified:
    - src/finance_bro/db/transaction_repo.py
    - src/finance_bro/services/import_service.py
    - src/finance_bro/categorizer/engine.py
    - src/finance_bro/categorizer/__init__.py

key-decisions:
  - "compile_rules lives in the pure engine (not the repo) so Plan 04's history sweep reuses the exact same ORM->CompiledRule adapter (D-11)"
  - "RuleRowLike Protocol + cast at the import_service boundary — keeps the categorizer package free of any SQLAlchemy import (purity guard stays green)"
  - "no-match rows are stamped category_source='rule' with category_id NULL (D-02) — evaluated-but-unmatched, never silently bucketed"
  - "touched ids derived from the upserted items list ([t.source_tx_id for t in items]) — import categorizes ONLY touched non-locked rows (Open Question 2)"

patterns-established:
  - "Step 4b categorize wiring: insert_many -> compile_rules(list_active_ordered) -> fetch_for_categorize -> categorize_rows -> apply_categories, all inside one begin() block"
  - "session_factory tests that call run_one_card need autouse accounts/transactions truncation (the lowest-id-card poll picks up leaked accounts otherwise)"

requirements-completed: [CAT-01, CAT-04]

duration: 34min
completed: 2026-05-30
---

# Phase 4 Plan 03: Auto-Categorize-On-Import Summary

**Import ticks now auto-categorize newly-touched non-locked rows by reusing the Plan 01 pure engine verbatim (D-11), stamping `category_source='rule'` (NULL on no-match, D-02), while a user-locked manual row provably survives a re-import untouched (CAT-04 headline).**

## Performance

- **Duration:** ~34 min
- **Started:** 2026-05-30
- **Completed:** 2026-05-30
- **Tasks:** 2
- **Files modified:** 6 (4 source/test modified+created, 2 new test files)

## Accomplishments
- `TransactionRepo.fetch_for_categorize` — loads touched non-locked rows as pure `RowView`s, filtering `NOT is_user_locked AND NOT is_deleted` in SQL (D-09 / Pitfall 1).
- `TransactionRepo.apply_categories` — targeted parameterized UPDATEs setting `category_id` + `category_source='rule'`; a `(id, None)` update writes NULL (D-02); never references `is_user_locked`.
- `categorizer.engine.compile_rules` — the single ORM-rule → `CompiledRule` adapter (validates the JSON predicate into the AST once), reused verbatim by the upcoming Plan 04 history sweep.
- `import_service.run_one_card` Step 4b — after `insert_many`, loads active rules + touched non-locked rows, runs the PURE engine, writes back — all inside the existing `begin()` block.
- CAT-04 proven on the import path: a re-import re-touching a locked, manually-categorized row leaves its `category_id` / `category_source='manual'` / `is_user_locked=true` unchanged; a fresh non-locked grocery debit comes back `category_source='rule'` + the Groceries id.

## Task Commits

1. **Task 1 (RED): failing repo+engine seam test** — `fcad6f5` (test)
2. **Task 1 (GREEN): fetch_for_categorize + apply_categories + compile_rules** — `b143556` (feat)
3. **Task 2: import_service categorize step + lock-invariant test** — `a0fb932` (feat)

_Task 1 was `tdd="true"` (RED → GREEN, no separate REFACTOR needed). Task 2 is `type="auto"`._

## Files Created/Modified
- `src/finance_bro/db/transaction_repo.py` — added `fetch_for_categorize` (locked-filtered RowView read) and `apply_categories` (rule-stamping write-back).
- `src/finance_bro/categorizer/engine.py` — added `compile_rules` + the `RuleRowLike` Protocol (keeps the package SQLAlchemy-free).
- `src/finance_bro/categorizer/__init__.py` — exported `compile_rules`.
- `src/finance_bro/services/import_service.py` — Step 4b categorize wiring inside the upsert `begin()` block.
- `tests/test_categorize_on_import.py` — repo+engine seam: locked-row exclusion, engine round-trip (Groceries / NULL / untouched).
- `tests/test_lock_invariant.py` — CAT-04 headline import-path test via a stub importer through `run_one_card`.

## Decisions Made
- `compile_rules` placed in the pure engine, not the repo, so Plan 04 reuses the identical ORM→AST adapter (D-11 verbatim-reuse).
- `RuleRowLike` Protocol + `cast("list[RuleRowLike]", ...)` at the `import_service` boundary — the categorizer package imports no SQLAlchemy (the no-eval/purity guard stays green).
- No-match rows are stamped `category_source='rule'` with `category_id` NULL (D-02) — the row was evaluated and matched nothing, never silently bucketed.
- Touched ids come from `[t.source_tx_id for t in items]` (the just-upserted items) — the import categorizes only touched non-locked rows (Open Question 2 resolution).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `compile_rules` adapter was implied but not specified**
- **Found during:** Task 1 (writing the engine round-trip test).
- **Issue:** `RuleRepo.list_active_ordered()` returns ORM `Rule` objects with a JSON-dict `predicate`, but `engine.categorize_rows` requires `list[CompiledRule]` with a parsed `RulePredicate`. The plan's interface comment showed `categorize_rows(rows, rules)` without naming the conversion step, so an adapter had to exist to wire the two.
- **Fix:** Added `compile_rules(Sequence[RuleRowLike]) -> list[CompiledRule]` to the pure engine — it `RulePredicate.model_validate`s each stored predicate (mirroring the route DTO's boundary validation, V5). Kept in the engine (not the repo) so Plan 04's history sweep reuses the same adapter (D-11). The `RuleRowLike` Protocol keeps the engine free of any SQLAlchemy import; the `import_service` cast bridges the `Mapped[int]`→`int` type-checker friction at the ORM boundary.
- **Files modified:** `src/finance_bro/categorizer/engine.py`, `src/finance_bro/categorizer/__init__.py`, `src/finance_bro/services/import_service.py`
- **Verification:** purity guard (`test_no_eval_in_categorizer`) green; `basedpyright` clean on engine + import_service; round-trip test green.
- **Committed in:** `b143556` (Task 1) / `a0fb932` (Task 2 cast at boundary).

**2. [Rule 1 - Bug] Lock-invariant test polled the wrong account due to leaked rows**
- **Found during:** Task 2 (running the plan trio together).
- **Issue:** `run_one_card` Step 2 polls the **lowest-id** `mono.card` (D-04). `session_factory`-based tests do NOT truncate accounts (only the `client` fixture does — conftest), so accounts created by `test_categorize_on_import.py` leaked and `run_one_card` imported the statement into the wrong account; my test's `fresh-tx` never landed under the queried `account_id` (KeyError). The test passed in isolation but failed when run after the sibling file (and would be order-dependent in the full suite).
- **Fix:** Added an `autouse` `_isolate_accounts` fixture (TRUNCATE `transactions, accounts RESTART IDENTITY CASCADE` before AND after each test) to BOTH new test files — mirrors the `client` fixture's isolation contract. Seeded categories/rules are untouched (not truncated).
- **Files modified:** `tests/test_lock_invariant.py`, `tests/test_categorize_on_import.py`
- **Verification:** plan trio green when run together (`test_categorize_on_import.py tests/test_lock_invariant.py`); full suite 152 passed.
- **Committed in:** `a0fb932` (Task 2).

---

**Total deviations:** 2 auto-fixed (1 blocking adapter, 1 test-isolation bug).
**Impact on plan:** Both necessary for correctness; no scope creep. The `compile_rules` adapter is the explicit D-11 reuse seam Plan 04 depends on; the isolation fixtures make the integration tests deterministic.

## Issues Encountered
- During Task 1, my first "no-match" test row used `mcc=4111`, which is actually the seeded **Transport** rule — it categorized instead of returning NULL. Switched the no-match row to `mcc=9999` (not in any seeded rule) so it genuinely matches nothing.
- A test-local `session.begin()` after a prior `execute` raised `InvalidRequestError: A transaction is already begun` — restructured the test to open one `s.begin()` block for fetch+apply.

## User Setup Required
None - no external service configuration required.

## Verification
- Plan trio: `uv run pytest tests/test_categorize_on_import.py tests/test_lock_invariant.py -x -q` → **3 passed** (2 categorize-seam + 1 lock-invariant).
- Full suite: `uv run pytest -q` → **152 passed, 0 failed**.
- Acceptance greps: `NOT is_user_locked` present in `fetch_for_categorize`; `apply_categories` sets `category_source = 'rule'` and never references `is_user_locked`; no f-string SQL in `transaction_repo.py`; `categorize_rows`/`fetch_for_categorize`/`list_active_ordered` all wired in `import_service.py`.
- `ruff check` + `ruff format --check` clean on all new/modified source + tests.
- `basedpyright` clean (0 errors) on `categorizer/engine.py` and `services/import_service.py`. The 7 pre-existing `reportUnknownVariableType` errors in `transaction_repo.insert_many` (lines 119-134) are confirmed pre-existing (documented in 04-02 SUMMARY) and out of scope — my new `fetch_for_categorize`/`apply_categories` methods are clean.
- Purity guard `test_no_eval_in_categorizer` green — the `compile_rules` additions introduce no DB/eval surface.

## TDD Gate Compliance

Task 1 followed RED → GREEN:
- RED `fcad6f5` (`test(04-03): ...`): the repo+engine seam test committed against not-yet-existing `compile_rules` / repo methods (ImportError-level RED confirmed).
- GREEN `b143556` (`feat(04-03): ...`): the two repo methods + `compile_rules` that turn it green.
No separate REFACTOR commit was needed. Task 2 is a non-TDD `type="auto"` task.

## Known Stubs
None. The import-step categorizer is fully wired and proven by live integration tests. The history-sweep half of the CAT-04 invariant (and the `tests/test_lock_invariant.py` history-sweep assertions) is intentionally Plan 04's scope — this file's assertions cover the import path only, and the two halves coexist by construction.

## Threat Flags
None. All security-relevant surface introduced (the `fetch_for_categorize` locked-row filter, the parameterized `apply_categories` write-back, the engine reuse) is covered by the plan's `<threat_model>` mitigations and proven by the new tests (T-4-lock → re-import leaves a locked row untouched; T-4-bucket → no-match writes NULL; T-4-sqli → no f-string SQL).

## Next Phase Readiness
- Plan 04 (run-over-history sweep) can reuse `compile_rules`, `categorize_rows`, `fetch_for_categorize`, and `apply_categories` verbatim — the D-11 single-engine promise holds at the repo+engine seam.
- The `tests/test_lock_invariant.py` file is structured so Plan 04 can add the history-sweep half alongside the existing import-path test.

---
*Phase: 04-categorized-spending*
*Completed: 2026-05-30*
