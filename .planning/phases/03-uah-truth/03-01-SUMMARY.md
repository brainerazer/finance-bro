---
phase: 03-uah-truth
plan: 01
subsystem: db-schema + test-scaffold
tags: [fx, migration, schema, alembic, attributed_day, test-scaffold]
requires: []
provides:
  - "fx_rates table (PK rate_date,currency; NUMERIC(18,8) rate; covering index)"
  - "tracked_fx_currencies table (USD/EUR seeded, bootstrap_done=false)"
  - "transactions.attributed_day NOT NULL (Kyiv-backfilled)"
  - "FxRate + TrackedFxCurrency ORM models"
  - "2 NBU fixtures + 9 Wave 0 FX test scaffolds"
affects:
  - "alembic migration chain head -> 0003"
  - "every downstream Phase 3 FX plan (lands against existing tests)"
tech-stack:
  added: []
  patterns:
    - "Alembic transactional revision: UPDATE-before-ALTER for NOT NULL on populated table (Pitfall 3)"
    - "RED test scaffolds via in-body imports + xfail(strict=False) for clean collection against unbuilt symbols"
key-files:
  created:
    - alembic/versions/0003_fx_truth.py
    - tests/test_attributed_day_migration.py
    - tests/fixtures/nbu_usd_range.json
    - tests/fixtures/nbu_empty.json
    - tests/test_fx_importer_nbu.py
    - tests/test_fx_rollup_join.py
    - tests/test_fx_on_card.py
    - tests/test_fx_rollup_math.py
    - tests/test_fx_stale_fallback.py
    - tests/test_fx_bootstrap_lazy.py
    - tests/test_fx_cron_dst.py
    - tests/test_fx_tick.py
  modified:
    - src/finance_bro/db/models.py
decisions:
  - "Plain btree on (currency, rate_date) — Postgres scans backward for the DESC LATERAL limit, no DESC ops needed (D-04)"
  - "No fx_rates seed in the migration — lifespan bootstrap fills it (D-04)"
  - "FX scaffolds use in-body imports + xfail(strict=False) so collection is clean while target symbols do not yet exist"
metrics:
  duration: ~1h
  completed: 2026-05-30
  tasks: 3
  files: 12 created, 1 modified
requirements: [FX-02, FX-03]
---

# Phase 3 Plan 01: UAH Truth Schema Spine + Wave 0 Test Scaffolds Summary

Laid the Phase 3 FX schema spine — `fx_rates` (FX-02, keyed `(rate_date, currency)`, `NUMERIC(18,8)`) and `tracked_fx_currencies` (USD/EUR seeded) — backfilled `transactions.attributed_day` Kyiv-correctly and tightened it to NOT NULL, and authored the 9 Wave 0 FX test scaffolds + 2 NBU fixtures so every downstream FX plan lands against existing RED tests. No denormalized UAH column (FX-03 immutable).

## What Was Built

**Task 1 — ORM models (`f16dece`)**
- `FxRate` -> `fx_rates`: composite PK `(rate_date, currency)`, `rate NUMERIC(18,8)`, `fetched_at` default `now()`, covering index `ix_fx_rates_currency_rate_date` on `(currency, rate_date)` (D-04).
- `TrackedFxCurrency` -> `tracked_fx_currencies`: `currency CHAR(3)` PK, `first_seen_at`, `bootstrap_done` default false, `last_attempted_at`, `last_error` (D-05).
- `Transaction.attributed_day` flipped `nullable=True` -> `nullable=False`, annotation `Mapped[date | None]` -> `Mapped[date]` (D-09).
- Added `Numeric`, `PrimaryKeyConstraint`, `Decimal` imports. No `uah_amount_minor` anywhere (FX-03).

**Task 2 — migration 0003 + migration test (`83008cf`)**
- `alembic/versions/0003_fx_truth.py` (`revision=0003`, `down_revision=0002`): create `fx_rates` + index, create `tracked_fx_currencies`, seed USD/EUR, `UPDATE transactions SET attributed_day = (time AT TIME ZONE 'Europe/Kyiv')::date WHERE attributed_day IS NULL`, then `alter_column(..., nullable=False)`. UPDATE textually precedes ALTER in a single transactional revision (Pitfall 3 / T-03-02). Downgrade reverses in order.
- `tests/test_attributed_day_migration.py`: downgrades to 0002, inserts a `2026-01-15 23:30 UTC` row (= `2026-01-16 01:30` Kyiv) with NULL `attributed_day`, upgrades to 0003, asserts the day lands on `2026-01-16` (T-03-01 tz-correctness), the column rejects NULL, and the USD/EUR seed is present.

