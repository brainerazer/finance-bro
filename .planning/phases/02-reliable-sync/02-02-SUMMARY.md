---
phase: 02-reliable-sync
plan: 02
subsystem: db-upsert + api-schema
tags: [phase-02, upsert, hold-cleared, on-conflict-do-update, transaction-out, d-10, frozen-by-omission]

# Dependency graph
requires:
  - phase: 02-reliable-sync
    plan: 01
    provides: accounts.mono_type column, ImportRunRepo, SchedulerStateRepo, fixtures (statement_with_hold + statement_cleared_followup), conftest TRUNCATE extension
  - phase: 01-first-real-transaction
    provides: TransactionRepo skeleton, partial unique index uq_transactions_account_source_tx, CanonicalTransaction dataclass, ImportService, conftest+testcontainers harness
provides:
  - "TransactionRepo.insert_many: ON CONFLICT DO UPDATE with EXACTLY THREE EXCLUDED columns (hold, amount_minor, raw_payload); returns (inserted, updated_in_place) via the (xmax = 0) RETURNING trick"
  - "CanonicalTransaction extended with hold/description/mcc (defaulted) — bridges to 02-03's MonobankImporter wiring"
  - "TransactionOut.hold: bool surfaced on GET /api/transactions"
  - "AccountOut.mono_type: str | None surfaced on GET /api/accounts (status-surface input for 02-04)"
  - "D-10 frozen-by-omission invariant proven by tests/test_hold_cleared_upsert.py — central correctness test for Phase 2"
  - "ImportService call-site adapter: inserted_total = inserted + updated; ImportResultOut Phase-1 shape preserved until 02-04 reshapes the route (D-16)"
affects: [02-03-scheduler-backfill, 02-04-status-surface]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ON CONFLICT DO UPDATE with EXCLUDED-style SET clause restricted to a closed allowlist of mutable columns (RESEARCH.md Pattern 1) — D-10 frozen-by-omission privacy/integrity invariant"
    - "(xmax = 0) RETURNING trick to distinguish inserts from updates in a single round-trip (RESEARCH.md Pattern 3 + Pitfall 6)"
    - "Backwards-compat dataclass extension via defaulted fields — frozen=True dataclass + new fields with defaults preserves Phase 1 callers (Plan 02-03 will populate the fields)"
    - "Per-test autouse TRUNCATE for upsert tests (mirror of 02-01's per-test isolation in repo tests) — multiple tests touch the same source_tx_id 'HOLD-FIXTURE-ID-1', so isolation must be per-test, not per-account"
    - "Call-site adapter to fold a tuple return into a Phase-1 single-int field (inserted_total = inserted + updated) — keeps the route shape stable across the wave-2/wave-4 transition"

key-files:
  created:
    - tests/test_hold_cleared_upsert.py
  modified:
    - src/finance_bro/importers/base.py
    - src/finance_bro/db/transaction_repo.py
    - src/finance_bro/services/import_service.py
    - src/finance_bro/api/schemas.py
    - tests/test_idempotency.py
    - tests/test_transactions_route.py

