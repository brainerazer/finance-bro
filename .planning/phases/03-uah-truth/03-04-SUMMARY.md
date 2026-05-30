---
phase: 03-uah-truth
plan: 04
subsystem: fx-lifecycle (bootstrap + cron wiring)
tags: [fx, nbu, bootstrap, cron, apscheduler, lifespan, dst, tzdata]
requires:
  - "NbuFxImporter.fetch_range + aclose (03-02)"
  - "FxRateRepo.upsert_many + count_in_window (03-02)"
  - "TrackedFxCurrencyRepo list/get/set_bootstrap_done/mark_attempted (03-02)"
  - "FxRatesPort protocol (03-02)"
  - "fx_rates LATERAL read path that consumes the rows this plan produces (03-03)"
provides:
  - "FxBootstrapService.maybe_bootstrap_fx + maybe_bootstrap_fx_all_tracked (idempotent, sequential)"
  - "SchedulerRunner.fx_tick — per-currency daily NBU fetch + bootstrap re-run (D-17)"
  - "main.py lifespan: fx_tick CronTrigger @16:00 Europe/Kyiv + fire-and-forget bootstrap + NBU client aclose"
  - "tzdata as an unconditional runtime dependency"
affects:
  - "A fresh install fills fx_rates within ~10-30s of boot and keeps them current daily"
  - "Completes Phase 3 (4/4 plans) — FX-02 satisfied"
tech-stack:
  added:
    - "tzdata>=2025.1 (unconditional runtime dep — slim container ZoneInfo resolution)"
  patterns:
    - "asyncio.create_task fire-and-forget on a backgroundable startup job (non-blocking lifespan)"
    - "CronTrigger(timezone=ZoneInfo('Europe/Kyiv')) for DST-correct local-time firing"
    - "Two HTTP-client owners closed in one lifespan finally (CR-01)"
    - "Per-currency error isolation in a sequential cron loop (one failure logs and continues)"
key-files:
  created:
    - src/finance_bro/services/fx_bootstrap.py
  modified:
    - src/finance_bro/scheduler/runner.py
    - src/finance_bro/main.py
    - pyproject.toml
    - uv.lock
    - tests/test_fx_bootstrap_lazy.py
    - tests/test_fx_tick.py
decisions:
  - "fx_importer added as an optional SchedulerRunner ctor kwarg; importer (Mono) also made optional so the fx_tick-only test path can construct the runner without a Mono importer"
  - "fx_tick reuses FxBootstrapService.maybe_bootstrap_fx for the bootstrap-incomplete branch (idempotent 12-month range) rather than duplicating the backfill logic"
  - "BOOTSTRAP_THRESHOLD=250 (~1 year of NBU business days); FX_TICK_LOOKBACK=3 days (absorbs missed-tick/weekend gaps cheaply for already-bootstrapped currencies)"
  - "nbu_base NOT added to Settings — the hardcoded NBU_BASE constant in nbu.py is the SSRF-safe source of truth (plan allowed implementer's call)"
  - "tzdata added unconditionally (Option b — zero-risk) rather than gating ZoneInfo at boot"
metrics:
  duration: ~25m
  completed: 2026-05-30
  tasks: 3
  files: 1 created, 5 modified
requirements: [FX-02]
---

# Phase 3 Plan 04: FX Cron/Bootstrap Lifecycle Summary

**One-liner:** A fire-and-forget 12-month NBU backfill runs on boot and a daily 16:00 Europe/Kyiv APScheduler cron keeps `fx_rates` current per tracked currency — both self-healing on NBU failure via logs + `tracked_fx_currencies.last_error` only (never `scheduler_state`), with the NBU httpx client closed in the lifespan finally.

## What Was Built

**Task 1 — `services/fx_bootstrap.py` (`3f8d52c`)**
- `FxBootstrapService(session_factory, importer: FxRatesPort)`. `_log = structlog.get_logger()`.
- `maybe_bootstrap_fx(currency)` (D-03): session block reads `count_in_window(currency, today-365d)`; `count >= BOOTSTRAP_THRESHOLD (250)` → `fx.bootstrap.skip`, return. Otherwise `fetch_range(currency, today-366d, today)` is called OUTSIDE any session, wrapped in try/except. On exception → `mark_attempted(currency, str(exc))` + log + return; on empty rows → `mark_attempted(currency, "no rates published")` (D-16) + return; on success → session block `upsert_many(rows)` + `set_bootstrap_done(currency)` + `mark_attempted(currency, None)` + `fx.bootstrap.done`.
- `maybe_bootstrap_fx_all_tracked()` (D-07/D-17): reads `list_currencies()`, iterates SEQUENTIALLY (no `asyncio.gather`), each currency wrapped so one exception is logged (`fx.bootstrap.currency_failed`) and the loop continues. Never touches `scheduler_state` (D-08).
- Flipped `test_fx_bootstrap_lazy` xfail → live PASS.

