# Phase 3: UAH Truth - Research

**Researched:** 2026-05-30
**Domain:** NBU FX rate ingestion + on-read UAH rollup for a single-user Monobank importer (FastAPI 0.136 / SQLAlchemy 2.0 / Postgres 17 / APScheduler 3.11)
**Confidence:** HIGH (NBU endpoints verified live; existing code read directly; all dependencies already pinned)

## Summary

Phase 3 adds honest UAH rollups to every foreign-currency transaction by (1) fetching NBU rates into a new `fx_rates` table — a 12-month backfill on first boot plus a daily 16:00 Europe/Kyiv cron — and (2) computing the UAH equivalent **on read** via a `LEFT JOIN LATERAL` against `fx_rates`. The architecture is fully specified by 03-CONTEXT.md (D-01 through D-17, all LOCKED); this research's job was to confirm the NBU endpoint shape, the weekend/holiday behavior, the SQLAlchemy LATERAL syntax, and the runtime preconditions in the existing codebase.

The single most consequential empirical finding: **NBU's range endpoint now carries forward rates server-side for every calendar day** (weekends and holidays included), returning exactly one row per calendar day (366 rows for a 12-month range). This is materially different from PITFALLS.md Pitfall 7's assumption that weekends return empty arrays. It does NOT change any locked decision — the LATERAL `rate_date <= attributed_day ORDER BY rate_date DESC` fallback (D-14) is still correct and still required as a safety net (cold-boot, pre-bootstrap, fringe currencies, and historical robustness) — but it means the **happy-path weekend test fixture should reflect that NBU itself supplies a carried-forward Sunday row**, and the planner should write the Sunday-uses-Friday test using a *deliberately sparse* `fx_rates` table (insert only the Friday row) to exercise the LATERAL fallback in isolation, exactly as D-14/the CONTEXT testing list already prescribes.

**Primary recommendation:** Use the **`exchange_site` range endpoint** (`https://bank.gov.ua/NBU_Exchange/exchange_site?start=YYYYMMDD&end=YYYYMMDD&valcode=USD&json`) for both bootstrap and daily tick — it is the true one-currency-one-call range endpoint D-01 selected, it returns a clean per-currency JSON array with `exchangedate`/`rate`/`cc`/`calcdate`, and an empty/unknown result is an HTTP-200 empty array `[]` (never a 4xx), which maps directly onto D-16. All required dependencies (`tenacity`, `httpx`, `apscheduler`, `freezegun`, `respx`, `testcontainers`) are already installed — Phase 3 adds **zero** new top-level dependencies.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| NBU rate fetch (HTTP) | Importer adapter (`importers/nbu.py`) | — | Outbound HTTP is the importer seam; mirrors `MonobankImporter`. structlog redaction already covers it. |
| FX rate persistence | Repository (`db/fx_rate_repo.py`) | DB (`fx_rates` table) | Repos own all SQL; no SQLA leakage outside `db/`. |
| Tracked-currency lifecycle | Repository (`db/tracked_fx_currency_repo.py`) | DB (`tracked_fx_currencies`) | Lazy auto-add + bootstrap state lives in one table, one repo. |
| 12-month bootstrap orchestration | Service (`services/fx_bootstrap.py` or helper) | Importer + 2 repos | Idempotent `maybe_bootstrap_fx(currency)`; fire-and-forget from lifespan. |
| Daily cron tick | Scheduler (`scheduler/runner.py` — new `fx_tick`) | Service + repos | Second APScheduler job in the existing in-process `AsyncIOScheduler`. |
| UAH rollup math | Service helper (`services/fx_rollup.py`) + Repo (LATERAL join) | DB (Postgres LATERAL) | Join in SQL (one round-trip); Decimal arithmetic in Python. |
| `attributed_day` derivation | Importer boundary (`monobank.py`) + migration backfill | DB (NOT NULL after 0003) | Kyiv-day attribution frozen on first write (Phase 2 D-10 invariant extended). |
| FX field exposure | API schema (`api/schemas.py` — `TransactionOut`) | Route (`routes_transactions.py`) | Five new computed fields; no new endpoint. |

## Standard Stack

Phase 3 introduces **no new top-level dependency**. Everything is already pinned in `pyproject.toml` / `uv.lock`.

### Core
| Library | Version (installed) | Purpose | Why Standard |
|---------|--------------------|---------|--------------|
| `httpx` | 0.28.1 | NBU AsyncClient | Same event loop as FastAPI/APScheduler; already the Mono client. `[VERIFIED: pyproject.toml]` |
| `tenacity` | 9.1.4 | Retry decorator for NBU 5xx/network | D-07 retry (3 attempts, exp backoff). **Already installed** — Open Question 6 answered. `[VERIFIED: pyproject.toml line 22]` |
| `apscheduler` | 3.11.2 | `CronTrigger(hour=16, timezone=ZoneInfo("Europe/Kyiv"))` | Second job in the existing `AsyncIOScheduler`. `[VERIFIED: pyproject.toml line 12]` |
| `SQLAlchemy` | 2.0.49 | `fx_rates`/`tracked_fx_currencies` models + LATERAL join | `lateral()` + `.lateral()` Core constructs support the D-14 query. `[VERIFIED: pyproject.toml]` |
| `alembic` | 1.18.4 | Migration `0003_fx_truth.py` | Same authors as SQLA; matches 0001/0002 style. `[VERIFIED: pyproject.toml]` |
| `psycopg` | 3.3.4 (`postgresql+psycopg://`) | Postgres 17 async driver | LATERAL is native Postgres. `[VERIFIED: pyproject.toml]` |
| stdlib `zoneinfo` | Python 3.13 | `ZoneInfo("Europe/Kyiv")` for cron + `attributed_day` | Project mandates `zoneinfo`, never `pytz` (Pitfall 14). `[CITED: CLAUDE.md]` |
| stdlib `decimal` | Python 3.13 | `Decimal` rollup math, `ROUND_HALF_EVEN` | No float for money (Pitfall 1). `[CITED: CLAUDE.md §Money]` |

