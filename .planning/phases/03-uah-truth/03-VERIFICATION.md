---
phase: 03-uah-truth
verified: 2026-05-30T18:45:00Z
status: human_needed
score: 4/5 roadmap success criteria verified; 1 WARNING on tzdata gap
overrides_applied: 0
gaps:
deferred:
human_verification:
  - test: "Confirm ZoneInfo('Europe/Kyiv') resolves in the Docker runtime image before production deploy"
    expected: "python -c \"from zoneinfo import ZoneInfo; ZoneInfo('Europe/Kyiv')\" exits 0 inside the built container"
    why_human: "tzdata is NOT in pyproject.toml (SUMMARY.md claimed it was added; the commit stat shows only main.py changed in e4c971b). The dev env works because macOS ships /usr/share/zoneinfo. python:3.13-slim-trixie does NOT ship system zoneinfo data. Either add tzdata to pyproject.toml dependencies or apt-get install tzdata in the Dockerfile RUN layer. Cannot verify without running docker build."
---

# Phase 3: UAH Truth Verification Report

**Phase Goal:** Bohdan looks at any USD or EUR transaction in the feed and sees an honest UAH equivalent computed at the NBU rate of the transaction's day, with weekend/holiday fallback to the most recent prior business-day rate. FX-on-card transactions use Mono's already-converted account-currency amount — never double-converted via NBU.
**Verified:** 2026-05-30T18:45:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| SC1 | App fetches 12 months of NBU rates on first run; persisted as NUMERIC(18,8) keyed by (rate_date, currency) | VERIFIED | `alembic/versions/0003_fx_truth.py` creates `fx_rates` with `Numeric(18,8)`, PK `(rate_date, currency)`. `FxBootstrapService.maybe_bootstrap_fx` does a 12-month range fetch on first boot. Test `test_fx_bootstrap_lazy.py::test_chf_tracked_and_bootstrapped` passes. Note: SC1 says `(rate_date, from_currency, to_currency)` but D-04 in CONTEXT.md explicitly documents `to_currency` is implicit UAH in v1 — this is a locked design decision, not a gap. |
| SC2 | Daily cron at 16:00 Europe/Kyiv; failure does not block import; rollup falls back to prior business-day rate; `fx_stale: true` in response | VERIFIED | `main.py` wires `CronTrigger(hour=16, minute=0, timezone=KYIV)`. Per-currency error isolation in `fx_tick` logs and continues (never blocks import). `fx_stale=True` when `fx_rate_date < attributed_day` (D-13, confirmed in `fx_rollup.py`). Test `test_fx_cron_dst.py` (×2 PASS), `test_fx_tick.py` (×3 PASS). |
| SC3 | GET /api/transactions returns UAH equivalent computed on read via `transactions × fx_rates` join; no `uah_amount_minor` denormalized column | VERIFIED | `ROLLUP_SQL` in `transaction_repo.py` is a verbatim `LEFT JOIN LATERAL (SELECT rate, rate_date FROM fx_rates WHERE currency = t.currency AND rate_date <= t.attributed_day ORDER BY rate_date DESC LIMIT 1) fx ON true`. `grep -v '^#' models.py | grep -c uah_amount_minor` returns 0. `TransactionOut` exposes `uah_amount_minor` but it is computed on read. Tests `test_fx_rollup_join.py` and `test_transactions_route.py` pass. |
| SC4 | FX-on-card transaction uses Mono's `amount` field (account currency) × NBU rate — no re-conversion via operation-currency leg | VERIFIED | `fx_rollup.rollup()` uses `amount_minor × fx_rate` regardless of `fx_source`. The `fx_source = "mono_card"` label is audit-only (comment: "math is identical for mono_card and nbu"). `test_fx_on_card.py::test_card_foreign_op_uses_account_currency_nbu` passes: EUR account, currencyCode=840 (USD merchant), asserts `uah_amount_minor = EUR amount × NBU EUR rate`. `test_fx_rollup_math.py::test_mono_card_and_nbu_identical_when_same_account_amount` passes the no-double-conversion property. |
| SC5 | Sunday-dated transaction uses Friday's NBU rate; API response makes rate date and source visible | VERIFIED | `test_fx_rollup_join.py::test_sunday_tx_uses_friday_rate_and_is_stale` seeds ONLY a Friday `2026-05-08` USD rate, inserts a Sunday `2026-05-10` USD transaction, asserts `fx_rate_date == 2026-05-08` and `fx_stale == True`. `TransactionOut` exposes `fx_rate_date`, `fx_source`, `fx_rate` (Decimal-as-string). All pass. |