**Task 2 — `SchedulerRunner.fx_tick` (`8a91e0f`)**
- `__init__` gains `fx_importer: FxRatesPort | None = None`; `importer` made optional (`MonobankImporter | None = None`) so the fx_tick-only test path constructs `SchedulerRunner(session_factory=..., fx_importer=importer)`.
- `fx_tick()` (D-17): builds an `FxBootstrapService` from the injected fx_importer, reads `list_currencies()` (ORDER BY currency), iterates sequentially, each currency error-isolated (`fx.tick.currency.failed` logs and continues). Per currency (`_fx_tick_currency`): `bootstrap_done=false` → `maybe_bootstrap_fx(currency)` (idempotent 12-month re-fetch); `bootstrap_done=true` → `fetch_range(currency, today-3d, today)` OUTSIDE session, empty → `mark_attempted("no rates published")` (D-16), else `upsert_many` + `mark_attempted(None)` + `fx.tick.currency.done`.
- Adds ZERO new `_set_state_auth_failed` / `scheduler_state` references (D-08 — count unchanged at 3). HTTP fetch outside every open session. Mono `tick` untouched.
- Flipped `test_fx_tick` (×3) xfail → live PASS.

**Task 3 — lifespan wiring + tzdata (`6f2e0b1`)**
- `main.py`: imports `asyncio`, `CronTrigger`, `ZoneInfo`, `NbuFxImporter`, `FxBootstrapService`; module-level `KYIV = ZoneInfo("Europe/Kyiv")`.
- Constructs `nbu_importer = NbuFxImporter()` next to `MonobankImporter`; passes `fx_importer=nbu_importer` to the runner; builds `fx_bootstrap = FxBootstrapService(session_factory, nbu_importer)`; declares `bootstrap_task: asyncio.Task[None] | None = None`.
- Inside the existing `if state == "running" and not disable_scheduler:` guard, after `scheduler.add_job(runner.tick, ...)`: registers `scheduler.add_job(runner.fx_tick, CronTrigger(hour=16, minute=0, timezone=KYIV), id="fx_tick", max_instances=1, coalesce=True, misfire_grace_time=3600)` (D-06); after `scheduler.start()` spawns `bootstrap_task = asyncio.create_task(fx_bootstrap.maybe_bootstrap_fx_all_tracked())` (D-07).
- `finally`: cancels `bootstrap_task` if present, then `await runner.aclose()` AND `await nbu_importer.aclose()` (CR-01 — both client owners closed).
- `pyproject.toml` + `uv.lock`: `tzdata>=2025.1` added unconditionally so `ZoneInfo("Europe/Kyiv")` resolves in the slim container (Pitfall 5 / A4).

## Verification Results

- `uv run pytest` (full suite) — **112 passed, 0 failed, 0 xfailed, 0 xpassed** (`-rxX` clean). The suite went from 109 passed + 3 xfailed (Plan-04 scaffolds) to 112 passed; `test_fx_bootstrap_lazy`, `test_fx_tick` (×3) now PASS.
- `tests/test_fx_bootstrap_lazy.py tests/test_fx_stale_fallback.py` — 2 passed.
- `tests/test_fx_tick.py tests/test_fx_importer_nbu.py tests/test_scheduler_round_robin.py tests/test_scheduler_recovery.py` — 9 passed.
- `tests/test_health.py tests/test_no_auth.py tests/test_fx_cron_dst.py tests/test_transactions_route.py` — 11 passed (app boots cleanly; no unclosed-client warning escalated under `filterwarnings=["error"]`; cron fires 16:00 Kyiv across the Oct-25 DST boundary).
- Grep gates: `CronTrigger` in main.py = 2; `fx_tick` in main.py = 1; `aclose` in main.py = 2; `scheduler_state` in fx_bootstrap.py = 0; `asyncio.gather` in fx_bootstrap.py = 0; `fx_tick` in runner.py = 6; `_set_state_auth_failed` in runner.py = 3 (unchanged, zero new refs from fx_tick).
- `python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Kyiv')"` succeeds; `tzdata` present in `uv.lock`.
- `ruff check` + `ruff format --check` pass on `fx_bootstrap.py`, `runner.py`, `main.py`, `settings.py`. `basedpyright` clean on all three source files.