key-decisions:
  - "SET clause restricted to EXACTLY hold/amount_minor/raw_payload — D-10 frozen-by-omission invariant. Test test_cleared_updates_in_place enforces this at the SQL layer (not by convention) by mutating six manual-edit columns post-insert and asserting they survive the cleared upsert. Adding any other column to set_={...} is a bug that silently breaks Phase 1's Pitfall-10 promise (importer never overwrites manual edits)."
  - "ImportService.run_one_card folds (inserted, updated_in_place) into a single inserted_total because Phase 1's ImportResultOut has only an `inserted` field. Plan 02-04 owns the D-16 reshape that introduces ImportEnqueuedOut etc.; until then, routes_import.py is intentionally untouched and tests/test_import_route.py passes UNCHANGED."
  - "test_idempotency.py second-import assertion changed from inserted=0/skipped=2 to inserted=2/skipped=0. Under DO UPDATE the second import touches both rows via UPDATE rather than skipping them via DO NOTHING; the user-facing single-row invariant (SC#3) is unchanged — exactly what the existing `len(r.json()) == 2` line proves at the bottom of the test."
  - "CanonicalTransaction.hold/description/mcc default to False/None/None so Phase 1's MonobankImporter (which doesn't yet pass them) keeps working. Plan 02-03 lands the actual `MonobankImporter.fetch_statement` wiring; until then, getattr-style fallback isn't even needed because dataclass defaults are first-class."
  - "tests/test_hold_cleared_upsert.py uses an autouse TRUNCATE fixture (transactions+accounts CASCADE) rather than per-test unique account ids. Reason: all three tests use source_tx_id='HOLD-FIXTURE-ID-1' (the canonical 02-01 fixture id), so even with unique account ids per test, a stray row from a sibling test could surface in a SELECT WHERE source_tx_id='HOLD-FIXTURE-ID-1' — the truncate kills the ambiguity at the root."
  - "Comment that initially read 'absent from set_={...}' was reworded to 'absent from the on-conflict update clause below' so the plan's grep gate (`grep -c 'set_=' transaction_repo.py | grep -qE '^1$'`) sees exactly one occurrence (the keyword arg). 02-01 SUMMARY documents the same kind of false-positive deviation."

patterns-established:
  - "Frozen-by-omission tests: mutate manual-edit columns post-insert, run the upsert, assert all manual-edit columns survive. The list (is_user_locked, category_id, category_source, description, mcc, attributed_day) is the closed set Phases 4-6 will write; together with the structural-frozen list (currency, time, account_id, source_tx_id, created_at, is_deleted) they enumerate the entire `transactions` schema minus the three mutable columns."
  - "Inline Mono-payload→CanonicalTransaction conversion in tests (mirror of MonobankImporter.fetch_statement mapping) — lets the unit test exercise the upsert path without dragging in the HTTP layer; same fixture, same code path, +HTTP layer is what 02-03 will add."

requirements-completed: [ING-05]

# Metrics
duration: ~12 min
completed: 2026-05-10
---

# Phase 02 Plan 02: Hold-Aware Upsert Summary

**TransactionRepo.insert_many switches from ON CONFLICT DO NOTHING to ON CONFLICT DO UPDATE with EXACTLY THREE EXCLUDED columns (hold, amount_minor, raw_payload); D-10 frozen-by-omission invariant proven by a CENTRAL test that mutates six manual-edit columns post-insert and asserts they survive the cleared upsert; TransactionOut.hold and AccountOut.mono_type surfaced on the API.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-05-10 (Task 1 commit `a474f1d`)
- **Completed:** 2026-05-10 (Task 2 commit `7b4db77`)
- **Tasks:** 2
- **Files touched:** 7 (1 created, 6 modified across `src/`, `tests/`)
- **Tests:** full suite green — 57 passed, 0 failed

## Accomplishments