**Score:** 5/5 truths VERIFIED.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `alembic/versions/0003_fx_truth.py` | fx_rates + tracked_fx_currencies DDL, USD/EUR seed, attributed_day backfill + NOT NULL | VERIFIED | Exists, 90 lines. Correct ordering: UPDATE before ALTER (Pitfall 3). USD+EUR seeded with `bootstrap_done=false`. |
| `src/finance_bro/db/models.py` | FxRate + TrackedFxCurrency ORM models; attributed_day NOT NULL | VERIFIED | `FxRate.__tablename__ == "fx_rates"`, `Numeric(18,8)` rate column, `PrimaryKeyConstraint("rate_date", "currency")`, `Index("ix_fx_rates_currency_rate_date", "currency", "rate_date")`. `TrackedFxCurrency` with all D-05 fields. `Transaction.attributed_day: Mapped[date]` (nullable=False). |
| `src/finance_bro/importers/nbu.py` | NbuFxImporter.fetch_range; tenacity retry; aclose() | VERIFIED | Exists. `parse_float=Decimal` (not `resp.json()`). No X-Token, no RateLimitGate. 3-attempt tenacity retry. `await self._client.aclose()` present. |
| `src/finance_bro/importers/base.py` | FxRatesPort protocol + FxRateRow dataclass | VERIFIED | `@dataclass(frozen=True) class FxRateRow` with `rate_date: date, currency: str, rate: Decimal`. `class FxRatesPort(Protocol)` with `fetch_range`. |
| `src/finance_bro/db/fx_rate_repo.py` | Idempotent rate upsert + count_in_window | VERIFIED | `on_conflict_do_nothing(index_elements=["rate_date", "currency"])`. `count_in_window` via `text("SELECT count(*) FROM fx_rates WHERE ...")`. |
| `src/finance_bro/db/tracked_fx_currency_repo.py` | Tracked-currency lifecycle CRUD | VERIFIED | `list_currencies` ORDER BY currency (D-17). `upsert_currency`, `set_bootstrap_done`, `mark_attempted`. No `scheduler_state` import or reference. |
| `src/finance_bro/services/fx_rollup.py` | Decimal UAH rollup math + fx_source/fx_stale classification | VERIFIED | `ROUND_HALF_EVEN`, `getcontext().prec = 28`, `Decimal("0.01")` quantize. No `float()` on money path. D-11/D-12/D-13/D-14 semantics all implemented. |
| `src/finance_bro/db/transaction_repo.py` | LATERAL-join read + attributed_day frozen-by-omission upsert | VERIFIED | `ROLLUP_SQL = text("""LEFT JOIN LATERAL ...""")`. `set_={"hold": ..., "amount_minor": ..., "raw_payload": ...}` — exactly 3 columns, `attributed_day` absent (frozen by omission, D-09). Fallback: `t.occurred_at.astimezone(_KYIV).date()` when importer omits it. |
| `src/finance_bro/api/schemas.py` | TransactionOut + 5 FX fields | VERIFIED | `uah_amount_minor: int | None = None`, `fx_rate: str | None = None`, `fx_rate_date: date | None = None`, `fx_source: Literal["native_uah", "mono_card", "nbu"]`, `fx_stale: bool` all present. |
| `src/finance_bro/api/routes_transactions.py` | Route wired through fx_rollup | VERIFIED | `rows = await TransactionRepo(session).list_for_account(card.id)` then `TransactionOut.model_validate(r)`. Rollup computed in the repo. |
| `src/finance_bro/services/fx_bootstrap.py` | maybe_bootstrap_fx + maybe_bootstrap_fx_all_tracked (idempotent, sequential) | VERIFIED | `BOOTSTRAP_THRESHOLD = 250`. Sequential (no `asyncio.gather`). Error isolation per currency. No `scheduler_state` import or write. |
| `src/finance_bro/scheduler/runner.py` | fx_tick coroutine (D-17) | VERIFIED | `async def fx_tick(self) -> None` present. Per-currency error isolation. D-08: `_set_state_auth_failed` count unchanged at 3 (fx_tick adds zero new references). |
| `src/finance_bro/main.py` | CronTrigger fx_tick job + fire-and-forget bootstrap + NBU client aclose | VERIFIED (partial) | `CronTrigger(hour=16, minute=0, timezone=KYIV)` registered. `bootstrap_task = asyncio.create_task(...)`. `await nbu_importer.aclose()` in finally (alongside `await runner.aclose()`). See tzdata WARNING below. |
| `tests/fixtures/nbu_usd_range.json` | exchange_site-shaped USD fixture | VERIFIED | Contains `exchangedate`, `cc`, `rate`, `r030`, `calcdate`. Includes Friday `08.05.2026` and Sunday `10.05.2026` rows. |
| `tests/fixtures/nbu_empty.json` | Empty NBU response | VERIFIED | Parses to `[]`. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `alembic/versions/0003_fx_truth.py` | `transactions.attributed_day` | UPDATE backfill before ALTER SET NOT NULL | VERIFIED | `UPDATE transactions SET attributed_day = (time AT TIME ZONE 'Europe/Kyiv')::date WHERE attributed_day IS NULL` at line 76, `op.alter_column(..., nullable=False)` at line 82 — correct order |
| `src/finance_bro/db/transaction_repo.py` | `fx_rates` | LEFT JOIN LATERAL on (currency, rate_date <= attributed_day) | VERIFIED | `WHERE currency = t.currency AND rate_date <= t.attributed_day ORDER BY rate_date DESC LIMIT 1` — exact D-14 SQL |
| `src/finance_bro/api/routes_transactions.py` | `src/finance_bro/services/fx_rollup.py` | rollup() per row → TransactionOut | VERIFIED | Rollup is called inside `TransactionRepo.list_for_account`; route validates the enriched dicts via `TransactionOut.model_validate(r)` |
| `src/finance_bro/importers/monobank.py` | `transactions.attributed_day` | `time.astimezone(ZoneInfo("Europe/Kyiv")).date()` on CanonicalTransaction | VERIFIED | `attributed_day=occurred_at.astimezone(KYIV).date()` at line 144. `KYIV = ZoneInfo("Europe/Kyiv")` module constant at line 45. |
| `src/finance_bro/main.py` | `src/finance_bro/scheduler/runner.fx_tick` | scheduler.add_job(CronTrigger(hour=16, timezone=ZoneInfo('Europe/Kyiv'))) | VERIFIED | `scheduler.add_job(runner.fx_tick, CronTrigger(hour=16, minute=0, timezone=KYIV), id="fx_tick", max_instances=1, coalesce=True, misfire_grace_time=3600)` at lines 109-116 |
| `src/finance_bro/main.py` | `src/finance_bro/services/fx_bootstrap.py` | asyncio.create_task after scheduler.start() | VERIFIED | `bootstrap_task = asyncio.create_task(fx_bootstrap.maybe_bootstrap_fx_all_tracked())` at line 121 |
| `src/finance_bro/services/fx_bootstrap.py` | `fx_rates` | NbuFxImporter.fetch_range → FxRateRepo.upsert_many | VERIFIED | Fetch outside session block at line 69, upsert inside session block at line 85. `ON CONFLICT DO NOTHING` confirmed. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `routes_transactions.py` → `TransactionOut` | `uah_amount_minor`, `fx_rate`, `fx_rate_date`, `fx_source`, `fx_stale` | LATERAL join in `ROLLUP_SQL` + `fx_rollup.rollup()` | Yes — DB query with real joins; `fx_rate` comes from `fx_rates` table join | FLOWING |
| `fx_rollup.rollup()` | `uah_amount_minor` | `(Decimal(amount_minor) / 100) * fx_rate` — input from DB row | Yes — Decimal arithmetic on real DB values | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| 111 tests pass (full suite) | `uv run pytest` | 111 passed in 5.81s | PASS |
| Sunday→Friday LATERAL fallback | `uv run pytest tests/test_fx_rollup_join.py -v` | PASS | PASS |
| FX-on-card uses account-currency math | `uv run pytest tests/test_fx_on_card.py -v` | PASS | PASS |
| No-rate row still appears with fx_stale=True | `uv run pytest tests/test_fx_stale_fallback.py -v` | PASS | PASS |
| CronTrigger fires at 16:00 Kyiv across DST | `uv run pytest tests/test_fx_cron_dst.py -v` | PASS (×2) | PASS |
| fx_tick D-08/D-16/D-17 contract | `uv run pytest tests/test_fx_tick.py -v` | PASS (×3) | PASS |
| attributed_day backfill Kyiv-correct | `uv run pytest tests/test_attributed_day_migration.py -v` | PASS (×2) | PASS |
| NBU importer Decimal parse + empty→[] | `uv run pytest tests/test_fx_importer_nbu.py -v` | PASS (×2) | PASS |
| Banker's rounding + no double-conversion property | `uv run pytest tests/test_fx_rollup_math.py -v` | PASS (×2) | PASS |
| uah_amount_minor NOT a column on transactions | `grep -c 'uah_amount_minor' src/finance_bro/db/models.py` | 0 | PASS |
| parse_float=Decimal in nbu.py | `grep -c 'parse_float=Decimal' src/finance_bro/importers/nbu.py` | 1 | PASS |
| float() absent from fx_rollup.py | `grep -c 'float(' src/finance_bro/services/fx_rollup.py` | 0 | PASS |
| scheduler_state absent from fx_bootstrap.py (code) | inspected — only docstring comments | 0 code references | PASS |
| attributed_day absent from ON CONFLICT SET clause | lines 118-122 transaction_repo.py | only hold/amount_minor/raw_payload | PASS |
| ZoneInfo('Europe/Kyiv') resolves in dev | `python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Kyiv')"` | succeeds on macOS | PASS (dev) |

