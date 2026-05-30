---
phase: 04-categorized-spending
reviewed: 2026-05-30T00:00:00Z
depth: standard
files_reviewed: 28
files_reviewed_list:
  - alembic/versions/0004_categorized_spending.py
  - src/finance_bro/api/deps.py
  - src/finance_bro/api/routes_categories.py
  - src/finance_bro/api/routes_rules.py
  - src/finance_bro/api/schemas.py
  - src/finance_bro/categorizer/__init__.py
  - src/finance_bro/categorizer/engine.py
  - src/finance_bro/categorizer/fields.py
  - src/finance_bro/categorizer/interpreter.py
  - src/finance_bro/categorizer/predicate.py
  - src/finance_bro/db/category_repo.py
  - src/finance_bro/db/models.py
  - src/finance_bro/db/rule_repo.py
  - src/finance_bro/db/transaction_repo.py
  - src/finance_bro/main.py
  - src/finance_bro/services/import_service.py
  - src/finance_bro/services/rules_history.py
  - tests/test_categories_crud.py
  - tests/test_categorize_on_import.py
  - tests/test_categorizer_interpreter.py
  - tests/test_category_delete_guard.py
  - tests/test_engine_first_match.py
  - tests/test_field_resolver.py
  - tests/test_history_preview_commit.py
  - tests/test_lock_invariant.py
  - tests/test_migration_0004.py
  - tests/test_no_eval_in_categorizer.py
  - tests/test_rules_crud.py
findings:
  critical: 1
  warning: 6
  info: 4
  total: 11
status: issues_found
---

# Phase 4: Code Review Report

**Reviewed:** 2026-05-30T00:00:00Z
**Depth:** standard
**Files Reviewed:** 28
**Status:** issues_found

## Summary

Reviewed the Phase 4 categorized-spending slice: migration 0004, the pure categorizer package (predicate AST / interpreter / fields / engine), the category & rule CRUD routes + repos, the run-rules-over-history preview/commit service, and the import-time categorize wiring.

**Security boundary (predicate interpreter) is sound.** The closed-op discriminated-union AST plus the `match`-based interpreter genuinely avoids `eval`/`exec`/`re`, validates at the Pydantic boundary on the way in (route DTO) and again at compile time (`compile_rules`), and the static guard test enforces purity. All repo SQL is parameterized (`text()` bind params, ORM, `= ANY(:ids)`) — no SQL injection surface found. The D-15 delete guard correctly pre-checks and is backstopped by `ON DELETE RESTRICT`.

**The one BLOCKER is a real TOCTOU/lost-update hole in `RulesHistoryService.commit`** that re-opens the exact lock-clobber and stale-write hazard the staleness token was designed to close, because the recompute and the write happen in two separate sessions/transactions with no row locking. The token certifies state at *read* time, not at *write* time, and `apply_categories` writes blindly without re-checking `is_user_locked`.

The remaining warnings concern unvalidated/partial reorder input, the missing lock guard in the write-back SQL (which amplifies the BLOCKER), and unhandled UNIQUE-constraint collisions on rule priority that surface as 500s.

## Critical Issues

### CR-01: TOCTOU between recompute and write in `commit` re-opens the lost-update / lock-clobber hazard

**File:** `src/finance_bro/services/rules_history.py:122-138`

**Issue:** `commit` is documented (module docstring, line 6-10; method docstring, line 122-129) as closing the lost-update/stale-write hazard by recomputing the token from current state and applying "ONLY when it matches." But the recompute and the apply happen in **two different sessions and two different transactions**:

```python
comp = await self._compute(account_id)          # session A: read rules + rows, build diff + token
if comp.token != token:
    raise StaleRunError(...)
updates = [(c.transaction_id, c.new_category_id) for c in comp.diff]
async with self._session_factory() as session, session.begin():   # session B: write
    await TransactionRepo(session).apply_categories(updates)
```

`_compute` (line 75-105) opens and **closes** its own session before returning. The token therefore certifies the database state *as of session A's read*, which has already committed/closed by the time session B opens. Between the two, any concurrent writer can change the rows:

- The APScheduler import tick (`main.py:99-106`, `ImportService.run_one_card` -> `apply_categories`) runs every 10s and auto-categorizes touched rows.
- A user can lock or manually recategorize a row via another request.

Concretely: a row that is unlocked at session-A read time can be **locked** by the user between the recompute and the write. `apply_categories` (`transaction_repo.py:253-262`) issues `UPDATE transactions SET category_id=:cid, category_source='rule' WHERE id=:tid` with **no `is_user_locked` guard**, so the now-locked row is overwritten — violating the CAT-04 lock invariant that the token handshake claims to protect. Symmetrically, a row's category can change after recompute and the stale diff value is blind-applied (lost update). The token check provides no protection here because nothing holds a lock across the read→write gap.

The integration tests pass only because they are single-threaded and quiescent between `preview` and `commit`; they never exercise a concurrent mutation *inside* the commit's read→write window.

**Fix:** Perform the recompute, token comparison, and write inside **one** transaction, and lock the affected rows for the duration. Restructure so `_compute` can run against a caller-supplied session, then:

