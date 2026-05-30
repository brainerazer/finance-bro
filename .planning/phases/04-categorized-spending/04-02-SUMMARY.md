---
phase: 04-categorized-spending
plan: 02
subsystem: category-rule-crud
tags: [crud, rest-api, predicate-validation, delete-guard, fk]
requires:
  - "categories + rules tables; transactions.category_id FK ON DELETE RESTRICT (Plan 04-01)"
  - "RulePredicate discriminated-union AST (Plan 04-01, categorizer/predicate.py)"
  - "tracked_fx_currency_repo / transaction_repo idioms (Phases 2-3)"
provides:
  - "CategoryRepo: CRUD + reference_counts(cid) pre-check (D-15)"
  - "RuleRepo: CRUD, list_active_ordered (priority ASC, id ASC), two-phase reorder"
  - "GET/POST/PATCH/DELETE /api/categories with the 409 D-15 delete guard"
  - "GET/POST/PATCH/DELETE /api/rules + PATCH /api/rules/reorder"
  - "transactions read path carries category_id/category_source/category_name/category_color"
  - "category/rule request+response DTOs with RulePredicate validated at the boundary (V5)"
affects:
  - "Plan 03 (auto-categorize-on-import) consumes the same rules/categories the UI now manages"
  - "Plan 04 (run-over-history) reuses list_active_ordered ordering"
  - "Frontend (later phase) gets full taxonomy + rule management over HTTP"
tech-stack:
  added: []
  patterns:
    - "Per-handler repo instantiation from the request session (mirrors routes_transactions)"
    - "RulePredicate as the request DTO field → Pydantic rejects malformed predicates with 422 at parse"
    - "reference_counts pre-check → HTTPException 409 {rules, transactions}; FK RESTRICT as DB backstop"
    - "Two-phase priority rewrite (park id-derived high band → renumber) to dodge uq_rules_priority"
    - "Literal route /api/rules/reorder declared before /api/rules/{rid} so the static path wins"
    - "autouse _restore_taxonomy fixture (snapshot max ids → delete created rows) for non-truncated seed tables"
key-files:
  created:
    - src/finance_bro/db/category_repo.py
    - src/finance_bro/db/rule_repo.py
    - src/finance_bro/api/routes_categories.py
    - src/finance_bro/api/routes_rules.py
    - tests/test_categories_crud.py
    - tests/test_rules_crud.py
    - tests/test_category_delete_guard.py
  modified:
    - src/finance_bro/db/transaction_repo.py
    - src/finance_bro/api/schemas.py
    - src/finance_bro/main.py
decisions:
  - "reorder uses a two-phase rewrite (park into a 1_000_000+id band, then renumber 1..N) — avoids the uq_rules_priority UNIQUE collision a naive in-place swap would hit"
  - "RuleOut.predicate is typed RulePredicate (re-validated on the way out) so a malformed-at-rest predicate can never leak past the API boundary"
  - "write handlers call session.commit() explicitly — get_session yields a non-autocommitting session (the read-only transactions route never committed)"
  - "/api/rules/reorder declared before /api/rules/{rid} so 'reorder' is not parsed as an int rid"
requirements: [CAT-01, CAT-02, CAT-03]
metrics:
  duration_min: 38
  completed: "2026-05-30"
  tasks: 2
  files: 10
  tests_added: 12
---

# Phase 4 Plan 02: Category + Rule CRUD Summary

The full Category + Rule CRUD slice over HTTP: two repos, request/response DTOs (with the predicate AST validated at the API boundary), the `routes_categories.py`/`routes_rules.py` routers mounted in `main.py`, the D-15 delete guard (409 with reference counts), a collision-safe priority reorder, and the transactions read path extended to carry category id/name/color. A caller can now fully manage the taxonomy and rule set over REST.

## What Was Built

### Task 1 — Repos + DTOs + transactions read extension (TDD; RED `3433ccd`, GREEN `918eb12`)
- `src/finance_bro/db/category_repo.py`: `create`/`get`/`list_all` (ORM `select(Category).order_by(Category.id)`)/`update`/`delete`, plus `reference_counts(category_id) -> (int, int)` using TWO parameterized `text()` counts against `rules` and `transactions` (never f-string SQL — D-15 / T-4-sqli). This is the pre-check that backs the delete guard.
- `src/finance_bro/db/rule_repo.py`: `create`/`get`/`list_all`/`update`/`delete`, `list_active_ordered()` (`order_by(Rule.priority, Rule.id)` — the deterministic tiebreak the engine relies on, Pitfall 6), and `reorder(ordered_ids)`. The predicate is stored/returned as a plain JSON dict; validation lives at the route DTO.
- `src/finance_bro/db/transaction_repo.py`: extended `ROLLUP_SQL` with `LEFT JOIN categories c ON c.id = t.category_id` (LEFT — `category_id` may be NULL, D-02) and selected `t.category_id, t.category_source, c.name AS category_name, c.color AS category_color`. They flow through the existing `dict(m)` row composition untouched.
- `src/finance_bro/api/schemas.py`: added `CategoryOut`/`CategoryCreateIn`/`CategoryUpdateIn`, `RuleOut`/`RuleCreateIn`/`RuleUpdateIn`/`RuleReorderIn`, and the four `category_*` fields on `TransactionOut`. `RulePredicate` is imported from `finance_bro.categorizer.predicate` and used as the `predicate` field type on the rule DTOs so malformed predicates are rejected at request parse (V5).
- Tests: `test_categories_crud.py` (round-trip, reference_counts 0→nonzero, unreferenced delete), `test_rules_crud.py` (predicate JSONB round-trip + re-validation, list_active_ordered (priority, id) ordering, reverse reorder with no UNIQUE collision, update/delete).

