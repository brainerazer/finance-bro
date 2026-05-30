---
phase: 03-uah-truth
plan: 02
subsystem: fx-ingestion (importer + repos)
tags: [fx, nbu, importer, httpx, repo, upsert, bootstrap, decimal]
requires:
  - "fx_rates table + FxRate ORM model (03-01)"
  - "tracked_fx_currencies table + TrackedFxCurrency ORM model (03-01)"
  - "importers/base.py canonical-dataclass + Protocol idiom"
provides:
  - "FxRatesPort protocol + FxRateRow frozen dataclass (importers/base.py)"
  - "NbuFxImporter.fetch_range — single-call NBU range fetch, Decimal rates, empty->[]"
  - "currency_map long tail: PLN/GBP/CHF (raise-on-unknown preserved)"
  - "FxRateRepo: idempotent upsert_many + count_in_window"
  - "TrackedFxCurrencyRepo: list/get/upsert/set_bootstrap_done/mark_attempted"
affects:
  - "Plan 03-03 (rollup join reads fx_rates; op-currency lookup may use currency_map)"
  - "Plan 03-04 (cron/bootstrap lifecycle wires NbuFxImporter + both repos)"
tech-stack:
  added: []
  patterns:
    - "httpx.AsyncClient with hardcoded base_url + no auth header (NBU has no token)"
    - "json.loads(resp.text, parse_float=Decimal) — never resp.json() on a rate path (Pitfall 1)"
    - "tenacity @retry capped 3 attempts, exp backoff max 30s (DoS-safe, D-07)"
    - "ON CONFLICT DO NOTHING upsert + text() count + text() targeted UPDATE"
key-files:
  created:
    - src/finance_bro/importers/nbu.py
    - src/finance_bro/db/fx_rate_repo.py
    - src/finance_bro/db/tracked_fx_currency_repo.py
    - tests/test_fx_repos.py
  modified:
    - src/finance_bro/importers/base.py
    - src/finance_bro/importers/currency_map.py
    - tests/test_fx_importer_nbu.py
decisions:
  - "Wave-0 xfail marks on the NbuFxImporter scaffolds (test_fx_importer_nbu) removed once the importer landed, so they assert as live PASS regression guards (per 03-01 SUMMARY intent)"
  - "The repo scaffolds named in the plan's Task 2 verify block (test_fx_bootstrap_lazy / test_fx_stale_fallback) actually depend on the Plan 04 FxBootstrapService and the Plan 03 LATERAL rollup respectively — NOT on this plan's repos. They were left xfail and a new live test_fx_repos.py was added to provide real repo coverage (idempotent upsert, count, ordered iterate, bootstrap flag, last_error set/clear)."
  - "NbuFxImporter gets source_kind='nbu' for symmetry with MonobankImporter, though FxRatesPort does not require it"
metrics:
  duration: ~25m
  completed: 2026-05-30
  tasks: 2
  files: 4 created, 3 modified
requirements: [FX-02]
---

# Phase 3 Plan 02: FX Ingestion Slice (NBU Importer + Repos) Summary

Delivered the FX-02 outbound-HTTP + persistence half: an `NbuFxImporter` behind the new `FxRatesPort` protocol that fetches one currency's NBU rates over a date range in a single `exchange_site` call (parsing every rate as `Decimal`, never a float), plus `FxRateRepo` (idempotent ON-CONFLICT-DO-NOTHING upsert + freshness count) and `TrackedFxCurrencyRepo` (ordered iteration, first-seen upsert, bootstrap flag, last-error recording that never touches `scheduler_state`). The NbuFxImporter scaffolds flipped from xfail to live PASS, and a new `test_fx_repos.py` gives the two repos live regression coverage.

## What Was Built