### Probe Execution

No `probe-*.sh` scripts declared or found for this phase. Step 7c: SKIPPED (no conventional probes).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| FX-02 | 03-01, 03-02, 03-04 | NBU FX rates fetched daily, 12-month backfill, weekend fallback | SATISFIED | `NbuFxImporter`, `FxRateRepo`, `TrackedFxCurrencyRepo`, `FxBootstrapService`, `fx_tick` CronTrigger, `test_fx_importer_nbu` + `test_fx_tick` + `test_fx_cron_dst` all pass |
| FX-03 | 03-01, 03-03 | UAH rollup computed on read, never denormalized | SATISFIED | `ROLLUP_SQL` LEFT JOIN LATERAL in `transaction_repo.py`, no `uah_amount_minor` column on `transactions`, `test_fx_rollup_join` + `test_fx_stale_fallback` pass |
| FX-04 | 03-03 | FX-on-card uses account-currency amount, no double-conversion | SATISFIED | `fx_source = "mono_card"` label is audit-only; math is always `amount_minor × NBU_rate(account_currency)`. `test_fx_on_card` + `test_fx_rollup_math` (no-double-conversion property) pass |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/finance_bro/api/schemas.py` | 86 | `TODO: add a separate updated_in_place column` | INFO | Pre-existing from Phase 2 (commit `db1ef0d`, `feat(02-04)`); references "D-14" + "v1.5 split" as forward reference. Phase 3 modified this file (added TransactionOut FX fields). No issue number, but clearly a deferred v1.5 item, not a phase 3 gap. |
| `src/finance_bro/scheduler/runner.py` | 278, 363 | `# noqa: BLE001` (RUF100 — unused noqa directive) | INFO | Pre-existing from Phase 2 Mono tick block; confirmed by `git show HEAD~10:runner.py` showing these lines before Phase 3. Phase 3 modified runner.py (added `fx_tick`). The new Phase 3 code is clean. `ruff check` reports these 2 fixable warnings. |
| `pyproject.toml` | N/A | `tzdata` NOT present as unconditional runtime dep | WARNING | SUMMARY.md (plan 03-04) claims "tzdata added unconditionally as runtime dependency." The commit `e4c971b` stat shows only `main.py` changed (1 file, 38+/-1 lines). `pyproject.toml` was NOT in the diff. `tzdata` appears in `uv.lock` only as a `sys_platform == 'win32'` conditional dep of `psycopg` and `tzlocal`. On `python:3.13-slim-trixie` (Linux), there is no `/usr/share/zoneinfo`; without `tzdata` in `pyproject.toml`, `ZoneInfo("Europe/Kyiv")` will raise `ZoneInfoNotFoundError` at runtime in Docker. |