### Task 2 — Routers + main wiring + delete-guard test (commit `a52dcee`)
- `src/finance_bro/api/routes_categories.py`: `GET/POST/PATCH/DELETE /api/categories`. The DELETE handler is the D-15 guard — it calls `reference_counts(cid)` and raises `HTTPException(409, detail={"rules": n, "transactions": m})` if either count is nonzero, else deletes. Repos are instantiated per-handler from the request session (mirrors routes_transactions); write handlers commit explicitly.
- `src/finance_bro/api/routes_rules.py`: `GET/POST/PATCH/DELETE /api/rules` + `PATCH /api/rules/reorder`. `RuleCreateIn.predicate` is `RulePredicate`, so Pydantic returns 422 for an unknown `op` before the interpreter runs. The literal `/api/rules/reorder` is declared before `/api/rules/{rid}` so it is not parsed as an int rid.
- `src/finance_bro/main.py`: added `routes_categories, routes_rules` to the import tuple and `app.include_router(...)` for both (no prefix, no auth — DEP-02).
- `tests/test_category_delete_guard.py`: end-to-end over the `client` fixture — 409-with-counts on a referenced category, 204 on an unreferenced one, 422 on a malformed predicate, 201 on the canonical ATB predicate, plus a CRUD smoke for both routers.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] CRUD tests leaked seeded-table rows → broke test_migration_0004's absolute count**
- **Found during:** Task 2 full-suite run.
- **Issue:** The `client`/`session_factory` fixtures truncate `transactions, import_runs, accounts, scheduler_state, mono_rate_state` but NOT `categories`/`rules` (they carry the migration-0004 seed). My CRUD tests create categories (and rules) that survived the test, so `test_migration_0004::test_0004_seeds_and_fk_present` (which asserts exactly 15 seeded categories) saw 21.
- **Fix:** Added an `autouse` `_restore_taxonomy` fixture to each of the three new test files. It snapshots the seeded baseline (`max(id)` of categories + rules) before the test and deletes any rows with a higher id afterward — rules first (FK ON DELETE RESTRICT). The seeded baseline is restored exactly, so the absolute-count assertion holds again.
- **Files modified:** `tests/test_categories_crud.py`, `tests/test_rules_crud.py`, `tests/test_category_delete_guard.py`
- **Commit:** `a52dcee` (committed with Task 2; the test-fixture additions to the two Task-1 files are the cleanup work for Task 2's full-suite green).

No architectural (Rule 4) deviations. No authentication gates. Phase 4 installs zero packages (T-4-SC accept — vacuously satisfied).

### Notable implementation choice (within plan latitude)

- **Two-phase reorder.** `RuleRepo.reorder` parks each affected row at a distinct, collision-free temporary priority (`1_000_000 + id`) and then renumbers 1..N in the requested order. The plan flagged this exact pitfall (`uq_rules_priority` UNIQUE collision on a naive in-place swap); the id-derived park guarantees per-row uniqueness during the swap.

## Verification

- Plan trio: `uv run pytest tests/test_categories_crud.py tests/test_rules_crud.py tests/test_category_delete_guard.py -x -q` → **12 passed**.
- Full suite `uv run pytest -q` → **149 passed, 0 failed** (Phases 1-3 + Plan 04-01 unaffected; the seed-table count assertion is green after the cleanup fixture).
- Acceptance greps: no f-string SQL in either repo or router; `routes_categories`/`routes_rules` present in BOTH the main.py import tuple AND the include block.
- `ruff check` + `ruff format --check` clean on all new/modified source.
- `basedpyright` clean (0 errors) on all FOUR new source files (`category_repo.py`, `rule_repo.py`, `routes_categories.py`, `routes_rules.py`) and `schemas.py`. The 7 pre-existing `reportUnknownVariableType` errors in `transaction_repo.insert_many` and the 5 in `main.py` (APScheduler `add_job`) are confirmed present on the base (`git show HEAD:...`) and are out of scope for this plan's 3-line ROLLUP_SQL / 2-line include edits (SCOPE BOUNDARY).

## TDD Gate Compliance

Task 1 followed RED → GREEN:
- RED `3433ccd` (`test(04-02): ...`): `test_categories_crud.py` + `test_rules_crud.py` committed against not-yet-existing repos (ImportError-level RED confirmed).
- GREEN `918eb12` (`feat(04-02): ...`): repos + DTOs + ROLLUP_SQL extension that turn them green.
No separate REFACTOR commit was needed. Task 2 is a non-TDD `type="auto"` task (router wiring + integration test).

## Known Stubs

None. Both routers are fully implemented and mounted; the delete guard, reorder, and predicate-boundary validation are all exercised by live tests. (Auto-categorize-on-import and the history sweep remain out of this plan — Plans 03/04.)

## Threat Flags

None. All security-relevant surface introduced (the rule/category CRUD boundary, the predicate validation, the delete reference check) is covered by the plan's `<threat_model>` mitigations and proven by the new tests (T-4-validate → 422, T-4-sqli → no f-string SQL grep, T-4-fk → 409 delete guard).

## Self-Check: PASSED

All 7 created files present on disk; all 3 commits (`3433ccd`, `918eb12`, `a52dcee`) present in git history.
