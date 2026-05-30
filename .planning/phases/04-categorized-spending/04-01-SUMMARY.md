---
phase: 04-categorized-spending
plan: 01
subsystem: categorization-foundation
tags: [migration, categorizer, rules-engine, predicate-ast, fk]
requires:
  - "transactions table with category_id/category_source/is_user_locked/mcc/raw_payload (Phase 1/2)"
  - "frozen-by-omission upsert in TransactionRepo.insert_many (D-09 DB backing)"
provides:
  - "categories + rules tables; transactions.category_id FK ON DELETE RESTRICT (D-03/D-15)"
  - "seeded ~15-category taxonomy + 11 MCC default rules (D-01/D-04)"
  - "pure categorizer package: predicate AST, field resolver, interpreter, engine"
  - "categorize_row / categorize_rows reused verbatim by Plans 03 (import) + 04 (history) (D-11)"
affects:
  - "Plan 02 (category/rule CRUD + delete guard) builds on these tables"
  - "Plan 03 (auto-categorize-on-import) reuses engine.categorize_rows"
  - "Plan 04 (run-over-history) reuses engine.categorize_rows + token handshake"
tech-stack:
  added: []
  patterns:
    - "Pydantic v2 discriminated-union AST (Field discriminator='op') for the closed predicate vocabulary"
    - "match-statement interpreter over the union — no eval/exec/regex (T-4-eval)"
    - "frozen dataclass RowView resolver, raw_payload.get() never-raise discipline (D-08)"
    - "alembic op.bulk_insert + parameterized op.execute subselect seed (mirrors 0003)"
key-files:
  created:
    - alembic/versions/0004_categorized_spending.py
    - src/finance_bro/categorizer/__init__.py
    - src/finance_bro/categorizer/predicate.py
    - src/finance_bro/categorizer/fields.py
    - src/finance_bro/categorizer/interpreter.py
    - src/finance_bro/categorizer/engine.py
    - tests/test_migration_0004.py
    - tests/test_categorizer_interpreter.py
    - tests/test_field_resolver.py
    - tests/test_engine_first_match.py
    - tests/test_no_eval_in_categorizer.py
  modified:
    - src/finance_bro/db/models.py
    - tests/test_hold_cleared_upsert.py
decisions:
  - "priority is UNIQUE (uq_rules_priority) — forbids ties; engine orders priority ASC, id ASC (Pitfall 6)"
  - "MCC coverage = 11 seeded rule rows, no MCC_MAP constant (D-04 / Pitfall 3)"
  - "SKIP is a singleton sentinel distinct from None (locked vs no-match — D-09/D-02)"
  - "no-eval guard also asserts package purity (no sqlalchemy/AsyncSession/session token)"
metrics:
  duration_min: 42
  completed: "2026-05-30"
  tasks: 2
  files: 13
  tests_added: 26
---

# Phase 4 Plan 01: Categorization Foundation Summary

Migration 0004 (categories + rules tables, the `transactions.category_id` FK ON DELETE RESTRICT, and a seeded ~15-category taxonomy + 11 MCC default rules) plus the pure, eval-free, lock-respecting, first-match-wins `categorizer/` package — the single categorization mechanism both later slices reuse verbatim.

## What Was Built

### Task 1 — Migration 0004 + Category/Rule ORM models + migration test (commit `48b278b`)
- `alembic/versions/0004_categorized_spending.py`: creates `categories` (id/name/color/created_at, UNIQUE name) and `rules` (id/priority/category_id FK RESTRICT/predicate JSONB/description/created_at, UNIQUE priority), the `ix_rules_priority` index, and the `fk_transactions_category` FK (RESTRICT) on the pre-existing all-NULL `transactions.category_id`. Seeds 15 categories via `op.bulk_insert` and 11 MCC rules via parameterized `op.execute` with a `(SELECT id FROM categories WHERE name=...)` subselect so the seed is order-robust. Each seeded predicate is the closed-op AST shape `{"all":[{"op":"in_int","field":"mcc","values":[...]},{"op":"amount_sign","sign":"debit"}]}`. No `MCC_MAP` constant anywhere (D-04 / Pitfall 3).
- `src/finance_bro/db/models.py`: added `class Category` and `class Rule` ORM models (mirroring the TrackedFxCurrency seed-table shape) and the `ForeignKey("categories.id", ondelete="RESTRICT")` arg on `Transaction.category_id`.
- `tests/test_migration_0004.py`: asserts the 15/11 seed counts, the FK presence + RESTRICT delete rule (`pg_constraint.confdeltype == 'r'`), that every seeded rule resolves to a real category, that `priority` is unique, the predicate AST shape, and a full base→head downgrade/upgrade round-trip.

