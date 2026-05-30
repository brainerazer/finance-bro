---
phase: 03-uah-truth
plan: 03
subsystem: fx-rollup (read path + API surface)
tags: [fx, rollup, lateral, decimal, attributed_day, read-path, fx-on-card]
requires:
  - "fx_rates table + carry-forward index (03-01)"
  - "transactions.attributed_day NOT NULL (03-01)"
  - "currency_map.numeric_to_alpha long tail (03-02)"
  - "CanonicalTransaction.attributed_day field + insert_many safety-net (Wave-1 fix)"
provides:
  - "services/fx_rollup.rollup() — pure Decimal UAH math + fx_source/fx_stale (D-11..D-14)"
  - "TransactionRepo.list_for_account — LEFT JOIN LATERAL carry-forward read returning enriched dict rows"
  - "MonobankImporter populates attributed_day = time->Europe/Kyiv date (D-09)"
  - "TransactionOut + 5 computed-on-read FX fields (D-10)"
affects:
  - "GET /api/transactions now returns UAH equivalents + FX metadata per row"
  - "Plan 03-04 (cron/bootstrap) feeds the fx_rates this read path consumes"
tech-stack:
  added: []
  patterns:
    - "LEFT JOIN LATERAL carry-forward rate lookup via parameterized text() + .mappings().all()"
    - "Pure-function Decimal rollup in services/, composed per-row by the repo (no rollup in the route)"
    - "Decimal-as-string fx_rate transport — the one deliberate exception to int-minor-units"
    - "Hermetic autouse TRUNCATE fixture for direct-session FX tests (fx_rates not in conftest truncate list)"
key-files:
  created:
    - src/finance_bro/services/fx_rollup.py
    - .gitignore
  modified:
    - src/finance_bro/db/transaction_repo.py
    - src/finance_bro/importers/monobank.py
    - src/finance_bro/api/schemas.py
    - src/finance_bro/api/routes_transactions.py
    - tests/test_fx_rollup_math.py
    - tests/test_fx_rollup_join.py
    - tests/test_fx_stale_fallback.py
    - tests/test_fx_on_card.py
decisions:
  - "rollup() accepts account_currency (test signature contract) but the conversion always uses amount_minor x account-currency rate — informational, never a second leg (FX-04)"
  - "Per-row rollup composition lives in TransactionRepo.list_for_account, not the route — the locked FX read tests exercise the repo directly and the route stays a thin model_validate"
  - "getcontext().prec = 28 set in fx_rollup module (no project-wide setting exists)"
  - "stale = False when either date is None (pure-function callers in the math test pass attributed_day=None)"
metrics:
  duration: ~30m
  completed: 2026-05-30
  tasks: 3
  files: 2 created, 8 modified
requirements: [FX-03, FX-04]
---

# Phase 3 Plan 03: UAH Rollup Read Path Summary

**One-liner:** Every foreign-currency transaction now shows an honest UAH equivalent computed on read via a `LEFT JOIN LATERAL` carry-forward against NBU rates, with banker's-rounded Decimal math, a `mono_card`/`nbu`/`native_uah` audit label, and a stale flag — the importer freezes a Kyiv `attributed_day` on first write so the Sunday→Friday fallback works.

## What Was Built

**Task 1 — `services/fx_rollup.py` (`49f96d2`)**
- Pure `rollup(amount_minor, currency, account_currency=None, fx_rate=None, fx_rate_date=None, attributed_day=None, op_currency_alpha=None) -> FxFields` (frozen dataclass).
- D-11 native_uah: UAH rows roll up 1:1, `fx_rate == "1.00000000"`, never stale. D-11 label: `mono_card` when `op_currency_alpha != currency`, else `nbu` (audit-only). D-12 no-rate: all FX value fields None + `fx_stale=True`. D-14 math: `major = (Decimal(amount_minor)/100) * fx_rate`; `uah_minor = int(major.quantize(Decimal("0.01"), ROUND_HALF_EVEN) * 100)`. D-13 stale: `fx_rate_date < attributed_day` (False when either is None — the math-test callers pass `attributed_day=None`).
- `fx_rate` serialized `f"{fx_rate:.8f}"` (Decimal-as-string, never float). `getcontext().prec = 28` set in-module. mono_card and nbu math are byte-identical (no triangulation — FX-04 / Pitfall 2/8).
- Flipped `test_fx_rollup_math` (both properties) from xfail → live PASS.

**Task 2 — LATERAL read + importer attributed_day (`1c20e84`)**
- `TransactionRepo.list_for_account` rewritten to the locked `ROLLUP_SQL` (module-level `text()`): `LEFT JOIN LATERAL (SELECT rate, rate_date FROM fx_rates WHERE currency = t.currency AND rate_date <= t.attributed_day ORDER BY rate_date DESC LIMIT 1) fx ON true`, executed via `.mappings().all()`. Each row is enriched by `fx_rollup.rollup(...)` and returned as a plain dict carrying the transaction columns + 5 FX fields. LEFT (not INNER) keeps no-rate rows present (D-12).
- `_op_currency_alpha(raw_payload)` helper: missing/unmapped `currencyCode` → `None` (→ fx_source `nbu`), never raises on read (D-11 defensive fallback).
- `MonobankImporter.fetch_statement` now sets `attributed_day = occurred_at.astimezone(KYIV).date()` (D-09), derived from the same `item["time"]` as `occurred_at`. `insert_many` `set_={...}` unchanged (hold/amount_minor/raw_payload only) — attributed_day frozen by omission.
- Flipped `test_fx_rollup_join` (Sunday→Friday carry-forward + stale) and `test_fx_stale_fallback` (no-rate LEFT-JOIN survival) xfail → live PASS.