- **`insert_many` rewritten with the verbatim RESEARCH.md Pattern 1 + Pattern 3 idiom.** Signature `tuple[int, int]`. The SET clause contains exactly `hold`, `amount_minor`, `raw_payload` — every other column FROZEN BY OMISSION. The `(xmax = 0)` returning expression with `literal_column` distinguishes inserts (xmax=0 on a freshly-inserted row) from updates (xmax=current xact id) in a single SQL round-trip.
- **D-10 invariant proven at the SQL layer.** `tests/test_hold_cleared_upsert.py::test_cleared_updates_in_place` is the central test of Phase 2: it inserts a hold:true row, simulates a Phase-4/5/6 manual edit by mutating six columns (is_user_locked, category_id, category_source, description, mcc, attributed_day), then runs a cleared upsert with importer-supplied "from importer cleared" / mcc=9999 / different occurred_at. The test asserts: 3 mutated columns reflect the cleared payload, 6 manual-edit columns survive, 6 structural columns survive (id, account_id, source_tx_id, currency, time, created_at, is_deleted). Failure of any frozen-field assertion = SET clause leaked = Pitfall-10 broken at the SQL layer, not at code-review time.
- **CanonicalTransaction extended additively** — three new fields with defaults, so Phase 1 importer code (which doesn't yet construct hold/description/mcc) keeps working unchanged. Plan 02-03 will land the importer wiring; Plan 02-02 only plumbs the dataclass fields through.
- **API surfaces wired:** `TransactionOut.hold` (always populated — `Transaction.hold` is non-null with server_default 'false'), `AccountOut.mono_type` (nullable for non-card source_kinds). The route layer hydrates both fields automatically via `ConfigDict(from_attributes=True)` — no route changes needed.
- **ImportService adapter keeps Phase 1 contract intact.** `inserted_total = inserted + updated`, so `ImportResultOut.inserted` reflects every touched row (whether INSERT or UPDATE). `tests/test_import_route.py` passes UNCHANGED — Plan 02-04 will reshape the route per D-16, this plan stays surgical.
- **Round-trip e2e test** anchors 02-03's integration: `tests/test_hold_cleared_upsert.py::test_e2e_hold_then_cleared` loads `tests/fixtures/statement_with_hold.json` and `statement_cleared_followup.json` (the 02-01 fixtures), converts each to a CanonicalTransaction inline, and asserts the round-trip produces a single row with the cleared payload. Same fixtures, same upsert path; 02-03 will add the HTTP+scheduler layer on top.

## Task Commits

1. **Task 1: ON CONFLICT DO UPDATE + tuple return + ImportService adapter + idempotency-test refresh** — `a474f1d` (feat)
2. **Task 2: TransactionOut.hold + AccountOut.mono_type + new test_hold_cleared_upsert.py + extended test_transactions_route.py** — `7b4db77` (feat)

## Files Created/Modified

### Created
- `tests/test_hold_cleared_upsert.py` — three test functions (`test_hold_inserted_with_flag`, `test_cleared_updates_in_place`, `test_e2e_hold_then_cleared`) covering ING-05 + SC#3 + D-10. Autouse `_truncate_tx` keeps tests independent (all three reuse `source_tx_id='HOLD-FIXTURE-ID-1'`). Inline `_ct_from_mono_item` helper mirrors `MonobankImporter.fetch_statement` mapping for the e2e test.

### Modified
- `src/finance_bro/importers/base.py` — `CanonicalTransaction` gains `hold: bool = False`, `description: str | None = None`, `mcc: int | None = None`. Defaults preserve Phase 1 callers; 02-03 wires Mono payloads into these fields.
- `src/finance_bro/db/transaction_repo.py` — module docstring rewritten to reflect DO UPDATE + D-10. New imports: `literal_column`. `insert_many` returns `tuple[int, int]`; rows dict gains `description/mcc/hold`; `on_conflict_do_update(...)` SET clause restricted to `hold/amount_minor/raw_payload`; `.returning(Transaction.id, literal_column("(xmax = 0)").label("inserted"))` enables insert-vs-update counting.
- `src/finance_bro/services/import_service.py` — module docstring updated to reflect Phase 2 DO UPDATE semantics. Call site at the upsert: `inserted, updated = await TransactionRepo(session).insert_many(card.id, items)`; `inserted_total = inserted + updated` flows into Phase 1 `ImportResult`. `ImportResultOut` shape unchanged.
- `src/finance_bro/api/schemas.py` — `AccountOut.mono_type: str | None = None` (nullable; populated by 02-01's migration backfill + 02-03's wiring). `TransactionOut.hold: bool` (always populated; server_default 'false' from Phase 1 schema).
- `tests/test_idempotency.py` — second-import assertion updated for DO UPDATE semantics (inserted=2, skipped_duplicates=0). The user-visible single-row invariant (SC#3) — proven by the existing `len(r.json()) == 2` line at the bottom — stays intact.
- `tests/test_transactions_route.py` — adds `from sqlalchemy import text` and `test_hold_field_in_response`. Seeds via raw SQL after the conftest TRUNCATE; asserts both hold:true and hold:false rows surface correctly through the JSON boundary; regression-guards Phase 1 fields (amount_minor, raw_payload).

## Decisions Made

See `key-decisions:` in frontmatter for the full list. Highlights:

- **D-10 enforcement is at the SQL layer, not in code review.** The CENTRAL test mutates six manual-edit columns and asserts they survive the cleared upsert. Adding any other column to `set_={...}` instantly trips this test — there is no review-and-merge path that lets a regression slip through.
- **Phase 1 ImportResultOut shape preserved** via the `inserted_total` adapter. Plan 02-04 owns the D-16 reshape; this plan stays surgical and `tests/test_import_route.py` passes UNCHANGED.
- **`test_idempotency.py` assertion refresh** is the only Phase 1 test that needed adjustment, and the adjustment is small: the contract that "second import is a user-visible no-op" is what `len(r.json()) == 2` already enforces; the route's `inserted` counter just changed from "rows newly created" to "rows touched by the upsert (insert or update-in-place)". Plan 02-04's reshape will distinguish the two.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — false-positive grep gate] `set_=` count of 2 instead of 1 due to a comment mention**

- **Found during:** Task 1 verify step (`grep -c "set_=" transaction_repo.py | grep -qE '^1$'`).
- **Issue:** A comment inside the rows-dict comprehension read "they are absent from set_={...}." The grep gate counts literal occurrences and saw 2 (the comment plus the keyword arg).
- **Fix:** Reworded the comment to "absent from the on-conflict update clause below — D-10 frozen-by-omission." Meaning preserved; literal token count drops to 1.
- **Files modified:** `src/finance_bro/db/transaction_repo.py`
- **Verification:** Re-ran the grep; returns 1. Re-ran the targeted test set; 14 passed.
- **Committed in:** `a474f1d` (Task 1) — same commit as the original code.
- **Note:** This is the same false-positive class that 02-01 SUMMARY logged as Deviation #2 ("Anti-pattern grep false positive in docstrings"). Future plans should consider tightening grep gates to exclude comments when the same token legitimately appears in "we're explicitly NOT doing this" notes.

**2. [Rule 1 — sanity grep noise unrelated to this plan]**

- **Found during:** Plan-level verification step 8 (`grep -RE "(routes_status|routes_backfill|SchedulerRunner|MonoAuthError)" src/ | wc -l` should be 0).
- **Issue:** Returned 1 — a docstring mention of `SchedulerRunner` inside `src/finance_bro/db/import_run_repo.py` (landed by 02-01).
- **Fix:** None needed — the hit is pre-existing wave-1 scope and was introduced before this plan ran. Confirmed by `git diff eb0ef0a..HEAD -- src/ | grep -E "^\+.*(routes_status|routes_backfill|SchedulerRunner|MonoAuthError)"` returning empty: this plan added zero scope-leak hits.
- **Files modified:** none
- **Action:** Documenting as a deviation log so the next executor doesn't waste time investigating; 02-01's docstring is a forward reference to 02-03's runner, not scope leak from this plan.

**Total deviations:** 2 (1× Rule 1 grep-false-positive fix, 1× verification-noise documentation). No architectural decisions, no Rule 4 escalations.
**Impact on plan:** None on the shipped artifacts. Plan executed substantively as written.

## Issues Encountered

None blocking. The grep-gate false positive in (1) was the only friction; resolved in the same commit it appeared in.

## Empirical Observations

- **`(xmax = 0)` works as documented.** PostgreSQL 17 (the testcontainer image) returns `(xmax = 0)::boolean` correctly under `RETURNING`; the cast happens implicitly because `literal_column("(xmax = 0)")` already produces a boolean expression. No `cast(... AS boolean)` wrapping needed.
- **`stmt.excluded.<col>` is fully typed under SQLAlchemy 2.0.49.** No mypy/basedpyright friction in the new SET clause; this matches the typing story documented in CLAUDE.md (psycopg 3 + SQLAlchemy 2 with the `postgresql+psycopg://` URL).
- **The 02-01 fixtures are immediately reusable for unit tests.** The Mono payload mapping (Unix-epoch `time` → `datetime.fromtimestamp(..., tz=UTC)`, numeric `currencyCode` → `numeric_to_alpha`, `id` → `source_tx_id`, `amount` → `amount_minor`) is identical to `MonobankImporter.fetch_statement` modulo the `hold/description/mcc` extension. The inline `_ct_from_mono_item` helper in `test_hold_cleared_upsert.py` makes this explicit.
- **Tests using `session_factory` directly need their own truncate fixture** — same pattern 02-01 surfaced for `test_import_run_repo.py`. The conftest `client` fixture truncates only between HTTP-route tests. `test_hold_cleared_upsert.py` uses an autouse `_truncate_tx` for this reason; cross-test contamination via the shared `HOLD-FIXTURE-ID-1` source_tx_id was avoided up front rather than caught after-the-fact.

## Next Plan Readiness

- **02-03 (scheduler-backfill):** unblocked. Will consume the new tuple return from `insert_many` directly (no adapter needed for the SchedulerRunner — it'll call `insert_many` and use both `inserted` and `updated_in_place` for the structlog event). Will populate `CanonicalTransaction.hold/description/mcc` from Mono payloads in `MonobankImporter.fetch_statement`. Logical dependency satisfied: the unit-level `test_e2e_hold_then_cleared` proves the upsert behavior 02-03's runner integration test will rely on.
- **02-04 (status-surface):** unblocked. `AccountOut.mono_type` is now serialized; `GET /api/accounts` returns the field for status rendering. The D-16 reshape of `routes_import.py` (introducing `ImportEnqueuedOut`, etc.) is the only remaining work — this plan deliberately did NOT touch that route, and `tests/test_import_route.py` passes UNCHANGED to prove it.

## Threat Flags

None. The CanonicalTransaction extension and the SET-clause restriction are squarely inside the existing trust boundaries documented in the plan's `<threat_model>` (T-02-04 mitigation is exactly what this plan ships). No new network endpoints, no new auth paths, no new file-access patterns, no schema changes at trust boundaries.

## Self-Check: PASSED

Verified files exist on disk:
- `tests/test_hold_cleared_upsert.py` — FOUND
- `src/finance_bro/importers/base.py` — FOUND (modified, contains `hold: bool = False`)
- `src/finance_bro/db/transaction_repo.py` — FOUND (modified, contains `literal_column` + `set_=` once)
- `src/finance_bro/services/import_service.py` — FOUND (modified, contains `inserted_total`)
- `src/finance_bro/api/schemas.py` — FOUND (modified, contains `hold: bool` and `mono_type: str | None`)
- `tests/test_idempotency.py` — FOUND (modified)
- `tests/test_transactions_route.py` — FOUND (modified, contains `test_hold_field_in_response`)

Verified commits exist in git log:
- `a474f1d` — FOUND (feat: switch insert_many to ON CONFLICT DO UPDATE for hold-aware upsert)
- `7b4db77` — FOUND (feat: surface hold on TransactionOut, mono_type on AccountOut, add D-10 frozen-fields tests)

Verified plan-level invariants:
- Full pytest suite: 57 passed, 0 failed
- `grep -c "set_=" transaction_repo.py` returns 1 (D-10 SET clause restriction)
- SET clause body contains EXACTLY `hold`, `amount_minor`, `raw_payload` — no other EXCLUDED columns
- `literal_column` present (xmax detection wired)
- `TransactionOut.hold: bool` declared
- `AccountOut.mono_type: str | None` declared
- This branch's diff vs. wave-1 baseline (`eb0ef0a..HEAD`): 0 scope-leak hits introduced (`SchedulerRunner` mention in `import_run_repo.py` is pre-existing 02-01 forward-reference, not new)
- CENTRAL correctness test `test_cleared_updates_in_place` is green: 3 mutated columns + 6 manual-edit frozen + 6 structural frozen, single-row invariant intact

---
*Phase: 02-reliable-sync*
*Plan: 02 (hold-aware-upsert)*
*Completed: 2026-05-10*