### Human Verification Required

#### 1. tzdata in Docker Runtime Image

**Test:** Build the Docker image from `Dockerfile` and run `docker run --rm finance-bro python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Kyiv'); print('OK')"`.

**Expected:** Exits 0 and prints "OK".

**Why human:** `tzdata` is NOT in `pyproject.toml` despite the SUMMARY claiming it was added. Dev macOS works because the OS ships `/usr/share/zoneinfo`. The Dockerfile uses `python:3.13-slim-trixie` which does NOT include timezone data. Without this fix, the lifespan will crash at `KYIV = ZoneInfo("Europe/Kyiv")` on the first boot in Docker, breaking: the CronTrigger registration, the NBU bootstrap task, and any `ZoneInfo` call in `transaction_repo.py` / `monobank.py`. **Fix:** Add `tzdata` to `pyproject.toml` dependencies and run `uv lock`.

---

## Gaps Summary

No automated test failures. The tzdata omission is the sole unresolved item from this phase's implementation plan. The SUMMARY.md claims "tzdata added unconditionally" but this is contradicted by:
1. `pyproject.toml` has no `tzdata` entry (confirmed by `cat pyproject.toml | grep -i tz` returning nothing)
2. `git log -- pyproject.toml` shows the last change was Phase 2 (`4496c35`, `chore(02-01): install apscheduler`)
3. `git show e4c971b --stat` shows only `main.py` changed in the commit described as "add tzdata"

This is a production deploy blocker (Docker runtime fails on ZoneInfo resolution) that passes in CI because tests run on macOS with system zoneinfo available. The automated test `test_fx_cron_dst.py` does exercise `ZoneInfo("Europe/Kyiv")` but passes on macOS, masking the Docker gap.

All other Phase 3 implementation is solidly verified:
- FX-02 (NBU fetch + bootstrap + cron): fully wired, fully tested
- FX-03 (computed on read, never denormalized): LATERAL join confirmed, no denorm column
- FX-04 (no double-conversion): property test + integration test pass
- D-09 (attributed_day backfill + NOT NULL): migration correct, test passes
- D-08 (no scheduler_state from FX path): confirmed zero writes
- D-13 (fx_stale semantics): correct in both directions
- Decimal discipline: parse_float=Decimal, ROUND_HALF_EVEN, no float() on money path

---

_Verified: 2026-05-30T18:45:00Z_
_Verifier: Claude (gsd-verifier)_