**Task 3 — NBU fixtures + 9 FX scaffolds (`367e866`)**
- `nbu_usd_range.json` (Friday `08.05.2026` + carried-forward Sunday `10.05.2026` with `calcdate=08.05.2026`) and `nbu_empty.json` (`[]`), mirroring the live `exchange_site` shape.
- 9 RED scaffolds encoding the locked behavior contracts: `test_fx_importer_nbu` (FX-02, Decimal rate, empty->`[]` D-16, mandatory `aclose`), `test_fx_rollup_join` (FX-03 LATERAL stale carry-forward), `test_fx_on_card` (FX-04 account-currency x NBU, Pitfall 2), `test_fx_rollup_math` (banker's rounding + no-double-conversion property), `test_fx_stale_fallback` (D-12 null-FX row still present), `test_fx_bootstrap_lazy` (D-15 CHF lazy track + bootstrap), `test_fx_cron_dst` (D-06 — runs now, **passes**), `test_fx_tick` (D-08/D-16/D-17 — ORDER BY currency, bootstrap re-fetch, empty->last_error, per-currency error isolation, no scheduler_state write), and `test_attributed_day_migration` (authored under Task 2).
- Scaffolds use in-body imports + `@pytest.mark.xfail(strict=False)` so they collect cleanly while target symbols are unbuilt.

## Verification Results

All run against a real `postgres:17-bookworm` testcontainer where DB-backed:
- `uv run pytest tests/test_migrations.py tests/test_attributed_day_migration.py` — **4 passed** (round-trip up/down; post-upgrade `fx_rates` + `tracked_fx_currencies` exist; 23:30-UTC row attributes to `2026-01-16`; NULL rejected; USD/EUR seeded).
- `uv run pytest <8 FX files> --collect-only -q` — **zero collection errors**.
- `uv run pytest <8 FX files>` — **2 passed, 12 xfailed**, 0 errors (cron passes; scaffolds xfail as designed).
- `uv run ruff check src/finance_bro/db/models.py alembic/versions/0003_fx_truth.py <8 FX files>` — PASS.
- `grep -v '^#' src/finance_bro/db/models.py | grep -c uah_amount_minor` — **0** (FX-03 / T-03-03).
- Models import check — `fx_rates`/`tracked_fx_currencies` tables, `rate` is `NUMERIC(18, 8)`, `attributed_day` nullable=False, FxRate PK `(rate_date, currency)`, TrackedFxCurrency PK `currency`, `bootstrap_done` default false.
- Fixture validation — empty parses to `[]`; USD array has required keys; Friday + carried-forward Sunday (calcdate=Friday) present; importer test contains `aclose`.

## Deviations from Plan

None — plan executed as written.

Process notes (not plan deviations):
- My initial edits/writes resolved to the shared-checkout path and were rejected/not persisted by the worktree harness; I re-applied every change against the worktree path before committing. No content changed.
- Four ruff findings (import sorting in `0003_fx_truth.py` and `test_fx_bootstrap_lazy.py`; an unused `noqa: PT011` in the migration test; ambiguous `×` glyphs in `test_fx_on_card.py` docstrings/comments) were auto/manually fixed in commit `4f… (lint follow-up)` so the acceptance-criteria `ruff check` passes cleanly across every plan file.

## Known Stubs

The 9 FX test files are intentional RED scaffolds (xfail) for not-yet-built symbols (`NbuFxImporter`, `fx_rollup.rollup`, `FxBootstrapService`, `SchedulerRunner.fx_tick`, `TransactionRepo.list_for_account` LATERAL rollup, `FxRateRow`). This is the explicit Nyquist intent of Wave 0 (per 03-VALIDATION.md): tests exist before the code so downstream plans land green. They are NOT accidental stubs — each will flip to PASS as the implementing plan builds its target. `test_fx_cron_dst` is already non-xfail and passing.

## Threat Surface

No new threat surface beyond the plan's `<threat_model>`. T-03-01 (tz backfill — proven by the 23:30-UTC -> 2026-01-16 assertion), T-03-02 (UPDATE-before-ALTER — proven by `test_migrations` upgrade), T-03-03 (no denormalized UAH — proven by the grep gate) all mitigated as specified. T-03-SC (no new packages) holds — zero install surface added.

## Self-Check: PASSED

- All 12 created files + 1 modified file exist on disk and are committed.
- Commits `f16dece`, `83008cf`, `367e866` present in `git log`.