### Task 2 — Pure categorizer package (TDD; RED `cafb076`, GREEN `e8e4f8e`)
- `predicate.py`: Pydantic discriminated-union AST — `IContains`/`Equals`/`InInt`/`InStr`/`AmountSign`/`AmountRange`/`HoldIs`, each with a `Literal op` tag; `Condition = Annotated[... , Field(discriminator="op")]`; `RulePredicate.all: list[Condition]` with `min_length=1` (flat AND-only, D-06). Field vocabularies are closed `Literal` enums encoding D-08's column-vs-raw_payload split. Amount bounds are integer minor units only.
- `fields.py`: frozen `RowView` dataclass + `make_row(...)` helper. Column-backed fields read attributes; raw_payload-backed fields (`comment`/`counter_iban`/`counter_edrpou`/`original_mcc`) read `raw_payload.get(...)` and return None on absence (Pitfall 5). `original_mcc` is int-coerced, None on non-coercible.
- `interpreter.py`: `eval_condition(cond, row)` is a `match` over the union; a None field value yields False (no match), never an exception. No `re`, no `eval`/`exec`.
- `engine.py`: `CompiledRule` (priority/id/category_id/predicate) + `categorize_row` returning `SKIP` for locked rows (D-09 defense-in-depth), the first matching rule's category_id (CAT-02), or None (no match → NULL, D-02). `categorize_rows` is the batch entry point both Plan 03 and Plan 04 call verbatim — it omits SKIP rows from its output.
- Four pure test files (interpreter truth table incl. the canonical ATB-vs-Silpo example, field resolver + absent-key safety, first-match-wins + lock skip, and a static no-eval/no-DB-import guard).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] FK addition broke a pre-existing test that inserted a fictitious category_id**
- **Found during:** Task 1 verification (full-suite run after adding the FK).
- **Issue:** `tests/test_hold_cleared_upsert.py::test_cleared_updates_in_place` set `category_id = 42` directly to prove the frozen-by-omission importer never resets it. The literal `42` only worked because there was no FK; migration 0004's `fk_transactions_category` (the explicit goal of this plan, D-03) correctly rejected it with a `ForeignKeyViolation`.
- **Fix:** The test now selects a real seeded category id (`SELECT id FROM categories WHERE name='Groceries'`) and asserts the importer leaves *that* id intact. The test's intent (category_id frozen by the importer) is fully preserved; only the fictitious value changed. The `_truncate_tx` fixture truncates only `transactions, accounts` (CASCADE), so seeded categories survive.
- **Files modified:** `tests/test_hold_cleared_upsert.py`
- **Commit:** `e8e4f8e` (committed with the GREEN implementation it depends on).

**2. [Rule 1 - Bug] No-eval purity guard caught the word "session" in package docstrings**
- **Found during:** Task 2 RED/GREEN runs.
- **Issue:** The `test_no_eval_in_categorizer` purity guard greps for a bare `\bsession\b` token; the `__init__.py` and `fields.py` docstrings used the word "session" in prose ("no session imports"), tripping the guard.
- **Fix:** Reworded the two docstrings to "no DB connection imports" — the guard stays strict (it would still catch a real `session` usage) and the package prose no longer trips it. This is the guard working as intended.
- **Files modified:** `src/finance_bro/categorizer/__init__.py`, `src/finance_bro/categorizer/fields.py`
- **Commit:** `e8e4f8e`.

No architectural (Rule 4) deviations. No authentication gates. Phase 4 installs zero packages (T-4-SC accept — vacuously satisfied).

## Verification

- Migration test: `uv run pytest tests/test_migration_0004.py -x` → 3 passed.
- Categorizer suite: `uv run pytest tests/test_categorizer_interpreter.py tests/test_field_resolver.py tests/test_engine_first_match.py tests/test_no_eval_in_categorizer.py -x` → 23 passed.
- Foundation suite (plan `<verification>`): 26 passed.
- Full suite `uv run pytest -q` → **137 passed, 0 failed** (Phases 1-3 unaffected — additive DDL + new pure package).
- Acceptance greps: `grep -rn "MCC_MAP\|MCC_TO_CATEGORY" src/finance_bro/` → none; `grep -rEn "eval\(|exec\(|re\.compile|^import re|^from re " src/finance_bro/categorizer/` → none; `grep -rn "import.*sqlalchemy\|AsyncSession\|session" src/finance_bro/categorizer/` → none.
- `ruff check` + `ruff format --check` + `basedpyright` clean on all new/modified source.

## TDD Gate Compliance

Task 2 followed RED → GREEN:
- RED `cafb076` (`test(04-01): ...`): four pure test files committed against not-yet-implemented stubs.
- GREEN `e8e4f8e` (`feat(04-01): ...`): implementation that turns them green.
No separate REFACTOR commit was needed.

## Known Stubs

None. The categorizer engine is fully implemented; the migration seeds real data. (Category/Rule CRUD endpoints, the import-step wiring, and the history sweep are intentionally out of this plan — Plans 02/03/04.)

## Threat Flags

None. All security-relevant surface introduced (the predicate interpreter, the field resolver, the FKs) is covered by the plan's `<threat_model>` mitigations and proven by the new tests (T-4-eval, T-4-payload, T-4-lock, T-4-fk).

## Self-Check: PASSED

All 11 created files present on disk; all 3 commits (`48b278b`, `cafb076`, `e8e4f8e`) present in git history.
