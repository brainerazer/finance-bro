---
phase: 3
slug: uah-truth
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-30
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Source: 03-RESEARCH.md § Validation Architecture (live-verified harness).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 + pytest-asyncio 1.3.0 (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths=["tests"]`, `pythonpath=["src"]`, `filterwarnings=["error"]`) |
| **Quick run command** | `uv run pytest tests/test_fx_rollup_join.py -x` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~60 seconds (testcontainers Postgres spin-up dominates) |
| **Real-DB harness** | `tests/conftest.py` — session-scoped `PostgresContainer("postgres:17-bookworm")`, alembic-to-head, truncate-between-tests |
| **HTTP mock** | `respx` (mock NBU); existing Mono tests use the same pattern |

> **Note on `filterwarnings=["error"]`:** any unclosed `httpx.AsyncClient` in `NbuFxImporter`
> escalates to a hard test failure (same trap as `main.py` CR-01). The NBU client MUST be
> closed in lifespan teardown or via an `aclose()` the bootstrap awaits.

---

## Sampling Rate

- **After every task commit:** Run the single test file for the task under change (`-x`).
- **After every plan wave:** Run `uv run pytest tests/test_fx_*.py tests/test_attributed_day_migration.py`
- **Before `/gsd:verify-work`:** Full `uv run pytest` green + `ruff check` + `basedpyright` (strict on `src/`)
- **Max feedback latency:** ~60 seconds (full suite)

---

## Per-Task Verification Map

| Req | Behavior | Test Type | Automated Command | File Exists |
|-----|----------|-----------|-------------------|-------------|
| FX-02 | 12-month backfill populates `fx_rates` NUMERIC(18,8) keyed (rate_date,currency) | integration | `uv run pytest tests/test_fx_importer_nbu.py -x` | ❌ W0 |
| FX-02 | Daily 16:00 Kyiv cron fires correctly across DST | unit | `uv run pytest tests/test_fx_cron_dst.py -x` | ❌ W0 |
| FX-02 | Empty NBU result → bootstrap_done stays false, last_error set, row stays fx_stale | integration | `uv run pytest tests/test_fx_stale_fallback.py -x` | ❌ W0 |
| FX-02/03 | Sunday tx uses Friday's rate; fx_rate_date=Friday; fx_stale=true | integration | `uv run pytest tests/test_fx_rollup_join.py -x` | ❌ W0 |
| FX-03 | UAH computed on read via LATERAL join; no denormalized column exists | integration + schema | `uv run pytest tests/test_fx_rollup_join.py tests/test_schema_invariants.py -x` | partial |
| FX-03 | Banker's-rounding kopeck math is exact (property test) | unit | `uv run pytest tests/test_fx_rollup_math.py -x` | ❌ W0 |
| FX-04 | mono_card: UAH = account-amount × NBU rate, NOT double-converted (property test) | integration | `uv run pytest tests/test_fx_on_card.py -x` | ❌ W0 |
| FX-02 | attributed_day backfill + NOT NULL migration is Kyiv-correct | integration | `uv run pytest tests/test_attributed_day_migration.py -x` | ❌ W0 |
| FX-02 | Lazy auto-add: new currency → tracked row + bootstrap; subsequent read non-null | integration | `uv run pytest tests/test_fx_bootstrap_lazy.py -x` | ❌ W0 |
| FX-02 | fx_tick per-currency loop: ORDER BY currency, re-bootstrap incomplete currency, empty→last_error, error isolation, no scheduler_state write (D-08/D-16/D-17) | unit/integration | `uv run pytest tests/test_fx_tick.py -x` | ❌ W0 |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky · W0 = created in Wave 0*

---

## Wave 0 Requirements

- [ ] `tests/test_fx_importer_nbu.py` — FX-02 (range fetch, parse, weekend carry-forward shape, empty→[])
- [ ] `tests/test_fx_rollup_join.py` — FX-03 (LATERAL fallback; Sunday→Friday on sparse fx_rates)
- [ ] `tests/test_fx_on_card.py` — FX-04 (no double-conversion; mono_card label + identical math)
- [ ] `tests/test_fx_rollup_math.py` — property test for Decimal/banker's-rounding (Pitfall 1)
- [ ] `tests/test_fx_stale_fallback.py` — D-12/D-13 (no-rate → null + fx_stale, row still appears)
- [ ] `tests/test_fx_bootstrap_lazy.py` — D-15 (lazy auto-add)
- [ ] `tests/test_fx_cron_dst.py` — D-06 (CronTrigger fire time across DST)
- [ ] `tests/test_fx_tick.py` — FX-02/D-17 (per-currency loop: ORDER BY, re-bootstrap incomplete, empty→last_error, error isolation, no scheduler_state write per D-08)
- [ ] `tests/test_attributed_day_migration.py` — D-09 (backfill + NOT NULL)
- [ ] NBU fixtures: `tests/fixtures/nbu_usd_range.json`, `nbu_empty.json` (mirror real `exchange_site` shape, incl. `calcdate`)
- [ ] No new framework install — pytest/respx/freezegun/testcontainers all present.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| First-boot bootstrap fills 12mo of real NBU rates within ~10–30s | FX-02 | Hits live `bank.gov.ua` | Fresh `docker compose up`; after boot, `psql -c "select count(*) from fx_rates where currency='USD'"` ≥ ~250 |

*Most phase behaviors have automated verification; live-NBU bootstrap is the one manual smoke check.*

---

## Validation Sign-Off

- [ ] All tasks have automated verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