**Task 3 — TransactionOut FX fields + route wiring (`1bb9a86`)**
- `schemas.TransactionOut` gains `uah_amount_minor: int | None`, `fx_rate: str | None`, `fx_rate_date: date | None`, `fx_source: Literal[...]`, `fx_stale: bool` (D-10). Module docstring documents `fx_rate` as the deliberate Decimal-as-string exception.
- `routes_transactions` keeps the no-card guard and `model_validate`s the enriched mapping rows into `TransactionOut` (rollup computed in the repo, never denormalized — FX-03).
- Flipped `test_fx_on_card` xfail → live PASS (mono_card label + EUR account-amount × NBU EUR rate).

## Key Decisions

- **rollup() carries `account_currency` for the test contract but never converts by it.** The locked `test_fx_rollup_math` signature passes `account_currency=`; the parameter is accepted and documented as informational. The conversion is always `amount_minor × account-currency rate` — `amount_minor` is already in the account currency, so there is exactly one leg (FX-04).
- **Per-row rollup lives in the repo, not the route.** The four locked FX read tests call `TransactionRepo.list_for_account(...)` directly and assert `row["uah_amount_minor"]`, `row["fx_source"]`, etc. Composing the rollup in the repo satisfies those contracts and keeps the route a thin `model_validate`. The plan's interface text allowed "construct directly or via model_validate — implementer's call"; this is the implementer's call that the tests dictate.
- **`prec = 28` set in fx_rollup.** No project-wide `getcontext()` setting exists (grep of `src/` returned nothing), so the module sets it on import per CLAUDE.md §Money.

## Deviations from Plan

### 1. [Rule 1 — Bug] Cross-test fx_rates contamination broke `test_fx_stale_fallback`

- **Found during:** Task 2 verification (full task-test run).
- **Issue:** `test_fx_stale_fallback` asserts no USD rate exists, but `fx_rates` is keyed `(rate_date, currency)` and is NOT in the conftest `client` fixture truncate list. The three direct-session FX tests use unique `source_account_id` values (so accounts/transactions don't collide) but share the `fx_rates` table — `test_fx_rollup_join` seeded `('2026-05-08','USD',43.8033)`, which then satisfied the stale test's USD LATERAL lookup (got `uah_amount_minor=-109508` instead of `None`). Passed in isolation, failed in suite.
- **Fix:** Added an `autouse` `pytest_asyncio` fixture to each of the three direct-session FX tests that `TRUNCATE TABLE fx_rates, transactions, accounts RESTART IDENTITY CASCADE` before the test body — hermetic isolation matching the per-test pattern already used in `test_hold_cleared_upsert.py`. No test body assertions changed.
- **Files modified:** `tests/test_fx_stale_fallback.py`, `tests/test_fx_rollup_join.py`, `tests/test_fx_on_card.py`.
- **Commit:** `1c20e84` (join/stale), `1bb9a86` (on_card).

### 2. [Rule 1 — Bug] Accidentally tracked `__pycache__/*.pyc`; added `.gitignore`

- **Found during:** Task 1 commit.
- **Issue:** `git add src/finance_bro/services/fx_rollup.py` pulled two `.pyc` files into the commit because the repo had no `.gitignore` for `__pycache__` (the only 2 tracked `.pyc` files in the whole repo were these accidental ones).
- **Fix:** `git rm --cached` the two `.pyc` files, added `.gitignore` (`__pycache__/` + `*.py[cod]`), amended the Task 1 commit (not yet pushed). Repo now tracks zero `.pyc` files.
- **Files modified:** `.gitignore` (new).
- **Commit:** `49f96d2` (amended Task 1).

### 3. [Note — not a deviation] base.py unchanged

- `CanonicalTransaction.attributed_day: date | None = None` was already present from the Wave-1 fix (and the insert_many safety-net derives it from `occurred_at` when the importer omits it). Per the orchestrator note, the importer setting it explicitly (Task 2) is the preferred path; the safety-net stays as defense-in-depth. No edit to base.py was needed.

## For the Next Planner

- Plan 03-04 (cron/bootstrap lifecycle) feeds the `fx_rates` rows this read path consumes. The 4 remaining xfail scaffolds are all Plan-04-owned: `test_fx_bootstrap_lazy`, `test_fx_tick` (×3). They will flip when `FxBootstrapService` and `SchedulerRunner.fx_tick` land.
- `list_for_account` now returns `list[dict[str, Any]]` (mapping rows), NOT `list[Transaction]` ORM objects. Any future caller must consume the dict shape (the route already does via `model_validate`).
- The dashboard "total spent" honest-UAH number (a phase success criterion) can now sum `uah_amount_minor` across rows; `fx_stale`/null `uah_amount_minor` rows must be surfaced, never silently summed as zero.

## Self-Check

- Created files exist: `src/finance_bro/services/fx_rollup.py`, `.gitignore` — both on disk and committed.
- Commits present in `git log`: `49f96d2` (Task 1), `1c20e84` (Task 2), `1bb9a86` (Task 3).
- Full suite: **106 passed, 4 xfailed (Plan-04 scaffolds), 0 failed.** The 4 plan-owned scaffolds (`test_fx_rollup_math`, `test_fx_rollup_join`, `test_fx_stale_fallback`, `test_fx_on_card`) now PASS.
- Grep gates: `uah_amount_minor` in models.py = 0 (FX-03); `float(` in fx_rollup = 0; `ROUND_HALF_EVEN` = 2; `LEFT JOIN LATERAL` = 1; `Europe/Kyiv` in monobank.py = 1; `fx_source` in schemas.py = 2; `set_={...}` = exactly hold/amount_minor/raw_payload.
- `ruff check` + `ruff format --check` pass on all touched source + test files; `basedpyright` clean on fx_rollup.py.

## Self-Check: PASSED