**Task 1 — FxRatesPort/FxRateRow + NbuFxImporter + currency_map long tail (`ab6b74b`)**
- `importers/base.py`: added `FxRateRow` (`@dataclass(frozen=True)`: `rate_date: date`, `currency: str`, `rate: Decimal`) and `class FxRatesPort(Protocol)` with the single async `fetch_range(currency, start, end) -> list[FxRateRow]` (D-02). Added `from decimal import Decimal`.
- `importers/nbu.py`: `NbuFxImporter` mirroring `MonobankImporter` but with NO `X-Token` header and NO `RateLimitGate` (NBU has no token). Constant `NBU_BASE = "https://bank.gov.ua/NBU_Exchange/exchange_site"` (hardcoded — SSRF mitigation T-03-06). `fetch_range` decorated with tenacity `@retry(stop_after_attempt(3), wait_exponential(multiplier=2, max=30), retry_if_exception_type((TransportError, HTTPStatusError)), reraise=True)` (T-03-05). GET with `start`/`end` (`%Y%m%d`), `valcode`, `json` params; `raise_for_status()`; `raw = json.loads(resp.text, parse_float=Decimal)` (NEVER `resp.json()` — Pitfall 1 / T-03-04); rows parse `exchangedate` via `%d.%m.%Y`, use `cc` for currency, and `rate_per_unit` defensively when `units != 1` (A3). `raw == []` → `[]` (D-16). `aclose()` closes the client.
- `importers/currency_map.py`: extended `_NUM_TO_ALPHA` with `985:"PLN"`, `826:"GBP"`, `756:"CHF"` (D-15); raise-on-unknown behavior of `numeric_to_alpha` left intact (Mono insert path depends on it).
- `tests/test_fx_importer_nbu.py`: removed the two Wave-0 `xfail` marks (and refreshed docstring) so the importer tests assert as live PASS.

**Task 2 — FxRateRepo + TrackedFxCurrencyRepo + live repo tests (`9815a18`)**
- `db/fx_rate_repo.py`: `upsert_many(rows)` builds value dicts and runs `insert(FxRate).values(...).on_conflict_do_nothing(index_elements=["rate_date","currency"])` (D-03), returning `rowcount`; `count_in_window(currency, since_date)` uses `text()` count with `int(row[0]) if row else 0`.
- `db/tracked_fx_currency_repo.py`: `list_currencies` (ordered `select().order_by(currency)` → scalars, D-17), `get`, `upsert_currency` (ON CONFLICT DO NOTHING first-seen, D-15), `set_bootstrap_done` (text UPDATE), `mark_attempted(currency, last_error)` (text UPDATE of `last_attempted_at=now()` + `last_error` only — never `scheduler_state`, D-08).
- `tests/test_fx_repos.py` (NEW): live coverage against a real Postgres container — idempotent double-upsert produces no duplicates (`count == 2`), `count_in_window` window-bounded, empty upsert is a no-op, `list_currencies` ascending (D-17), `set_bootstrap_done` flips true, `mark_attempted` sets then clears `last_error`.

## Verification Results

Run against a real `postgres:17-bookworm` testcontainer where DB-backed.
- `uv run pytest tests/test_fx_importer_nbu.py tests/test_importer_currency_map.py` — **4 passed** (NBU range parses dates + Decimal rates; empty body → `[]`; `aclose()` leaves no unclosed-client warning under `filterwarnings=["error"]`; PLN/GBP/CHF mappings).
- `uv run pytest tests/test_fx_repos.py` — **3 passed** (idempotent re-upsert no-dupe/no-error; `count_in_window` bounded; `list_currencies` ascending; `set_bootstrap_done`→true; `mark_attempted` records then clears `last_error`).
- `uv run pytest tests/` (full suite) — **102 passed, 9 xfailed, 0 failed**. The 9 remaining xfails are all Plan 03/04 scaffolds (`test_fx_bootstrap_lazy`, `test_fx_on_card`, `test_fx_rollup_join`, `test_fx_rollup_math` ×2, `test_fx_stale_fallback`, `test_fx_tick` ×3) — none owned by this plan; **0 XPASS leaks** (confirmed via `-rxX`).
- Grep gates: `parse_float=Decimal` present; `resp.json()` = 0; `X-Token` = 0; `RateLimitGate` = 0 (nbu.py); `on_conflict_do_nothing` = 1 (fx_rate_repo.py); `scheduler_state` = 0 (tracked_fx_currency_repo.py).
- Imports: `from finance_bro.importers.base import FxRatesPort, FxRateRow`; `from finance_bro.db.fx_rate_repo import FxRateRepo`; `from finance_bro.db.tracked_fx_currency_repo import TrackedFxCurrencyRepo` — all clean.
- `uv run ruff check` + `ruff format --check` — PASS across all 7 touched source/test files.