## Deviations from Plan

### 1. [Rule 3 — Blocking] `SchedulerRunner.importer` made optional alongside the new `fx_importer` kwarg

- **Found during:** Task 2.
- **Issue:** `test_fx_tick.py` constructs `SchedulerRunner(session_factory=..., fx_importer=importer)` with NO Mono `importer`. The existing ctor required `importer` positionally, so the test would have raised `TypeError`.
- **Fix:** Added `fx_importer: FxRatesPort | None = None` and changed `importer` to `MonobankImporter | None = None`. The Mono tick paths still receive `importer=` from `main.py` and the existing scheduler tests; no Mono behavior changed (`test_scheduler_round_robin`/`test_scheduler_recovery` still green).
- **Files modified:** `src/finance_bro/scheduler/runner.py`.
- **Commit:** `8a91e0f`.

### 2. [Note — not a deviation] `nbu_base` Settings field omitted

- The plan listed adding `nbu_base` to `Settings` as optional ("implementer's call; a constant in nbu.py is equally fine"). The hardcoded `NBU_BASE` in `nbu.py` is the SSRF-safe constant (no user input in the URL — T-03-16), so no Settings change was made.

### 3. [Note — not a deviation] `test_scheduler_state_repo.py` does not exist

- Task 2's `<verify>` block named `tests/test_scheduler_state_repo.py`, which is not present in the repo. The Mono-untouched guarantee is instead covered by `test_scheduler_round_robin.py` and `test_scheduler_recovery.py` (both green) plus the full-suite run.

## Known Stubs

None. Every artifact this plan owns is fully wired and exercised by a live (non-xfail) test against a real Postgres testcontainer. The bootstrap and cron paths are wired into the running lifespan; the only test-mode difference is the `APP_DISABLE_SCHEDULER` guard that (correctly) keeps the cron and the create_task bootstrap silent during the test session.

## Threat Surface

No new threat surface beyond the plan's `<threat_model>`. T-03-12 (bootstrap blocking startup → fire-and-forget `create_task`, D-07), T-03-13 (NBU failure crashing app → per-currency try/except + logs/last_error, D-08/D-16), T-03-14 (cron wrong-time → `CronTrigger(timezone=ZoneInfo("Europe/Kyiv"))` + unconditional tzdata + `test_fx_cron_dst.py`), T-03-15 (unclosed NBU client → `aclose()` in finally, asserted under `filterwarnings=["error"]`), T-03-16 (NBU base URL → hardcoded constant, no user input) all mitigated as specified. T-03-SC (package installs) accepted: the only new package is the optional unconditional `tzdata`, which was already win32-marked in `uv.lock` and is a first-party PyPI tz database — zero new external install surface.

## For the Next Planner

- Phase 3 is complete (4/4 plans). FX-02 (daily NBU fetch + 12-month backfill, self-healing) is satisfied end-to-end: importer + repos (03-02), rollup read path (03-03), and the cron/bootstrap lifecycle (this plan).
- The dashboard "total spent" honest-UAH number (a phase success criterion) can now sum `uah_amount_minor` across rows fed by a populated `fx_rates`; `fx_stale`/null rows must still be surfaced, never summed as zero.

## Self-Check: PASSED

- Created file exists: `src/finance_bro/services/fx_bootstrap.py` — on disk and committed.
- Commits present in `git log`: `3f8d52c` (Task 1), `8a91e0f` (Task 2), `6f2e0b1` (Task 3).
- Full suite: **112 passed, 0 failed, 0 xfailed, 0 xpassed.** The 3 plan-owned scaffolds (`test_fx_bootstrap_lazy`, `test_fx_tick` ×3) now PASS.
- HEAD on `main`; no untracked/uncommitted code files left behind (`.planning` PATTERNS/RESEARCH docs staged pre-session belong to the final docs commit).