### Supporting (test-only — already present)
| Library | Version | Purpose |
|---------|---------|---------|
| `respx` | 0.23.1 | Mock the NBU `exchange_site` endpoint in `test_fx_importer_nbu.py`. `[VERIFIED: pyproject.toml line 64]` |
| `freezegun` | 1.5.5 | Freeze time for the DST cron test (`test_fx_cron_dst.py`). `[VERIFIED: pyproject.toml line 60]` |
| `testcontainers` | 4.14.2 | Real Postgres 17 for LATERAL-join + migration tests. `[VERIFIED: pyproject.toml line 66]` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `exchange_site` range endpoint | `statdirectory/exchange?valcode=&date=` per-day loop | D-01 fallback. ~366 calls/currency on bootstrap vs ~1. NBU has no rate limit so tolerable, but the range endpoint is cleaner. |
| LATERAL join | Python-side per-row lookup | LATERAL is one round-trip; Python loop is N+1. LATERAL is the locked choice (D-14). |
| `holidays` PyPI package | (none) | Explicitly deferred (CONTEXT Deferred Ideas) — NBU now carries forward rates, so "is it a holiday?" is moot for the rollup. |

**Installation:** None required. Confirm with:
```bash
uv sync   # all deps already in uv.lock; no `uv add` needed
```

**Version verification:** All versions read directly from `pyproject.toml` (lines 10-24, 55-67). `tenacity==9.1.4` and `freezegun==1.5.5` confirmed present — the two dependencies the open questions flagged as uncertain.

## Package Legitimacy Audit

> No new external packages are installed in Phase 3. All libraries used are already pinned in `pyproject.toml`/`uv.lock` and were vetted in Phase 1/2 research. slopcheck not re-run — zero install surface.

| Package | Registry | Disposition |
|---------|----------|-------------|
| (none) | — | No new packages. Phase 3 reuses the existing locked dependency set. |

## NBU API — Verified Endpoint Shapes (Open Questions 1, 2, 3)

All of the following were fetched **live on 2026-05-30** via `curl`. `[VERIFIED: live curl against bank.gov.ua]`

### Range endpoint (D-01 primary — use this)

```
https://bank.gov.ua/NBU_Exchange/exchange_site?start={YYYYMMDD}&end={YYYYMMDD}&valcode={ALPHA}&json
```

- **12-month USD bootstrap:** `?start=20250508&end=20260508&valcode=USD&json` → **366 rows** (one per calendar day; verified `unique=366`).
- **Single-day tick:** `?start=20260508&end=20260508&valcode=USD&json` → 1 row.
- `valcode` **filters server-side** to one currency. `sort`/`order` are optional (default ascending by date).
- `start > end` → HTTP 200, `[]` (count 0). Not an error.

**Response JSON shape** (array of objects; field names are literal):
```json
[
  {
    "exchangedate": "08.05.2026",   // dd.mm.yyyy — PARSE WITH "%d.%m.%Y"
    "r030": 840,                     // ISO-4217 numeric code
    "cc": "USD",                     // ISO-4217 alpha — use this for fx_rates.currency
    "txt": "Долар США",              // Ukrainian name (ignore)
    "enname": "US Dollar",           // English name (ignore)
    "rate": 43.8033,                 // the mid-rate — store as NUMERIC(18,8). Read as str, NEVER float.
    "units": 1,
    "rate_per_unit": 43.8033,        // == rate when units==1 (true for USD/EUR/PLN/GBP/CHF)
    "group": "1",
    "calcdate": "07.05.2026",        // the business day the rate was actually CALCULATED on
    "special": "N"                   // "N" or null — not load-bearing for v1
  }
]
```

**CRITICAL: parse `rate` from the raw JSON text as a string, not a Python float.** `httpx`'s `.json()` will decode `43.8033` into a Python `float`, reintroducing the exact drift Pitfall 1 forbids. Either (a) re-serialize via `str()` immediately and construct `Decimal(str(rate))`, or (b) parse the response body with `json.loads(body, parse_float=Decimal)`. Option (b) is cleaner and matches the Pitfall-1 guidance verbatim. `[VERIFIED: PITFALLS.md Pitfall 1]`

### Per-day fallback endpoint (D-01 fallback only)

```
https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode={ALPHA}&date={YYYYMMDD}&json
```
- Same `exchangedate`/`rate`/`cc`/`r030` fields (no `calcdate`/`enname`/`units`).
- Single date only — would require a ~366-call loop for bootstrap. Keep as the documented fallback per D-01; do not use unless `exchange_site` misbehaves.

### Weekend / holiday behavior (Open Question 2 — ANSWERED, with a surprise)

**NBU now carries forward rates server-side for every calendar day.** Verified:
- Sat 03.01.2026 USD = 42.1701 with `calcdate=01.01.2026` (carried forward from the Jan-1 holiday calculation).
- Sun 10.05.2026 USD = 43.8033 = Fri 08.05.2026 rate.
- Christmas 25.12.2025 returns a rate.

**Carry-forward is detectable:** when `calcdate < exchangedate`, the rate is a carried-forward weekend/holiday value. The range endpoint thus returns a dense, gap-free series.

**Empty / unsupported result:** HTTP **200** with body `[]` (literal `[\n]`). Verified for:
- Unknown valcode (`valcode=XYZ` or `valcode=ZWL`) → `[]`.
- Future range with no data yet (`start=20271201&end=20271210`) → `[]`.
- A far-future single date on the per-day endpoint → HTTP **504** (gateway timeout) — treat any non-200 OR empty array as "no rates" per D-16.

This maps **directly** onto D-16: an empty array means `bootstrap_done` stays `false`, `last_error="no rates published"`, retry next tick. **No 4xx handling branch is needed** for the unsupported-currency case — only empty-array detection. `[VERIFIED: live curl]`

### Minor-currency availability (Open Question 3 — ANSWERED)

PLN (985), GBP (826), CHF (756) **all return daily rates** with `units=1` and `rate_per_unit==rate`. Verified on 2026-05-08. The lazy auto-add path (D-15) will succeed for these on first observation; no special retry aggressiveness needed. `[VERIFIED: live curl]`

> **Implication for the weekend-fallback test (D-14 / CONTEXT testing list):** because NBU itself now carries forward, the `test_fx_rollup_join.py` "Sunday uses Friday" test must seed `fx_rates` with **only** the Friday row (not let the importer fetch a dense range), so the LATERAL `rate_date <= attributed_day ORDER BY rate_date DESC LIMIT 1` is the thing under test. This is exactly what the CONTEXT.md testing list describes; just be explicit that the test does NOT call the live importer.

## Architecture Patterns

### System Architecture Diagram