## Deviations from Plan

### 1. [Rule 3 — Blocking] The two repo scaffolds named in Task 2's verify block depend on later-plan symbols; left them xfail and added a real repo test instead

- **Found during:** Task 2 verification.
- **Issue:** The plan's Task 2 `<verify>` runs `tests/test_fx_bootstrap_lazy.py` and `tests/test_fx_stale_fallback.py` and the acceptance criteria imply they should pass with these repos. They do **not**: `test_fx_bootstrap_lazy` imports `FxBootstrapService` from `finance_bro.services.fx_bootstrap` (built in **Plan 04**), and `test_fx_stale_fallback` imports `TransactionRepo.list_for_account` returning rollup mappings (the LATERAL join built in **Plan 03**). Un-xfailing either would turn the suite red (ImportError / missing method), since neither symbol exists yet. (The planner appears to have mis-assigned these scaffolds to Task 2's verify block.)
- **Fix:** Left both scaffolds xfail (they will flip in their owning plans). Added `tests/test_fx_repos.py` as live PASS coverage that exercises **only** the two repos this plan owns — idempotent upsert, `count_in_window`, ordered `list_currencies`, `set_bootstrap_done`, and `mark_attempted` set/clear. This satisfies the real acceptance intent ("FxRateRepo upserts idempotently"; "list_currencies ascending") without breaking the suite.
- **Files modified:** `tests/test_fx_repos.py` (new).
- **Commit:** `9815a18`.

### 2. [Rule 3 — Blocking] Removed stale Wave-0 `xfail` marks on the NbuFxImporter scaffolds

- **Found during:** Task 1 GREEN verification.
- **Issue:** `test_fx_importer_nbu.py`'s two tests carried `@pytest.mark.xfail(strict=False)`. Once `NbuFxImporter` was implemented they began to XPASS; with `strict=False` an XPASS is silent and a stale xfail mark swallows future regressions, failing the plan's success criterion "the NbuFxImporter/FxRateRow scaffolds now pass."
- **Fix:** Removed the `xfail` decorators (and refreshed the docstring) so they assert as live PASS — the explicit Wave-0 intent recorded in 03-01-SUMMARY.
- **Files modified:** `tests/test_fx_importer_nbu.py`.
- **Commit:** `ab6b74b`.

### 3. [Style] `ruff format` applied to new files

- `ruff check` passed but `ruff format` reformatted the new `nbu.py`, `base.py` edit, and the new test files (line-length wrapping of the long `fetch_range` signature and decorators). No logic change.

## Known Stubs

None. Every artifact this plan owns is fully wired and exercised by a live (non-xfail) test against a real Postgres testcontainer. The remaining xfail scaffolds belong to Plans 03 (`rollup_join`, `fx_on_card`) and 04 (`bootstrap_lazy`, `stale_fallback` LEFT-JOIN contract) and are intentionally out of this plan's scope.

## Threat Surface

No new threat surface beyond the plan's `<threat_model>`. T-03-04 (Decimal parse — `parse_float=Decimal`, grep-asserted no `resp.json()`), T-03-05 (retry capped at 3 / 30s), T-03-06 (hardcoded `NBU_BASE`, no user input in URL) all mitigated as specified. T-03-07 (non-PII NBU payload) and T-03-SC (zero new packages) accepted as planned — no dependency added.

## Self-Check: PASSED

- All 4 created + 3 modified files exist on disk and are committed.
- Commits `ab6b74b` (Task 1) and `9815a18` (Task 2) present in `git log`.
- HEAD on `main`; no untracked/uncommitted code files left behind.
