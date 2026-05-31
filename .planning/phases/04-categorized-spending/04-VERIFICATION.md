---
phase: 04-categorized-spending
verified: 2026-05-31T09:30:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 4/5
  gaps_closed:
    - "After a manual re-categorization (category_source=manual, is_user_locked=1), every subsequent rule run — including on next import — leaves that row alone (CR-01/WR-02 TOCTOU gap closed by commit 9c89269)"
  gaps_remaining: []
  regressions: []
---

# Phase 4: Categorized Spending Verification Report

**Phase Goal:** Bohdan opens the app and most of his transactions already have a sensible category attached, courtesy of MCC defaults and his own rules. When auto-logic is wrong, he fixes one row, and re-running rules over history never clobbers his manual fix again.
**Verified:** 2026-05-31T09:30:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (commit 9c89269, fix(04): close TOCTOU + lock-clobber gap in run-rules-over-history)

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | On first connect, every imported transaction is auto-categorized by the rules engine using a default ~15-category taxonomy seeded from MCC groups; uncategorized rows are NULL never silently bucketed | VERIFIED | Migration 0004 seeds 15 categories + 11 MCC rules. `import_service.py` Step 4b calls `fetch_for_categorize` + `categorize_rows` + `apply_categories` inside one `begin()` block. `apply_categories` stamps NULL for no-match rows (D-02). 158 tests pass including `test_engine_round_trip_applies_categories_and_nulls`. |
| 2 | A rule can be created via `POST /api/rules` with composable predicates (fixed op vocabulary, no eval/regex-of-doom), priority-ordered, first-match-wins | VERIFIED | `routes_rules.py POST /api/rules` uses `RuleCreateIn.predicate: RulePredicate` — Pydantic discriminated-union validates at parse. Interpreter uses a `match` statement with no `eval`/`exec`/`re`. `list_active_ordered()` uses `ORDER BY Rule.priority, Rule.id`. `test_canonical_atb_predicate_accepted` and `test_malformed_predicate_rejected_at_boundary` pass. |
| 3 | "Run rules over history" returns a diff preview before commit; after confirm, only targeted rows update and `is_user_locked` rows are skipped unconditionally | VERIFIED | Preview shape correct and verified by tests. `commit` now recomputes + token-compares + writes ALL inside ONE `session.begin()` transaction using `_compute_in_session(session, account_id, lock=True)`. The recompute reads rows `FOR UPDATE`. `_APPLY_CATEGORY_SQL` carries `AND NOT is_user_locked`. The lock invariant is now transaction-safe, not merely probabilistic. |
| 4 | After a manual re-categorization (category_source=manual, is_user_locked=1), every subsequent rule run — including on next import — leaves that row alone | VERIFIED | CR-01 and WR-02 both fixed in commit 9c89269. Three independent guards now protect locked rows: (1) `_FETCH_ALL_FOR_CATEGORIZE_FOR_UPDATE_SQL` has `AND NOT is_user_locked` so locked rows never enter the engine; (2) engine returns SKIP for `is_user_locked=True` rows; (3) `_APPLY_CATEGORY_SQL` has `AND NOT is_user_locked` so the write-back physically cannot overwrite a locked row. The three-layer defense is proven by `test_apply_categories_writeback_refuses_locked_row` which hands a locked row's id DIRECTLY to `apply_categories` and asserts the UPDATE is refused. |
| 5 | The category list is fully user-editable (PATCH/POST /api/categories); rules referencing a deleted category surface a clear error rather than silently corrupting state | VERIFIED | `routes_categories.py` implements `GET/POST/PATCH/DELETE /api/categories`. Delete handler calls `reference_counts(cid)` and raises `HTTPException(409, detail={"rules": n, "transactions": m})` when nonzero. FK `ON DELETE RESTRICT` (`fk_transactions_category`) is the DB backstop. `test_delete_referenced_category_returns_409_with_counts` passes. |

**Score: 5/5 truths verified**

---