```python
async def commit(self, account_id: int, token: str) -> dict[str, int]:
    async with self._session_factory() as session, session.begin():
        # recompute INSIDE the write transaction so the snapshot the token
        # is compared against is the same one the UPDATE sees.
        comp = await self._compute_in_session(session, account_id)
        if comp.token != token:
            raise StaleRunError(...)            # rolls back, writes nothing
        repo = TransactionRepo(session)
        updates = [(c.transaction_id, c.new_category_id) for c in comp.diff]
        await repo.apply_categories(updates)
    return {"applied": len(comp.diff)}
```

and have the in-session read use `SELECT ... FOR UPDATE` on the swept rows (or, minimally, run under `REPEATABLE READ`/`SERIALIZABLE` isolation and let the DB abort the transaction on a concurrent write). Additionally harden `_APPLY_CATEGORY_SQL` to `... WHERE id = :tid AND NOT is_user_locked` (see WR-02) so a row locked mid-window is provably never clobbered even if the isolation guarantee is weakened later.

## Warnings

### WR-01: `reorder` accepts unvalidated / partial id lists and can corrupt priority ordering or collide

**File:** `src/finance_bro/db/rule_repo.py:83-109`, `src/finance_bro/api/routes_rules.py:66-72`, `src/finance_bro/api/schemas.py:194-195`

**Issue:** `reorder(ordered_ids)` blindly rewrites priorities to `1..N` for the supplied ids without verifying that (a) the ids exist, (b) the list contains **all** rules, or (c) there are no duplicates. `RuleReorderIn` only enforces `min_length=1`.

- Passing a **partial** list (e.g. only 3 of 11 rules) renumbers those 3 to priorities `1,2,3`, almost certainly colliding with — or silently leapfrogging — the untouched seeded rules that already occupy low priorities. Phase 2 parks rows at `1_000_000 + rid` then assigns `1..N`; the untouched rules keep their original low priorities, so the final state can have two rules at the same low priority only if the constraint doesn't catch it, or (more likely) the renumber silently changes first-match-wins semantics for rules the caller never intended to touch.
- A non-existent id is a silent no-op UPDATE (0 rows), so the caller gets 204 success for a request that did nothing meaningful.
- Duplicate ids in the list assign multiple priorities to one row (last wins) and skip an intended slot.

**Fix:** Validate in the repo (or route) before writing: fetch the full set of rule ids, assert `set(ordered_ids)` equals the full set (or at least is a subset of existing ids with no duplicates), and reject otherwise:

```python
existing = {r.id for r in await self.list_all()}
if len(set(ordered_ids)) != len(ordered_ids):
    raise ValueError("duplicate rule ids in reorder")
if set(ordered_ids) != existing:
    raise ValueError("reorder must list every rule id exactly once")
```

Map the error to HTTP 422/409 in the route.

### WR-02: `apply_categories` write-back lacks the `NOT is_user_locked` guard it relies on upstream

**File:** `src/finance_bro/db/transaction_repo.py:253-262`

**Issue:** `_APPLY_CATEGORY_SQL` updates purely by `id`, trusting that every id passed in was already filtered to non-locked rows by `fetch_for_categorize` / `fetch_all_for_categorize` and by the engine's SKIP. That is true at *fetch* time, but (per CR-01) the fetch and the write can straddle a concurrent lock. The comment at line 248-252 explicitly notes the SET clause never references `is_user_locked` "because locked rows are excluded upstream" — that assumption is not transaction-safe. Defense-in-depth here is cheap and the lock invariant (CAT-04 / D-09) is a stated hard invariant.

**Fix:** Add the guard to the UPDATE so a row that became locked is never written, regardless of timing:

```python
_APPLY_CATEGORY_SQL = text(
    "UPDATE transactions SET category_id = :cid, category_source = 'rule' "
    "WHERE id = :tid AND NOT is_user_locked"
)
```

### WR-03: Creating/updating a rule with a duplicate priority raises an unhandled `IntegrityError` (HTTP 500)

**File:** `src/finance_bro/api/routes_rules.py:50-63` and `75-94`, `src/finance_bro/db/rule_repo.py:27-42` and `58-78`

**Issue:** `priority` carries `UniqueConstraint("priority", name="uq_rules_priority")` (`models.py:202`). `create_rule` and `update_rule` set the priority directly and `await session.commit()` with no handling for a UNIQUE collision. A client POSTing/PATCHing a rule whose priority already exists triggers a `psycopg.errors.UniqueViolation` -> SQLAlchemy `IntegrityError` -> uncaught -> HTTP 500. This is a foreseeable client error (the UI reorders/edits priorities constantly) that should be a 409, not a server error, and the unflushed/aborted transaction can leave the session in a bad state for the request.

**Fix:** Catch `IntegrityError` in the route (or pre-check the priority) and translate to a 409 with a clear message, e.g.:

```python
from sqlalchemy.exc import IntegrityError
try:
    created = await repo.create(...)
    await session.commit()
except IntegrityError as e:
    await session.rollback()
    raise HTTPException(status_code=409, detail="priority already in use") from e
```