```
                          ┌─────────────────────────────────────────┐
   FastAPI lifespan ──────┤ on startup, AFTER scheduler.start():     │
   (main.py)              │  asyncio.create_task(                    │
                          │    maybe_bootstrap_fx_all_tracked())     │ fire-and-forget (D-07)
                          └───────────────┬─────────────────────────┘
                                          │
   AsyncIOScheduler ───── fx_tick ───────►│  (CronTrigger 16:00 Europe/Kyiv, D-06)
   (existing, +1 job)     daily           │
                                          ▼
                          ┌───────────────────────────────────────┐
                          │ maybe_bootstrap_fx(currency)           │
                          │  - read tracked_fx_currencies          │◄── TrackedFxCurrencyRepo
                          │  - if < ~250 rows in last 365d:        │
                          │      NbuFxImporter.fetch_range() ──────┼──► GET exchange_site (httpx + tenacity)
                          │  - upsert ON CONFLICT DO NOTHING ──────┼──► FxRateRepo → fx_rates
                          │  - set bootstrap_done / last_error     │
                          └───────────────────────────────────────┘

   Mono importer insert path (existing):
     CanonicalTransaction{ +attributed_day = time→Kyiv date (D-09) }
        │
        ├─► TransactionRepo.insert_many (attributed_day in INSERT cols, OMITTED from SET — frozen, D-09)
        │
        └─► after commit: if currency ∉ tracked → INSERT tracked_fx_currencies
                          + asyncio.create_task(bootstrap_currency(ccy))   (lazy auto-add, D-15)

   GET /api/transactions (read path):
     TransactionRepo.list_for_account
        │  SELECT t.*, fx.rate, fx.rate_date
        │  FROM transactions t
        │  LEFT JOIN LATERAL (                          ◄── D-14 (Postgres LATERAL, one round-trip)
        │    SELECT rate, rate_date FROM fx_rates
        │    WHERE currency=t.currency AND rate_date<=t.attributed_day
        │    ORDER BY rate_date DESC LIMIT 1) fx ON true
        ▼
     fx_rollup helper: uah_amount_minor, fx_source, fx_stale   ──► TransactionOut (+5 fields, D-10)
```

### Recommended Project Structure (additions only)
```
src/finance_bro/
├── importers/
│   ├── base.py                 # + FxRatesPort protocol, + FxRateRow dataclass (D-02)
│   ├── nbu.py                  # NEW — NbuFxImporter.fetch_range() (D-02)
│   ├── monobank.py             # EXTEND — set attributed_day on CanonicalTransaction (D-09)
│   └── currency_map.py         # EXTEND — PLN/GBP/CHF/… long tail (Discretion)
├── db/
│   ├── models.py               # + FxRate, + TrackedFxCurrency; attributed_day → NOT NULL
│   ├── transaction_repo.py     # REPLACE list_for_account with LATERAL join (D-14);
│   │                           #   add attributed_day to insert_many INSERT cols (frozen)
│   ├── fx_rate_repo.py         # NEW — upsert (ON CONFLICT DO NOTHING) + count-in-window
│   └── tracked_fx_currency_repo.py  # NEW — iterate, upsert, set bootstrap/last_error
├── services/
│   ├── fx_bootstrap.py         # NEW — maybe_bootstrap_fx / bootstrap_currency (D-03)
│   └── fx_rollup.py            # NEW — Decimal rollup math + fx_source/fx_stale (D-11..D-14)
├── scheduler/runner.py         # + fx_tick coroutine (D-17)
├── main.py                     # + scheduler.add_job(fx_tick,…) + create_task(bootstrap) (D-06/D-07)
└── api/schemas.py              # TransactionOut + 5 FX fields (D-10)

alembic/versions/0003_fx_truth.py  # NEW (see Don't Hand-Roll + migration shape below)
```

### Pattern 1: SQLAlchemy 2.0 LEFT JOIN LATERAL (Open Question 5 — ANSWERED)

SQLAlchemy Core/ORM expresses LATERAL via `.lateral()` on a subquery and `isouter=True` for the LEFT JOIN with an `ON true` condition. Two viable approaches:

**Approach A — raw `text()` (lowest risk, matches D-14's literal SQL):**
```python
# Source: D-14 verbatim SQL; Postgres LATERAL native.
from sqlalchemy import text

ROLLUP_SQL = text("""
    SELECT t.id, t.account_id, t.source_tx_id, t.amount_minor, t.currency,
           t.time, t.hold, t.raw_payload, t.attributed_day,
           fx.rate AS fx_rate, fx.rate_date AS fx_rate_date
    FROM transactions t
    LEFT JOIN LATERAL (
        SELECT rate, rate_date
        FROM fx_rates
        WHERE currency = t.currency
          AND rate_date <= t.attributed_day
        ORDER BY rate_date DESC
        LIMIT 1
    ) fx ON true
    WHERE t.account_id = :account_id AND NOT t.is_deleted
    ORDER BY t.time DESC
""")
rows = (await session.execute(ROLLUP_SQL, {"account_id": account_id})).mappings().all()
```

**Approach B — Core constructs (`select().lateral()` + `.join(..., isouter=True)`):**
```python
# Source: SQLAlchemy 2.0 docs — Select.lateral()
from sqlalchemy import select, literal, true
fx_sub = (
    select(FxRate.rate, FxRate.rate_date)
    .where(FxRate.currency == Transaction.currency)
    .where(FxRate.rate_date <= Transaction.attributed_day)
    .order_by(FxRate.rate_date.desc())
    .limit(1)
    .lateral("fx")
)
stmt = (
    select(Transaction, fx_sub.c.rate, fx_sub.c.rate_date)
    .select_from(Transaction.__table__.join(fx_sub, true(), isouter=True))
    .where(Transaction.account_id == account_id, Transaction.is_deleted.is_(False))
    .order_by(Transaction.time.desc())
)
```

**Recommendation: use Approach A (`text()`).** It matches D-14's locked SQL verbatim, is trivially reviewable, and avoids the one real SQLA-LATERAL gotcha (the correlated column `Transaction.currency` inside `.lateral()` must reference the *outer* table — Core handles this by auto-correlation, but it's a known source of "subquery references unbound table" errors when the outer table isn't in the enclosing `select_from`). The repo already uses `text()` for the `(xmax = 0)` trick, so raw SQL is an established pattern here. `[VERIFIED: transaction_repo.py uses text(); CITED: docs.sqlalchemy.org/en/20 Select.lateral]`

**Index coverage:** `CREATE INDEX ON fx_rates (currency, rate_date DESC)` covers the LATERAL lookup's `WHERE currency = ? AND rate_date <= ? ORDER BY rate_date DESC LIMIT 1` exactly — Postgres can satisfy it with an index-only backward scan stopping at the first match. The PK `(rate_date, currency)` does NOT cover this (wrong leading column), so the explicit `(currency, rate_date DESC)` index in D-04 is necessary, not redundant. `[VERIFIED: Postgres index semantics; CITED: D-04]`

### Pattern 2: APScheduler CronTrigger with ZoneInfo + DST (Open Question 6 — ANSWERED)

```python
# Source: APScheduler 3.x docs — CronTrigger timezone; matches existing IntervalTrigger usage in main.py
from zoneinfo import ZoneInfo
from apscheduler.triggers.cron import CronTrigger

scheduler.add_job(
    runner.fx_tick,
    CronTrigger(hour=16, minute=0, timezone=ZoneInfo("Europe/Kyiv")),  # D-06
    id="fx_tick",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=3600,
)
```

- APScheduler 3.x computes the next fire time in the supplied timezone and re-evaluates the UTC offset at each fire, so DST transitions are handled correctly. `coalesce=True` + `misfire_grace_time=3600` mean: if the container was down at 16:00, the first tick within the next hour runs **once** (collapsed), not N times. `[CITED: apscheduler.readthedocs.io/en/3.x CronTrigger]`
- **Ukraine note:** Ukraine still observes DST as of 2026 (the 2024 abolition bill did not pass into effect). `Europe/Kyiv` in the OS tzdata reflects current rules; relying on `ZoneInfo` (not `pytz`) is correct per Pitfall 14. The DST test (`test_fx_cron_dst.py`) should assert the *next fire time* the trigger computes around the last-Sunday-of-October boundary, using `freezegun` — it does not require an actual hour to pass. `[ASSUMED — DST policy; see Assumptions Log A1]`

### Pattern 3: attributed_day frozen-on-first-write (D-09)

Extend `TransactionRepo.insert_many` to add `"attributed_day": t.attributed_day` to the INSERT `rows` dict **and leave it OUT of the `on_conflict_do_update set_={…}`** — identical to how `description`/`mcc` are already frozen-by-omission. The importer computes it as `t.occurred_at.astimezone(ZoneInfo("Europe/Kyiv")).date()`. This preserves the Phase 2 D-10 invariant: a hold→cleared upsert must not shift `attributed_day` even if Mono moves the `time`. `[VERIFIED: transaction_repo.py lines 46-72; CITED: D-09]`

### Anti-Patterns to Avoid
- **Denormalizing `uah_amount_minor` into `transactions`** — forbidden by FX-03 (immutable). Always compute on read.
- **Re-converting Mono-already-converted amounts via the operation-currency leg** — Pitfall 8. Always `amount_minor × NBU_rate(transactions.currency, attributed_day)`. The `mono_card` label is audit-only; the math is identical to `nbu`.
- **`float(rate)` anywhere** — parse NBU `rate` as `Decimal` at the edge (`parse_float=Decimal`).
- **Adding an FK from `transactions` to `fx_rates`** — wrong; the rollup must fall back to prior dates (Pitfall 7). No FK.
- **Putting NBU through `RateLimitGate`** — NBU has no documented rate limit; the gate is Mono-only.
- **Lighting up `fx_stale` for a failed cron** — D-13: `fx_stale` is solely about rate-date mismatch / no-rate-found, never about cron health.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Weekend/holiday gap fill | A Ukrainian-holiday calendar + business-day arithmetic | NBU's own carry-forward + the LATERAL `rate_date <= day` fallback | NBU already carries forward; the LATERAL handles any residual gap. `holidays` pkg is deferred. |
| NBU date range fetch | A per-day loop calling 366 endpoints | `exchange_site?start=&end=&valcode=` (one call) | D-01; one round-trip per currency. |
| Retry/backoff on NBU 5xx | `try/except/sleep` loop | `tenacity` (already installed) | Exponential backoff, 3 attempts (D-07). |
| Idempotent rate upsert | SELECT-then-INSERT | `INSERT … ON CONFLICT (rate_date, currency) DO NOTHING` | D-03; range re-fetches are cheap and safe. |
| Cron DST handling | Manual UTC-offset math | `CronTrigger(timezone=ZoneInfo("Europe/Kyiv"))` | APScheduler re-evaluates offset per fire. |
| Decimal rounding | `round()` (float) | `Decimal.quantize(Decimal("0.01"), ROUND_HALF_EVEN)` | Banker's rounding (Pitfall 1 / D-14). |
| numeric→alpha currency | Inline dict literals | Extend `currency_map.numeric_to_alpha` | Single source of truth (Pitfall 20). |

**Key insight:** The hardest-looking part of this phase — "handle weekend/holiday FX gaps correctly" — is mostly solved by NBU's server-side carry-forward plus the one LATERAL subquery. The risk is not in the gap logic; it's in **(a) not double-converting FX-on-card amounts** and **(b) never letting a float touch a rate**. Concentrate the property tests there.

## Runtime State Inventory

> Phase 3 is partly a migration phase (it backfills `attributed_day` and tightens it to NOT NULL, and seeds `tracked_fx_currencies`). Inventory below.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `transactions.attributed_day` is currently `DATE NULL` (models.py:67) and **Phase 2's importer never populated it** — every existing row has `attributed_day = NULL`. | **Data migration** (one-shot `UPDATE … SET attributed_day = (time AT TIME ZONE 'Europe/Kyiv')::date WHERE attributed_day IS NULL`) **+ code edit** (importer sets it on insert going forward). Both required (D-09). |
| Stored data | `fx_rates` is empty on first boot (migration seeds no rate rows — D-04 bullet). | Lifespan bootstrap fills it; no migration data seed. |
| Live service config | None — NBU is stateless, no token, no UI-stored config. | None. Verified: no NBU credentials anywhere in repo. |
| OS-registered state | APScheduler jobs live in `MemoryJobStore` (runner.py docstring), re-registered every boot in `lifespan()`. Adding `fx_tick` is a code edit, not OS-registered state. | Code edit in `main.py` only. |
| Secrets/env vars | NBU needs **no** secret. `NBU_BASE` could be an optional env var (STACK.md compose skeleton mentions it) but a hardcoded constant in `nbu.py` mirroring `MONO_BASE` is fine and matches the existing pattern. | None (constant) — or add an optional `nbu_base` to `Settings` for symmetry; planner's call. |
| Build artifacts | None — no compiled artifacts; `tzdata` is OS-provided in the Debian slim image (see Environment Availability). | None, but verify tzdata presence (below). |

## Common Pitfalls

### Pitfall 1: Float drift on the NBU rate (most likely real bug)
**What goes wrong:** `httpx`'s `resp.json()` decodes `"rate": 43.8033` into a Python `float`; constructing `Decimal(43.8033)` then carries binary-float garbage (`Decimal('43.80330000000000...')`).
**Why it happens:** Default JSON decoding. The CLAUDE.md rule "never `float()` a Decimal" is usually applied on the *output* side; the *input* side (NBU) is the new surface.
**How to avoid:** In `NbuFxImporter`, decode with `json.loads(resp.text, parse_float=Decimal)` (or `Decimal(str(raw_rate))`). Store as `NUMERIC(18,8)`.
**Warning signs:** `fx_rate` strings in the API like `"43.80330000000001"`.

### Pitfall 2: Double-conversion of FX-on-card (Pitfall 8 / FX-04)
**What goes wrong:** Triangulating an EUR-account/USD-merchant transaction via `operationAmount` and a USD/UAH rate instead of using the settled EUR `amount` × EUR/UAH.
**How to avoid:** Math is ALWAYS `amount_minor × NBU_rate(transactions.currency, attributed_day)`. `fx_source = "mono_card"` is set when `numeric_to_alpha(raw_payload->>'currencyCode') != transactions.currency`, but it changes only the **label**, never the formula (D-11).
**Warning signs:** A property test where `mono_card` and `nbu` rows with the same account-currency amount + same day produce different UAH values.

### Pitfall 3: `attributed_day` NOT NULL migration on an empty-or-populated table
**What goes wrong:** `ALTER COLUMN … SET NOT NULL` fails if any row still has `NULL` after the backfill (e.g., a row with a `time` that the timezone cast somehow left null — won't happen, but ordering matters).
**How to avoid:** In migration 0003, run the `UPDATE` **before** the `SET NOT NULL`, in that order, single transaction (Postgres DDL is transactional). Test both directions: `test_attributed_day_migration.py` inserts a NULL-attributed_day row pre-upgrade and asserts it's Kyiv-correct post-upgrade.
**Warning signs:** Migration fails with `column "attributed_day" contains null values`.

### Pitfall 4: LATERAL correlation unbound (SQLA Core form)
**What goes wrong:** Using `select().lateral()` but `Transaction` isn't in the enclosing `select_from`, so `Transaction.currency` inside the lateral raises a compile error.
**How to avoid:** Prefer the `text()` form (Approach A). If using Core, ensure `Transaction.__table__` is the left side of the `.join(fx_sub, true(), isouter=True)`.

### Pitfall 5: Cron timezone falls back to UTC if `Europe/Kyiv` unresolvable
**What goes wrong:** If the container lacks OS tzdata AND the `tzdata` PyPI package (currently win32-only — see Environment Availability), `ZoneInfo("Europe/Kyiv")` raises `ZoneInfoNotFoundError`, and a naive fallback to UTC fires the cron at 16:00 UTC = 19:00 Kyiv (summer).
**How to avoid:** Confirm tzdata availability in the runtime image; if uncertain, add `tzdata` as an unconditional dependency (drop the `marker = "sys_platform == 'win32'"`). See Environment Availability.

## Code Examples

### NBU range fetch (NbuFxImporter — D-02)
```python
# Source: live-verified exchange_site shape (curl 2026-05-30) + PITFALLS.md Pitfall 1
import json
from datetime import date, datetime
from decimal import Decimal
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

NBU_BASE = "https://bank.gov.ua/NBU_Exchange/exchange_site"

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, max=30),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
async def fetch_range(client: httpx.AsyncClient, currency: str, start: date, end: date) -> list["FxRateRow"]:
    resp = await client.get(
        NBU_BASE,
        params={"start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
                "valcode": currency, "json": ""},
    )
    resp.raise_for_status()
    raw = json.loads(resp.text, parse_float=Decimal)   # never float the rate
    return [
        FxRateRow(
            rate_date=datetime.strptime(r["exchangedate"], "%d.%m.%Y").date(),
            currency=r["cc"],
            rate=Decimal(str(r["rate"])),               # belt-and-suspenders Decimal
        )
        for r in raw                                    # raw == [] on weekend-only/unknown ccy
    ]
```

### UAH rollup math (fx_rollup helper — D-11..D-14)
```python
# Source: D-14 + CLAUDE.md §Money (ROUND_HALF_EVEN, kopeck quantize)
from decimal import Decimal, ROUND_HALF_EVEN

def rollup(amount_minor: int, currency: str, fx_rate: Decimal | None,
           fx_rate_date: date | None, attributed_day: date,
           op_currency_alpha: str | None) -> "FxFields":
    if currency == "UAH":
        return FxFields(uah_amount_minor=amount_minor, fx_rate="1.00000000",
                        fx_rate_date=attributed_day, fx_source="native_uah", fx_stale=False)
    source = "mono_card" if (op_currency_alpha and op_currency_alpha != currency) else "nbu"
    if fx_rate is None:                                  # D-12 no-rate-available
        return FxFields(None, None, None, source, fx_stale=True)
    major = (Decimal(amount_minor) / 100) * fx_rate
    uah_minor = int(major.quantize(Decimal("0.01"), ROUND_HALF_EVEN) * 100)
    stale = fx_rate_date < attributed_day                # D-13
    return FxFields(uah_minor, f"{fx_rate:.8f}", fx_rate_date, source, fx_stale=stale)
```

### attributed_day on the importer boundary (D-09)
```python
# Source: monobank.py fetch_statement — add one field to CanonicalTransaction
from zoneinfo import ZoneInfo
KYIV = ZoneInfo("Europe/Kyiv")
# inside the yield:
attributed_day=datetime.fromtimestamp(item["time"], tz=UTC).astimezone(KYIV).date()
```

## State of the Art

| Old Assumption (research dated 2026-05-10) | Current Reality (verified 2026-05-30) | Impact |
|--------------------------------------------|---------------------------------------|--------|
| NBU returns `[]` on weekends/holidays (Pitfall 7) | NBU **carries rates forward** server-side on `exchange_site`; returns a dense per-calendar-day series | The LATERAL fallback is still correct/required but is now a *safety net*, not the primary gap-filler. Weekend test must use a deliberately sparse `fx_rates`. |
| `exchangenew?json&date=` is the endpoint (STACK.md / CLAUDE.md TL;DR) | `exchangenew` was not the path that worked; `statdirectory/exchange` (per-day) and `NBU_Exchange/exchange_site` (range) are the live endpoints | Use `exchange_site` for range (D-01). `exchangenew` is stale guidance. |
| Empty/unsupported currency might be a 4xx (Open Question 2) | HTTP 200 + `[]` for unknown valcode and future ranges; 504 only for far-future single-date | D-16 needs empty-array detection, not 4xx handling. |

**Deprecated/outdated:**
- The `exchangenew?json` endpoint cited in CLAUDE.md and STACK.md — superseded by the verified `exchange_site` (range) and `statdirectory/exchange` (per-day) endpoints. The fetched evidence overrides the older research.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Ukraine still observes DST in 2026 (so `Europe/Kyiv` has an Oct/Mar transition the cron test should cover). | Pattern 2 | If DST were abolished, the DST test asserts a transition that doesn't occur — test would need rewriting, but the cron's `ZoneInfo` behavior is correct either way. Low risk. |
| A2 | NBU's carry-forward behavior is permanent, not a transient API change. | NBU API findings | If NBU reverts to empty-array weekends, the LATERAL fallback (already required) covers it — no code change needed. Self-mitigating. |
| A3 | `units == 1` and `rate_per_unit == rate` for all currencies the user will hold (USD/EUR/PLN/GBP/CHF verified). | NBU API findings | A currency with `units != 1` (e.g., some exotic ones) would need `rate_per_unit` instead of `rate`. Use `rate_per_unit` defensively to be safe — recommend planner use `rate_per_unit` field, which equals `rate` when units==1. |
| A4 | The Debian `python:3.13-slim-trixie` runtime image ships `/usr/share/zoneinfo` (OS tzdata). | Environment Availability | If stripped, `ZoneInfo("Europe/Kyiv")` fails → cron mis-fires. Mitigation: add unconditional `tzdata` dep. See Environment Availability. |

## Open Questions

All six priority open questions were resolved:
1. **NBU range endpoint** — RESOLVED. `NBU_Exchange/exchange_site?start=&end=&valcode=&json`, shape verified. Per-day fallback `statdirectory/exchange`.
2. **Non-business-day / empty response** — RESOLVED. HTTP 200 + `[]` (empty array). NBU otherwise carries forward.
3. **Minor currencies (PLN/GBP/CHF)** — RESOLVED. All publish daily; no special retry needed.
4. **`raw_payload->>'currencyCode'` on every Mono row** — RESOLVED. Present on every statementItem fixture (`tests/fixtures/statement_two_items.json` etc. all carry `currencyCode`). The defensive `nbu` fallback in D-11 remains but is unlikely to trigger for cards.
5. **Postgres LATERAL in SQLA 2.0** — RESOLVED. Use `text()` (Approach A) matching D-14; index `(currency, rate_date DESC)` covers the lookup.
6. **APScheduler CronTrigger + DST + tenacity** — RESOLVED. `CronTrigger(timezone=ZoneInfo(...))` handles DST; `coalesce`+`misfire_grace_time=3600` give catch-up. `tenacity==9.1.4` already installed.

One residual (low-priority):
- **A4 / tzdata in the runtime image** — should be confirmed at plan time (cheap to check; cheap to fix by adding an unconditional `tzdata` dep).

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `bank.gov.ua` NBU API (no auth) | All FX fetch | ✓ (live-verified 2026-05-30) | — | Per-day `statdirectory/exchange` if range endpoint degrades (D-01) |
| `tenacity` | NBU retry (D-07) | ✓ | 9.1.4 | — |
| `freezegun` | DST cron test | ✓ | 1.5.5 | — |
| `respx` | NBU mock in tests | ✓ | 0.23.1 | — |
| `testcontainers` (Postgres 17) | LATERAL/migration tests | ✓ | 4.14.2 | — |
| OS tzdata (`Europe/Kyiv`) | Cron + attributed_day | ⚠ UNVERIFIED in container | — | Add unconditional `tzdata` PyPI dep (currently win32-only in uv.lock) |

**Missing dependencies with no fallback:** None.

**Missing dependencies with fallback / to verify:**
- **OS tzdata in the runtime image (A4).** `uv.lock` pins `tzdata` only for `sys_platform == 'win32'`, so on the Linux container the app relies on the OS zoneinfo database. Debian slim images normally include `/usr/share/zoneinfo`, but this is unverified for `python:3.13-slim-trixie`. **Recommendation for the planner:** either (a) verify `python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Kyiv')"` succeeds in the built image as a phase-gate check, or (b) drop the win32 marker and add `tzdata` unconditionally to `dependencies`. Option (b) is the zero-risk choice and costs ~200 KB.

## Validation Architecture

> `workflow.nyquist_validation` is not set to false in config.json → validation section included.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.3 + pytest-asyncio 1.3.0 (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths=["tests"]`, `pythonpath=["src"]`, `filterwarnings=["error"]`) |
| Quick run command | `uv run pytest tests/test_fx_rollup_join.py -x` |
| Full suite command | `uv run pytest` |
| Real-DB harness | `tests/conftest.py` — session-scoped `PostgresContainer("postgres:17-bookworm")`, alembic-to-head, truncate-between-tests |
| HTTP mock | `respx` (mock NBU); existing Mono tests use the same pattern |

> **Note on `filterwarnings=["error"]`:** any unclosed `httpx.AsyncClient` in `NbuFxImporter` will escalate to a hard test failure (same trap documented in `main.py` CR-01). The NBU client must be closed in lifespan teardown or via an `aclose()` the bootstrap awaits.

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Automated Command | File Exists? |
|-----|----------|-----------|-------------------|-------------|
| FX-02 | 12-month backfill populates `fx_rates` NUMERIC(18,8) keyed (rate_date,currency) | integration | `uv run pytest tests/test_fx_importer_nbu.py -x` | ❌ Wave 0 |
| FX-02 | Daily 16:00 Kyiv cron fires correctly across DST | unit | `uv run pytest tests/test_fx_cron_dst.py -x` | ❌ Wave 0 |
| FX-02 | Empty NBU result → bootstrap_done stays false, last_error set, row stays fx_stale | integration | `uv run pytest tests/test_fx_stale_fallback.py -x` | ❌ Wave 0 |
| FX-02/03 | Sunday tx uses Friday's rate; fx_rate_date=Friday; fx_stale=true (sparse fx_rates) | integration | `uv run pytest tests/test_fx_rollup_join.py -x` | ❌ Wave 0 |
| FX-03 | UAH computed on read via LATERAL join; no denormalized column exists | integration + schema | `uv run pytest tests/test_fx_rollup_join.py tests/test_schema_invariants.py -x` | partial (schema test exists) |
| FX-03 | Banker's-rounding kopeck math is exact (property test) | unit | `uv run pytest tests/test_fx_rollup_math.py -x` | ❌ Wave 0 |
| FX-04 | mono_card row: UAH = account-amount × NBU rate, NOT double-converted (property test) | integration | `uv run pytest tests/test_fx_on_card.py -x` | ❌ Wave 0 |
| FX-02 | attributed_day backfill + NOT NULL migration is Kyiv-correct | integration | `uv run pytest tests/test_attributed_day_migration.py -x` | ❌ Wave 0 |
| FX-02 | Lazy auto-add: new currency → tracked row + bootstrap; subsequent read non-null | integration | `uv run pytest tests/test_fx_bootstrap_lazy.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** the single test file for the task under change (`-x`).
- **Per wave merge:** `uv run pytest tests/test_fx_*.py tests/test_attributed_day_migration.py`.
- **Phase gate:** full `uv run pytest` green + `ruff check` + `basedpyright` (strict on `src/`).

### Wave 0 Gaps
- [ ] `tests/test_fx_importer_nbu.py` — covers FX-02 (range fetch, parse, weekend carry-forward shape, empty→[])
- [ ] `tests/test_fx_rollup_join.py` — covers FX-03 (LATERAL fallback; Sunday→Friday on sparse fx_rates)
- [ ] `tests/test_fx_on_card.py` — covers FX-04 (no double-conversion; mono_card label + identical math)
- [ ] `tests/test_fx_rollup_math.py` — property test for Decimal/banker's-rounding (Pitfall 1)
- [ ] `tests/test_fx_stale_fallback.py` — covers D-12/D-13 (no-rate → null + fx_stale, row still appears)
- [ ] `tests/test_fx_bootstrap_lazy.py` — covers D-15 (lazy auto-add)
- [ ] `tests/test_fx_cron_dst.py` — covers D-06 (CronTrigger fire time across DST, via freezegun)
- [ ] `tests/test_attributed_day_migration.py` — covers D-09 (backfill + NOT NULL)
- [ ] NBU fixtures: `tests/fixtures/nbu_usd_range.json`, `nbu_empty.json` (mirror real `exchange_site` shape, incl. `calcdate`)
- [ ] No new framework install — pytest/respx/freezegun/testcontainers all present.

## Security Domain

> `security_enforcement` not set to false → section included. Phase 3 has a narrow security surface (no new auth, no PII in NBU responses, no new endpoint).

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No new auth; NBU is unauthenticated; DEP-02 network-gating unchanged. |
| V3 Session Management | no | No sessions. |
| V4 Access Control | no | Single-user, no per-row ownership. |
| V5 Input Validation | yes | NBU response parsed with explicit `%d.%m.%Y` + `Decimal`; reject/log unknown currency codes (empty `[]` → no rows). Pydantic validates `TransactionOut` fields. |
| V6 Cryptography | no | No new secrets; NBU needs no key (never hand-roll crypto). |
| V7 Error Handling & Logging | yes | NBU responses contain no PII (date+ccy+rate only) — existing structlog redaction is sufficient; do not log full payloads at INFO+ (OPS-04). |
| V9 SSRF / outbound | yes | Outbound host is the fixed constant `bank.gov.ua` — no user-controlled URL. Matches OPS-05 documented egress (Mono + NBU only). |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Float precision loss on money | Tampering (integrity) | `Decimal` + `NUMERIC(18,8)` + string transport; `parse_float=Decimal` on NBU input. |
| Double-conversion FX error | Tampering (integrity) | `amount × NBU_rate(account_currency)` only; property test (FX-04). |
| Unbounded NBU retry storm | DoS (self-inflicted) | `tenacity` capped at 3 attempts; NBU has no rate limit but bound retries anyway. |
| SSRF via NBU base URL | Tampering | Hardcoded constant base URL; no user input in the URL. |
| PII in logs | Information disclosure | NBU payload is non-PII; existing redaction covers it; financial amounts stay at DEBUG (OPS-04). |

## Project Constraints (from CLAUDE.md)

- **No float for money anywhere** — DB, Pydantic, JSON. `Decimal` + `NUMERIC` + string transport. (Hard.)
- **Money in DB:** BIGINT minor units + ISO-4217 alpha; FX rates `NUMERIC(18,8)` (D-04). `Decimal` only at edges; `getcontext().prec = 28`, `ROUND_HALF_EVEN`.
- **NBU is the authoritative UAH FX source** — no openexchangerates/ECB/fixer.
- **`zoneinfo`, never `pytz`** for Europe/Kyiv (Pitfall 14).
- **Single uvicorn worker** — required for the in-process scheduler; Phase 3 adds a job, not a worker.
- **httpx async, never `requests`** — NBU client is `httpx.AsyncClient`.
- **psycopg 3 URL `postgresql+psycopg://`** — not psycopg2.
- **No new container / no Redis / no Celery** — APScheduler in-process only.
- **structlog redaction default-on at INFO+** (OPS-04) — covers NBU incidentally.
- **GSD workflow:** all edits go through a GSD command (this is plan-phase research; no edits made).

## User Constraints (from CONTEXT.md)

> CONTEXT.md exists and is exhaustive (D-01..D-17 all LOCKED). The planner MUST honor these verbatim. Summarized below; the authoritative source is `.planning/phases/03-uah-truth/03-CONTEXT.md`.

### Locked Decisions (D-01 .. D-17)
- **D-01** NBU range endpoint, one-currency-one-call (confirmed: `exchange_site`). Per-day fallback allowed only if range misbehaves.
- **D-02** New `importers/nbu.py` `NbuFxImporter` behind `FxRatesPort` protocol in `base.py`; method `fetch_range(currency, start, end) -> list[FxRateRow]`.
- **D-03** Lazy on-first-tick bootstrap; `maybe_bootstrap_fx(currency)`; threshold ~250 rows / 365 days; `ON CONFLICT (rate_date, currency) DO NOTHING`; no `fx_runs` table.
- **D-04** `fx_rates(rate_date DATE, currency CHAR(3), rate NUMERIC(18,8), fetched_at TIMESTAMPTZ, PK(rate_date,currency))` + index `(currency, rate_date DESC)`. UAH implicit to-currency.
- **D-05** `tracked_fx_currencies(currency PK, first_seen_at, bootstrap_done bool, last_attempted_at, last_error)`; seed USD+EUR in migration.
- **D-06** Second APScheduler job `fx_tick`, `CronTrigger(hour=16, timezone=ZoneInfo("Europe/Kyiv"))`, `max_instances=1, coalesce=True, misfire_grace_time=3600`.
- **D-07** Lifespan `asyncio.create_task(maybe_bootstrap_fx_all_tracked())` AFTER `scheduler.start()`; fire-and-forget; httpx 30s timeout; tenacity 3 attempts.
- **D-08** Failure surface = logs only; do NOT touch `scheduler_state`; do NOT extend `/api/import/status`; `last_attempted_at`/`last_error` on `tracked_fx_currencies`.
- **D-09** `attributed_day` set on insert/upsert at importer boundary = `time.astimezone(Kyiv).date()`; added to upsert frozen-on-first-write set; migration backfills + sets NOT NULL.
- **D-10** `TransactionOut` gains: `uah_amount_minor: int|None`, `fx_rate: str|None`, `fx_rate_date: date|None`, `fx_source: Literal["native_uah","mono_card","nbu"]`, `fx_stale: bool`. All computed on read.
- **D-11** `fx_source`: `native_uah` (currency==UAH), `mono_card` (currency!=UAH AND op-currency != account-currency), `nbu` (else). Math identical for mono_card/nbu; label is audit-only. Defensive fallback to `nbu` if `currencyCode` missing.
- **D-12** No-rate-available → all FX fields null, `fx_stale=true`; row still appears.
- **D-13** `fx_stale = true` iff `fx_rate_date < attributed_day` OR no rate; false when equal OR native_uah. NOT tied to cron health.
- **D-14** Rollup via `LEFT JOIN LATERAL` in `TransactionRepo.list_for_account`; index covers lookup; Decimal math `ROUND_HALF_EVEN`, quantize to 0.01 then ×100 → int.
- **D-15** Lazy auto-add: new currency on insert → upsert `tracked_fx_currencies` + `asyncio.create_task(bootstrap_currency(ccy))` after commit.
- **D-16** Empty NBU result → `bootstrap_done` false, `last_error="no rates published"`, cron retries; no hard 4xx on import.
- **D-17** `fx_tick` iterates `tracked_fx_currencies ORDER BY currency`, fetches today's rate, upserts, updates `last_attempted_at`; re-runs 12-mo range if `bootstrap_done=false`; sequential.

### Claude's Discretion
- Exact NBU URL/shape (RESOLVED in this research: `exchange_site`).
- httpx client reuse (separate NBU instance, no gate, 30s timeout, tenacity 3 attempts).
- `numeric_to_alpha` long-tail extension (PLN=985, GBP=826, CHF=756, etc.).
- structlog redaction (no new patterns — NBU has no PII).
- Alembic 0003 shape (create both tables, seed USD/EUR, backfill attributed_day, SET NOT NULL, no rate seed).
- No new API endpoints.
- `Money` value object optional in rollup; API surface stays int+str.
- Test files (the 7 listed in CONTEXT — mirrored in Wave 0 above).

### Deferred Ideas (OUT OF SCOPE)
`fx_fallback_kind` enum; `/api/fx/rates`; `/api/fx/bootstrap`; eager all-currency fetch; cross-rate (`from`/`to` columns); materialized view; `fx_runs` table; `holidays` package; per-tx `op_currency` exposure; `fx_freshness` global header; NBU 5xx telemetry; extending `/api/import/status`; `scheduler_state` FX entry.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| FX-02 | NBU rates daily (16:00 Kyiv) + 12-month backfill + weekend/holiday fallback | `exchange_site` endpoint verified (range + single-day); carry-forward + LATERAL fallback both confirmed; `CronTrigger(ZoneInfo)` + `tenacity` patterns; `fx_rates` schema (D-04). |
| FX-03 | UAH rollup computed on read, never denormalized | LATERAL join SQL (Approach A `text()`) + index coverage verified; Decimal/banker's-rounding helper; no denormalized column (enforced by `test_schema_invariants`). |
| FX-04 | FX-on-card uses Mono account-currency amount, not double-converted | `currencyCode` confirmed present on every Mono statementItem; `fx_source` detection rule + identical math; property test (`test_fx_on_card.py`). |

## Sources

### Primary (HIGH confidence)
- **Live NBU API** (`curl` 2026-05-30): `bank.gov.ua/NBU_Exchange/exchange_site` (range) and `bank.gov.ua/NBUStatService/v1/statdirectory/exchange` (per-day) — endpoint paths, params, JSON field names, weekend carry-forward, empty-array behavior, PLN/GBP/CHF availability all verified directly.
- **Existing codebase** (read directly): `models.py`, `transaction_repo.py`, `monobank.py`, `base.py`, `currency_map.py`, `scheduler/runner.py`, `main.py`, `api/schemas.py`, `services/import_service.py`, `conftest.py`, `tests/fixtures/statement_two_items.json`, `alembic/versions/0002_phase2_sync.py`, `pyproject.toml`.
- **03-CONTEXT.md** — D-01..D-17 locked decisions.

### Secondary (MEDIUM confidence)
- `.planning/research/PITFALLS.md` — Pitfall 1 (float), 7 (NBU weekend gaps — partially superseded), 8 (multi-hop FX), 14 (timezone), 20 (numeric currency code).
- `.planning/research/STACK.md` — stack versions; NBU `exchangenew` endpoint guidance (superseded by live verification).
- [APScheduler 3.x CronTrigger docs](https://apscheduler.readthedocs.io/en/3.x/) — timezone/coalesce/misfire semantics.
- [SQLAlchemy 2.0 — Select.lateral()](https://docs.sqlalchemy.org/en/20/) — LATERAL Core construct.

### Tertiary (LOW confidence)
- Ukraine DST policy 2026 (A1) — based on training knowledge that the 2024 abolition bill did not take effect; not re-verified live. Does not affect cron correctness (ZoneInfo reflects OS tzdata either way).

## Metadata

**Confidence breakdown:**
- NBU endpoint/shape/weekend/empty behavior: HIGH — live-verified by curl, multiple cases.
- Standard stack: HIGH — all deps read from pinned `pyproject.toml`; zero new packages.
- LATERAL syntax + index coverage: HIGH — matches D-14 literal SQL; repo already uses `text()`.
- Cron/DST: MEDIUM-HIGH — APScheduler behavior cited; DST policy assumption (A1) is the only soft spot, and it doesn't affect correctness.
- tzdata in container (A4): MEDIUM — flagged for plan-time verification.

**Research date:** 2026-05-30
**Valid until:** ~2026-06-29 (30 days) for the stack; NBU endpoint behavior is stable but re-confirm the live shape if the phase is planned later than ~July 2026.