### CR-01 / WR-02 Gap Closure Confirmation

**Prior gap (initial verification, 2026-05-30):** `RulesHistoryService.commit` opened a first session A to recompute the staleness token (via `_compute`), closed it, then opened a second session B to write. Between those two sessions a concurrent import tick or future manual lock-setter could change a row's state. `apply_categories` carried no `NOT is_user_locked` WHERE guard.

**Fix applied (commit 9c89269, 2026-05-31):**

1. `_compute_in_session(session, account_id, *, lock: bool)` extracted as the shared core. The `commit` path calls it with `lock=True` inside its own `session.begin()` context (line 143-144 of `rules_history.py`). The `preview` path calls `_compute` which opens a throwaway read-only session with `lock=False`. The two-session pattern for commit is gone.

2. `_FETCH_ALL_FOR_CATEGORIZE_FOR_UPDATE_SQL` (lines 212-222 of `transaction_repo.py`) reads the sweep rows under a PostgreSQL `FOR UPDATE` row lock. A concurrent writer must either serialize behind this transaction (blocked) or have already changed a row (which flips the recomputed token → `StaleRunError` / 409). The TOCTOU read→write window is closed.

3. `_APPLY_CATEGORY_SQL` (line 285-287 of `transaction_repo.py`) now reads `WHERE id = :tid AND NOT is_user_locked`. A locked row is refused at the SQL level even if somehow handed directly to the write-back.

**Regression test proving WR-02 deterministically:** `test_apply_categories_writeback_refuses_locked_row` (line 285 of `test_history_preview_commit.py`) bypasses every upstream filter and calls `apply_categories([(locked_id, groceries), (unlocked_id, groceries)])` directly. The locked row's category is asserted unchanged; the unlocked row in the same batch is asserted written — confirming the guard is surgical, not a blanket no-op.