### WR-04: D-15 delete guard has a check-then-act race against the FK backstop

**File:** `src/finance_bro/api/routes_categories.py:61-79`

**Issue:** `delete_category` calls `reference_counts(cid)` and, only if both counts are zero, proceeds to `repo.delete(cid)`. Between the count read and the delete, a concurrent request (or the import tick assigning `category_id`) could create a transaction or rule referencing the category. The `ON DELETE RESTRICT` FK is the real backstop and will raise — but that raise is an unhandled `IntegrityError` -> HTTP 500, not a clean 409. The handler treats the FK as documentation but never catches it. (This is lower severity than CR-01 because data integrity is preserved by the FK; only the error surface is wrong.)

**Fix:** Wrap the delete in a try/except for `IntegrityError` and return 409 (re-reading counts for the detail body), so the race resolves to the same 409 the pre-check would have produced rather than a 500.

### WR-05: Import path stamps `category_source='rule'` on touched no-match rows, overwriting prior source

**File:** `src/finance_bro/services/import_service.py:108-112`, `src/finance_bro/db/transaction_repo.py:257-262`

**Issue:** On import, `fetch_for_categorize` returns every touched non-locked row; `categorize_rows` returns `(id, None)` for rows matching no rule; `apply_categories` then writes `category_id=NULL, category_source='rule'` for those rows. For a freshly-inserted row this is the intended D-02 "evaluated, matched nothing" semantics. But a re-imported (touched) row that was previously categorized via some other mechanism and now matches nothing will have its `category_source` reset to `'rule'` while its `category_id` is nulled. Since the row is unlocked this is arguably allowed, but it means a re-import can silently *de-categorize* a previously-categorized unlocked row and relabel its provenance, which is surprising and not covered by any test (existing tests only assert the fresh-insert and locked-row cases).

**Fix:** Confirm this is intended (D-02). If de-categorizing unlocked rows on re-import is not desired, only write `(id, cid)` updates where `cid is not None`, or skip rows whose current category was set by a different source. At minimum add a test pinning the intended behavior for "touched, previously-categorized, now no-match, unlocked."

### WR-06: `commit` returns `applied` = diff length even though some UPDATEs may affect zero rows

**File:** `src/finance_bro/services/rules_history.py:135-138`, `src/finance_bro/db/transaction_repo.py:257-262`

**Issue:** `commit` returns `{"applied": len(comp.diff)}` — the *intended* count, not the *actual* number of rows written. `apply_categories` ignores each UPDATE's `rowcount`. If a diffed row was deleted, locked (WR-02 fix), or otherwise no longer matches the WHERE clause, the reported `applied` overstates what happened. The API contract (`RunCommitOut.applied`) reads as "rows actually applied."

**Fix:** Have `apply_categories` accumulate and return `sum(result.rowcount for ...)`, and surface that as `applied`. This also makes the WR-02 lock guard observable to the caller.

## Info

### IN-01: `CategoryUpdateIn` cannot clear `color` back to NULL

**File:** `src/finance_bro/api/schemas.py:156-160`, `src/finance_bro/db/category_repo.py:36-47`

**Issue:** `update` only assigns `color` when `color is not None` (line 44-45). Because `None` is indistinguishable from "field omitted" in the PATCH body, there is no way to reset a category's color to NULL once set. Same pattern for `name` (though name is NOT NULL so that is fine). Likely acceptable for v1; note it as a known limitation. A `model_fields_set` check or a sentinel would be needed to support explicit clearing.

### IN-02: `RowView.int_field` coerces non-int `originalMcc` via `int(v)` which truncates floats silently

**File:** `src/finance_bro/categorizer/fields.py:60-66`

**Issue:** `int(v)` on a float payload value (e.g. `4829.0`, or worse `4829.9`) truncates rather than rejecting. Mono `originalMcc` is an integer in practice, so impact is low, but a float string like `"4829.5"` raises `ValueError` and returns None (fine) while a JSON number `4829.9` would silently become `4829`. Consider rejecting non-integral numeric values rather than truncating.

### IN-03: `numeric_to_alpha(int(code))` could swallow a real mapping bug as "no match"

**File:** `src/finance_bro/db/transaction_repo.py:53-63`

**Issue:** `_op_currency_alpha` catches `(ValueError, TypeError)` and returns None, which is correct for absent/garbage codes, but it also masks any unexpected exception type from `numeric_to_alpha` and any KeyError-style miss inside it. Low risk on the read path (the comment explains the never-raise intent), but a genuinely unmapped-but-valid numeric code is indistinguishable from malformed input. Acceptable for v1; flagged for awareness.

### IN-04: `_Skip` singleton `__new__` is not concurrency-safe, but is only constructed at import

**File:** `src/finance_bro/categorizer/engine.py:26-40`

**Issue:** The `_Skip` singleton uses a non-locked check-then-set in `__new__`. Under concurrent first construction this could create two instances. In practice `SKIP = _Skip()` is created once at module import (single-threaded import lock), so this is benign. No action needed; noted because identity comparisons (`result is SKIP`) depend on the singleton property.

---

_Reviewed: 2026-05-30T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