**The lock invariant (CAT-04, SC#3, SC#4) is now transaction-safe, not merely probabilistic.**

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `alembic/versions/0004_categorized_spending.py` | categories + rules tables, FK, taxonomy + MCC seed | VERIFIED | 15 categories, 11 MCC rules, `fk_transactions_category` ON DELETE RESTRICT, parameterized subselect seed |
| `src/finance_bro/categorizer/predicate.py` | Pydantic discriminated-union predicate AST (closed op vocabulary) | VERIFIED | 7 condition types, `Condition = Annotated[..., Field(discriminator="op")]`, `RulePredicate.all: list[Condition]` |
| `src/finance_bro/categorizer/interpreter.py` | match-based op evaluator, no eval/regex | VERIFIED | `eval_condition` is a `match` statement; no `re`/`eval`/`exec`; None field → False |
| `src/finance_bro/categorizer/engine.py` | first-match-wins categorize_row / categorize_rows + compile_rules | VERIFIED | SKIP sentinel for locked rows, first-match-wins, compile_rules ORM→AST adapter |
| `src/finance_bro/db/models.py` | Category + Rule ORM models; FK on Transaction.category_id | VERIFIED | `class Category`, `class Rule`, `ForeignKey("categories.id", ondelete="RESTRICT")` on Transaction.category_id |
| `src/finance_bro/db/category_repo.py` | Category CRUD + reference_counts | VERIFIED | CRUD methods + `reference_counts(cid) -> (int, int)` using parameterized `text()` |
| `src/finance_bro/db/rule_repo.py` | Rule CRUD, list_active_ordered, two-phase reorder | VERIFIED | `list_active_ordered()` ORDER BY priority, id; two-phase park-then-renumber reorder |
| `src/finance_bro/api/routes_categories.py` | POST/PATCH/DELETE/list /api/categories with 409 delete guard | VERIFIED | All CRUD endpoints; `reference_counts` pre-check → 409 with `{rules, transactions}` |
| `src/finance_bro/api/routes_rules.py` | POST/PATCH/DELETE/list /api/rules + priority reorder + run/preview + run/commit | VERIFIED | All CRUD, PATCH /reorder, POST /run/preview, POST /run/commit with StaleRunError → 409 |
| `src/finance_bro/services/rules_history.py` | RulesHistoryService.preview / commit — sha256 staleness token, single-transaction commit | VERIFIED | `commit` uses one `session.begin()` context; `_compute_in_session` with `lock=True` reads FOR UPDATE; token compare and write-back are atomic. |
| `src/finance_bro/db/transaction_repo.py` | fetch_for_categorize, fetch_all_for_categorize (with FOR UPDATE variant), apply_categories (with lock guard) | VERIFIED | Both fetch methods have `NOT is_user_locked` SQL guards. `_FETCH_ALL_FOR_CATEGORIZE_FOR_UPDATE_SQL` adds `FOR UPDATE`. `_APPLY_CATEGORY_SQL` now has `AND NOT is_user_locked`. |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `import_service.py` | `categorizer/engine.py` | `categorize_rows` after `fetch_for_categorize` inside `begin()` block | VERIFIED | Confirmed in code |
| `import_service.py` | `transaction_repo.py` | `fetch_for_categorize` with `NOT is_user_locked` | VERIFIED | SQL at lines 141-151 confirmed |
| `rules_history.py` | `categorizer/engine.py` | `categorize_rows` in `_compute_in_session` | VERIFIED | `updates = categorize_rows(rows, rules)` — same code path for preview and commit |
| `rules_history.py` | `transaction_repo.py` | `fetch_all_for_categorize(for_update=True)` inside `session.begin()` + `apply_categories` | VERIFIED | `commit` calls `_compute_in_session(..., lock=True)` which calls `fetch_all_for_categorize(account_id, for_update=True)`; write-back in same transaction |
| `routes_categories.py` | `category_repo.py` | `reference_counts` pre-check before delete → 409 | VERIFIED | Confirmed |
| `routes_rules.py` | `categorizer/predicate.py` | `RulePredicate` validated at request parse | VERIFIED | `RuleCreateIn.predicate: RulePredicate` confirmed |
| `main.py` | `routes_categories.py` | `app.include_router` | VERIFIED | Lines 42, 146 confirmed |
| `main.py` | `routes_rules.py` | `app.include_router` | VERIFIED | Lines 45, 147 confirmed |
| `routes_rules.py` | `rules_history.py` | `RulesHistoryService` via `get_rules_history_service` | VERIFIED | Confirmed |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `import_service.py` Step 4b | `updates` (categorize result) | `RuleRepo.list_active_ordered()` → `fetch_for_categorize` → `engine.categorize_rows` | Yes — real seeded rules from DB, real transaction rows | FLOWING |
| `rules_history.py._compute_in_session` | `diff` (preview changes) | `RuleRepo.list_active_ordered()` → `fetch_all_for_categorize(for_update=lock)` → `engine.categorize_rows` | Yes — real DB rows, engine produces real diffs | FLOWING |
| `transaction_repo.apply_categories` | UPDATE results | `updates` list from engine, filtered by `AND NOT is_user_locked` in WHERE | Yes — writes to real rows; locked rows refused at write level | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite passes | `uv run pytest -q` | 158 passed in 7.28s | PASS |
| `_APPLY_CATEGORY_SQL` has lock guard | `grep "AND NOT is_user_locked" transaction_repo.py` | Line 287: `WHERE id = :tid AND NOT is_user_locked` | PASS |
| `FOR UPDATE` SQL present | `grep "FOR UPDATE" transaction_repo.py` | Line 220: `FOR UPDATE` in `_FETCH_ALL_FOR_CATEGORIZE_FOR_UPDATE_SQL` | PASS |
| `commit` uses single `session.begin()` | `grep "session.begin" rules_history.py` | Line 143: `async with self._session_factory() as session, session.begin():` | PASS |
| `commit` calls `_compute_in_session` with `lock=True` | `grep "lock=True" rules_history.py` | Line 144: `await self._compute_in_session(session, account_id, lock=True)` | PASS |
| Regression test for WR-02 exists and passes | `uv run pytest -q -k test_apply_categories_writeback_refuses_locked_row` | 1 passed | PASS |
| No eval/exec/regex in categorizer | `grep -rEn "eval\(|exec\(|re\.compile|^import re|^from re " src/finance_bro/categorizer/` | (no output) | PASS |
| No f-string SQL | `grep -n "f\"SELECT\|f'SELECT\|f\"UPDATE\|f'UPDATE" transaction_repo.py category_repo.py rule_repo.py` | (no output) | PASS |
| Both routers in main.py | `grep -n "routes_categories\|routes_rules" src/finance_bro/main.py` | Lines 42, 45 (import) + 146, 147 (include) | PASS |

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| CAT-01 | 04-01, 04-02, 04-03 | Rules engine with composable predicates: merchant substring, mcc/originalMcc, amount sign/range, account, currency, counterparty IBAN/EDRPOU, comment, hold flag | SATISFIED (regex deferred by design) | D-05 explicitly narrows regex to v2; `ICONTAINS`, `EQUALS`, `IN_INT`, `IN_STR`, `AMOUNT_SIGN`, `AMOUNT_RANGE`, `HOLD_IS` ops cover all CAT-01 fields except regex. Context doc §Deferred items confirms this is intentional. |
| CAT-02 | 04-01, 04-02 | User-controlled rule priority list; first-match-wins on category | SATISFIED | `list_active_ordered()` ORDER BY priority ASC, id ASC; engine iterates in that order; `PATCH /api/rules/reorder` with two-phase collision-safe rewrite |
| CAT-03 | 04-02 | Default ~15-category taxonomy seeded from MCC groups; user-editable categories | SATISFIED | 15 categories seeded in migration 0004; `GET/POST/PATCH/DELETE /api/categories` fully implemented |
| CAT-04 | 04-01, 04-03, 04-04 | `category_source` and `is_user_locked` columns; locked rows skipped by every categorizer re-run | SATISFIED | Three independent guards: SQL filter (`NOT is_user_locked`) on fetch, engine SKIP sentinel, and `AND NOT is_user_locked` in write-back WHERE clause. All three proven by tests. The commit now performs recompute + token check + write in one `session.begin()` with FOR UPDATE row locking — transaction-safe, not merely probabilistic. |
| CAT-05 | 04-04 | Run-rules-on-history with diff preview before commit | SATISFIED | `POST /api/rules/run/preview` returns full diff + token; `POST /api/rules/run/commit` recomputes token inside its write transaction, applies on match, returns 409 on stale. |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/finance_bro/api/schemas.py` | 94 | `TODO: add a separate updated_in_place column` | INFO | Pre-existing (introduced in Phase 2 commit `db1ef0d`); references deferred v1.5 work. Not a Phase 4 debt marker. Not introduced by commit 9c89269. |

No blockers. The CR-01 and WR-02 anti-patterns from the initial report are resolved by commit 9c89269.

---

### Human Verification Required

None. All success criteria are testable programmatically. The CR-01/WR-02 fix is fully observable in source code and proven by the deterministic regression test.

---

### Gaps Summary

No gaps remain.

The single gap from the initial verification (CR-01/WR-02 TOCTOU concurrency hazard in `RulesHistoryService.commit` + missing `AND NOT is_user_locked` guard in `_APPLY_CATEGORY_SQL`) was closed by commit 9c89269. The lock invariant is now held transaction-safely: recompute, token comparison, and write-back occur in one `session.begin()` context with `FOR UPDATE` row locking, and the write-back physically refuses locked rows at the SQL layer. All 158 tests pass.

---

_Verified: 2026-05-31T09:30:00Z_
_Verifier: Claude (gsd-verifier)_
_Re-verification of: 2026-05-30 gaps_found verdict_
_Gap closed by: commit 9c89269 (fix(04): close TOCTOU + lock-clobber gap in run-rules-over-history)_
