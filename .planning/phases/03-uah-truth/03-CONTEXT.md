# Phase 3: UAH Truth - Context

**Gathered:** 2026-05-12
**Status:** Ready for planning

<domain>
## Phase Boundary

Every foreign-currency transaction gets an honest UAH rollup computed on read at the NBU rate of the transaction's Europe/Kyiv calendar day, with weekend/holiday fallback to the most recent prior business-day rate. FX-on-card transactions use Mono's already-converted account-currency `amount` (never re-converted via NBU). This phase delivers FX-02, FX-03, and FX-04 from REQUIREMENTS.md.

This phase OWNS: the `fx_rates` table; the `tracked_fx_currencies` table for lazy currency discovery; a new `NbuFxImporter` adapter at `src/finance_bro/importers/nbu.py` behind an `FxRatesPort` protocol; a second APScheduler job (`fx_tick`) firing daily at 16:00 Europe/Kyiv inside the existing in-process `AsyncIOScheduler`; a lifespan-spawned `maybe_bootstrap_fx()` task that 12-month-backfills the NBU range API on first boot; a backfill of `attributed_day` for all existing `transactions` rows + making the column NOT NULL going forward; the Phase 1 `MonobankImporter` is extended (not replaced) to populate `attributed_day` on insert and upsert; the rollup join (`SELECT … FROM transactions LEFT JOIN LATERAL (SELECT rate, rate_date FROM fx_rates WHERE currency = t.currency AND rate_date <= t.attributed_day ORDER BY rate_date DESC LIMIT 1) fx ON true`) lives in `TransactionRepo` and feeds an enriched `TransactionOut`.

This phase does NOT TOUCH: categorization or `rules` (Phase 4 / CAT-*), `transaction_links` for transfers/refunds (Phase 5 / REC-*), the dashboard or frontend (Phase 6 / UI-*), backups or export (Phase 7 / OPS-*). The hard invariants from Phase 1 + 2 are immutable: BIGINT minor units + ISO-4217 alpha currency, composite idempotency on `(account_id, source_tx_id) WHERE NOT is_deleted`, single `RateLimitGate` for Mono (NBU uses no gate — no documented rate limit), single uvicorn worker, in-process scheduler, env-only Mono token, log redaction at INFO+, no float for money anywhere. FX-03's "computed on read, never denormalized" is also immutable — no `uah_amount_minor` column ever lands on `transactions`.

</domain>

<decisions>
## Implementation Decisions

### NBU client & 12-month bootstrap

- **D-01 (endpoint):** Use NBU's **range endpoint** — `https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&date={start}&date={end}&json` (or the period equivalent, whichever returns a date range in one call). One HTTP request per currency for the full 12-month bootstrap, then one request per currency per daily tick. Fewer round-trips than per-day-per-currency or per-day-all-currencies, at the cost of a slightly less battle-tested response shape in the Mono ecosystem. Researcher should confirm the exact range endpoint URL/parameters and JSON shape on NBU's official dev page (https://bank.gov.ua/en/open-data/api-dev) before Plan-stage. **Fallback to per-day endpoint** only if the range endpoint is misbehaving — keep the choice locked to "one currency, one call" semantics.
- **D-02 (module location):** New `src/finance_bro/importers/nbu.py` adapter, mirroring Phase 1's `MonobankImporter` shape. An `FxRatesPort` protocol lives in `src/finance_bro/importers/base.py` alongside `ImporterProtocol` and exposes a single method `fetch_range(currency: str, start: date, end: date) -> list[FxRateRow]`. `NbuFxImporter` is the only adapter in v1. Keeps the importer layer as the single seam for outbound HTTP (with the structlog redaction processor already covering it). Future cross-rate sources (ECB, openexchangerates) slot into the same port.
- **D-03 (bootstrap trigger):** **Lazy on-first-tick**. The 16:00 cron and the lifespan-spawned bootstrap task both call the same `maybe_bootstrap_fx(currency)` helper. The helper checks `fx_rates` row count for that currency over the last 12 months; if it's below a "looks fresh" threshold (concretely: fewer than ~250 business-day rows in the last 365 calendar days), it runs the range fetch. No `fx_runs` audit table — NBU is rate-limit-free and the range call is idempotent because `INSERT INTO fx_rates … ON CONFLICT (rate_date, currency) DO NOTHING` swallows re-fetches. If the bootstrap is killed mid-run, the next tick re-runs it; the conflict clause makes it cheap.
- **D-04 (schema, fx_rates):** `fx_rates(rate_date DATE NOT NULL, currency CHAR(3) NOT NULL, rate NUMERIC(18,8) NOT NULL, fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (rate_date, currency))`. UAH is the implicit to-currency in v1 — no `to_currency` column. ROADMAP SC#1's literal `(rate_date, from_currency, to_currency)` is captured semantically (the absent column is conventionally UAH). Index: the PK is enough; add an explicit `INDEX (currency, rate_date DESC)` for the LATERAL join's covering lookup. Reverse-cross rates (USD→EUR) and `to_currency`-aware schemas land in v2 if anyone ever asks; FX-03 in v1 is exclusively UAH-rollup.
- **D-05 (schema, tracked_fx_currencies):** `tracked_fx_currencies(currency CHAR(3) PRIMARY KEY, first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), bootstrap_done BOOL NOT NULL DEFAULT false, last_attempted_at TIMESTAMPTZ NULL, last_error TEXT NULL)`. Seeded in the same migration with `INSERT … VALUES ('USD', now(), false), ('EUR', now(), false)`. The importer's transaction-insert path checks this table; new currencies trigger an out-of-band `asyncio.create_task(bootstrap_currency(ccy))` after the transaction commits. The 16:00 cron iterates `SELECT currency FROM tracked_fx_currencies` and fetches each. `bootstrap_done` is set TRUE when the 12-month range fetch completes successfully for that currency; FALSE during/before; LATER cron ticks re-run if FALSE (so a half-bootstrapped currency self-heals).

### FX cron lifecycle

- **D-06 (cron registration):** A second APScheduler job lives next to the Phase 2 `tick`:
  ```python
  scheduler.add_job(
      fx_tick,
      CronTrigger(hour=16, minute=0, timezone=ZoneInfo("Europe/Kyiv")),
      id="fx_tick",
      max_instances=1,
      coalesce=True,
      misfire_grace_time=3600,  # if the container was down at 16:00, run within the next hour
  )
  ```
  Independent of the 10s Mono tick — different cadence, different failure model, different external dependency. DST handled by APScheduler's `ZoneInfo` integration. `coalesce=True` ensures only one catch-up tick fires if the container missed the window. The job is registered inside `lifespan()` right after the existing `tick` registration; `scheduler.start()` covers both.
- **D-07 (first-boot bootstrap):** Inside FastAPI `lifespan()`, AFTER `init_engine()` and AFTER `scheduler.start()`, spawn `asyncio.create_task(maybe_bootstrap_fx_all_tracked())` as a fire-and-forget task. The helper iterates `tracked_fx_currencies` and calls `maybe_bootstrap_fx(currency)` for each in sequence. Doesn't block lifespan startup (so health check / API are responsive immediately); doesn't wait for 16:00 (so a fresh install gets correct rollups within ~10–30s of boot). If a bootstrap call fails, log + set `last_error`, leave `bootstrap_done=false`; next 16:00 tick retries. Use `httpx.AsyncClient` with timeouts (e.g., 30s per call) and a `tenacity` retry decorator with exponential backoff capped at 3 attempts per currency per boot — NBU 5xx are rare but observed; don't lose the bootstrap to a transient blip.
- **D-08 (failure surface):** Logs only, via the existing `structlog` JSON pipeline. **Do NOT touch `scheduler_state`** — that's Phase 2's Mono-auth-failed surface and the semantics don't map (NBU has no token to revoke). Do NOT extend `/api/import/status` in v1 either; rollups continue working via fallback (`MAX(rate_date) WHERE rate_date <= attributed_day`), and rows for which today's rate is missing carry `fx_stale=true` in the API response — that's the user-visible signal. The mechanism is self-healing: the next 16:00 tick retries every tracked currency. Add `last_attempted_at` / `last_error` to `tracked_fx_currencies` so an operator can `psql` for the current state; no JSON surface in v1.
- **D-09 (attributed_day fill):** **On insert/upsert at the importer boundary**, computed as `tx.time.astimezone(ZoneInfo("Europe/Kyiv")).date()`. The Phase 1 `MonobankImporter` constructs `CanonicalTransaction` — extend it (and `CanonicalAccount`-equivalent flow for transactions) to set `attributed_day`. The Phase 2 hold→cleared upsert (D-10 of Phase 2) currently mutates only `hold, amount_minor, raw_payload` — Phase 3 **adds `attributed_day` to the upsert's frozen-on-first-write set** (so the cleared `time` doesn't shift `attributed_day` even if a Mono quirk moves the timestamp). The Alembic migration runs a one-shot `UPDATE transactions SET attributed_day = (time AT TIME ZONE 'Europe/Kyiv')::date WHERE attributed_day IS NULL`, then `ALTER COLUMN attributed_day SET NOT NULL`. Drop the Phase 1 "nullable" guard — Phase 3 makes it required.

### TransactionOut shape

- **D-10 (new fields, full quartet+source):** `TransactionOut` gains:
  - `uah_amount_minor: int | None` — UAH equivalent in kopecks (BIGINT). `None` when no rate is available for the currency on or before `attributed_day`.
  - `fx_rate: str | None` — the NBU mid-rate used, serialized as a Decimal-as-string ("28.34250000"). String transport prevents JS-side float drift (CLAUDE.md §"Money / Decimal Handling"). `None` when no rate available.
  - `fx_rate_date: date | None` — the date of the rate row that was applied (ISO date string in JSON). `None` when no rate available. SC#5 ("the API response makes the rate date and source visible") is satisfied by this field.
  - `fx_source: Literal["native_uah", "mono_card", "nbu"]` — see D-11 for the semantics. Always populated (never `None`).
  - `fx_stale: bool` — see D-13. Always populated.
  All five fields are computed on read (FX-03). No DB column changes on `transactions` beyond the existing nullable forward-looking columns from Phase 1.
- **D-11 (fx_source semantics):** Three-way classification at row-construction time:
  - `native_uah`: `transactions.currency == 'UAH'`. No rollup math needed. The API still populates the fields for symmetry: `uah_amount_minor = amount_minor`, `fx_rate = "1.00000000"`, `fx_rate_date = attributed_day`, `fx_stale = false`.
  - `mono_card`: `transactions.currency != 'UAH'` AND Mono already converted at the card level — detected by comparing `transactions.currency` (= account currency, what Mono settled in) against `raw_payload->>'currencyCode'` (numeric ISO, then mapped via `currency_map.numeric_to_alpha`). If they differ, Mono performed a bank-level FX conversion (Pitfall 8 case b or c). Math: `transactions.amount_minor × NBU_rate(transactions.currency, attributed_day)` — never triangulate via `operationAmount`. The label is purely an audit trail so the API consumer can see "Mono's spread is baked in; this is not pure NBU".
  - `nbu`: `transactions.currency != 'UAH'` AND operation currency == account currency (no Mono-level FX involved; the user paid in the foreign currency that matches the foreign-currency account). Pure NBU rollup. Math identical to `mono_card`: `amount_minor × NBU_rate(currency, attributed_day)`.
  - **Computation is identical for `mono_card` and `nbu`** — both use `amount_minor × NBU_rate(transactions.currency, attributed_day)` consistent with FX-04 and Pitfall 8. The distinction is exposure-only, for the Phase 6 detail drawer and audit clarity. Open question: if `raw_payload` doesn't carry `currencyCode` for some Mono row shapes (jars? FOPs?), fall back to `fx_source = "nbu"` and note the absence; not a v1 blocker because v1 only polls cards (Phase 2 D-01).
- **D-12 (no-rate-available fallback):** When the LATERAL lookup returns no row (cold-boot pre-bootstrap, fringe-currency NBU doesn't publish, or `tracked_fx_currencies.bootstrap_done = false` AND first range fetch failed), the API returns:
  - `uah_amount_minor = null`, `fx_rate = null`, `fx_rate_date = null`, `fx_source = "nbu"` (or `"mono_card"` per D-11), `fx_stale = true`.
  - The transaction row still appears in `GET /api/transactions` — never block the response. Frontend Phase 6 renders "—" or "rate pending".
- **D-13 (fx_stale definition):** `fx_stale = true` iff `fx_rate_date < attributed_day` (weekend/holiday fallback was used) OR no rate was found at all (D-12 case). `fx_stale = false` when `fx_rate_date == attributed_day` (rate for the transaction's own Kyiv day was applied) OR `fx_source == "native_uah"`. **Do NOT** also light up `fx_stale` for "today's cron failed" — that conflates two independent failure modes and the frontend can't distinguish them; operators check logs. **Do NOT** add an `fx_fallback_kind` enum in v1 — `fx_rate_date` already tells the story.
- **D-14 (computation, LATERAL JOIN):** The rollup runs in SQL via a Postgres `LEFT JOIN LATERAL` subquery in `TransactionRepo.list_for_account` (and any future variants):
  ```sql
  SELECT t.*, fx.rate AS fx_rate, fx.rate_date AS fx_rate_date
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
  ```
  Single round-trip. Index `fx_rates (currency, rate_date DESC)` covers the lookup. `uah_amount_minor` is computed in Python from the joined row using `Decimal(amount_minor) * Decimal(fx_rate)` with banker's rounding (`ROUND_HALF_EVEN`) to the nearest kopeck — `getcontext().prec = 28` already enforced project-wide per CLAUDE.md. UAH amount stays in minor units (kopecks) at the API boundary; the multiplication happens in major-unit Decimal space then snaps back: `quantize(Decimal("0.01"), ROUND_HALF_EVEN)` on the major-unit result, then `*100` and `int()`. For `native_uah`, skip the multiplication and pass `amount_minor` through directly.

### Currency scope policy

- **D-15 (lazy auto-add via tracked_fx_currencies):** USD + EUR seeded in the migration (D-05). When the Mono importer inserts a transaction with `currency NOT IN (SELECT currency FROM tracked_fx_currencies)`, an after-commit hook (or post-insert side-effect in `ImportService.run_one_card`) `INSERT … ON CONFLICT DO NOTHING`s the new currency, then `asyncio.create_task(bootstrap_currency(ccy))` fires a one-shot 12-month range fetch. The importer's main path doesn't await this — the transaction is already persisted with `fx_stale=true` from the next read, and the next read after bootstrap completes resolves cleanly. Range fetches for new currencies happen out-of-band so import latency isn't affected.
- **D-16 (NBU-unsupported currencies):** If NBU's range endpoint returns an empty result for a tracked currency, `tracked_fx_currencies.bootstrap_done` stays `false`, `last_error` records "no rates published", and the 16:00 cron will retry on every tick (cheap — one HTTP call per currency per day). Transactions in that currency stay `fx_stale=true` indefinitely until either NBU adds the rate or the user manually purges the row. No hard 4xx on import — ING-03 requires us to persist the verbatim Mono row regardless.
- **D-17 (cron iteration):** `fx_tick` iterates `SELECT currency FROM tracked_fx_currencies ORDER BY currency`, fetches today's rate per currency via the range endpoint (`date={today}&date={today}`), upserts into `fx_rates`, updates `last_attempted_at` per currency. For currencies where `bootstrap_done = false`, the cron also re-runs the 12-month range fetch (idempotent). Sequential, not parallel — NBU is fine with serial requests and we have no rate-limit budget to manage on their side; if it ever matters, switch to `asyncio.gather` later.

### Claude's Discretion

These framings the user did not select; Claude exercises judgment within the framing already established by Phase 1, Phase 2, ROADMAP.md, REQUIREMENTS.md, and CLAUDE.md:

- **NBU range endpoint URL & response shape** — Researcher confirms the exact URL/params in Plan-stage. The CLAUDE.md TL;DR and STACK.md both cite the `exchangenew?json` per-day endpoint; the per-currency range endpoint mentioned in the NBU PDF (https://bank.gov.ua/admin_uploads/article/Instr_API_KURS_VAL_Full_eng.pdf) is the natural fit for one-call-per-currency. Fallback: if the range endpoint behaves weirdly, fall back to per-currency-per-day calls in a loop — bootstrap goes from ~2 HTTP calls to ~500. NBU has no documented rate limit, so even the worst case is tolerable.
- **HTTP client reuse** — Use the existing `httpx.AsyncClient` pattern from `MonobankImporter`. New client instance for NBU (different base URL, no auth header, no token redaction concern); could share via `lifespan`-managed factory if convenient, but separate instances are fine. Timeouts: 30s connect+read per call; `tenacity` retry with exponential backoff (3 attempts, base 2s) for transient 5xx and network errors. No `RateLimitGate` for NBU.
- **`numeric_to_alpha` mapping for fx_source detection** — The existing `src/finance_bro/importers/currency_map.py` already maps 980→UAH, 840→USD, 978→EUR. Phase 3 extends it with the long tail (PLN=985, GBP=826, CHF=756, etc.) as needed; the lazy auto-add per D-15 may force this to be more complete on first observation. If a Mono numeric code isn't in the map, the importer falls back to the numeric code as the alpha (e.g., `"985"`) and logs a warning — the resulting transaction is unrollable but visible.
- **structlog redaction** — NBU responses contain no PII (just date+currency+rate); no new redaction patterns needed. The `RateLimitGate` redaction logic doesn't apply.
- **Alembic migration shape (0003_fx_truth.py)** — One revision that:
  1. Creates `fx_rates` per D-04 with PK and the covering index.
  2. Creates `tracked_fx_currencies` per D-05, seeded with USD + EUR.
  3. Backfills `attributed_day` per D-09: `UPDATE transactions SET attributed_day = (time AT TIME ZONE 'Europe/Kyiv')::date WHERE attributed_day IS NULL`.
  4. `ALTER TABLE transactions ALTER COLUMN attributed_day SET NOT NULL`.
  5. No data seeded into `fx_rates` — the lifespan bootstrap fills it.
- **API endpoint additions** — None in Phase 3. No `/api/fx/rates` listing endpoint; no `/api/fx/bootstrap` manual trigger. The lazy/scheduler model is the only path. Phase 6 may add a status surface for FX freshness if the dashboard needs it (deferred).
- **`Money` value object** — Phase 1 introduced `Money(Decimal, currency)` at the application edge. Phase 3's UAH rollup math (`Decimal(amount_minor) / 100 * Decimal(fx_rate)`) lives inside `TransactionRepo` or a small helper module `src/finance_bro/services/fx_rollup.py`. The `Money` wrapper is optional here — the API surface is `int` minor units + `str` Decimal rate, not a `Money` instance.
- **Testing** — Phase 1+2's testcontainers + httpx-mock harness extends naturally:
  - `tests/test_fx_importer_nbu.py` — fakes NBU's range endpoint; asserts `fx_rates` rows for the expected dates; asserts weekend-gap behavior (no row inserted for Sunday).
  - `tests/test_fx_rollup_join.py` — fixture: a USD transaction on 2026-05-10 (Sunday) + NBU rate for 2026-05-08 (Friday); assert the rollup uses Friday's rate, `fx_rate_date = 2026-05-08`, `fx_stale = true`.
  - `tests/test_fx_on_card.py` — fixture: EUR-account transaction with `raw_payload.currencyCode = 840` (USD merchant); assert `fx_source = "mono_card"`, `uah_amount_minor` = EUR amount × NBU EUR/UAH rate (Pitfall 8 case c).
  - `tests/test_fx_bootstrap_lazy.py` — fixture: insert a CHF transaction; assert `tracked_fx_currencies` gains a CHF row; assert (mocked) bootstrap fetches CHF range; assert subsequent reads return non-null rollup.
  - `tests/test_fx_stale_fallback.py` — fixture: USD transaction with no `fx_rates` rows; assert `uah_amount_minor = null`, `fx_stale = true`, row still appears in feed.
  - `tests/test_fx_cron_dst.py` — assert cron fires at 16:00 Europe/Kyiv across the DST boundary (last Sunday of October).
  - `tests/test_attributed_day_migration.py` — fixture: Phase 2 row inserted with `attributed_day = NULL`; run migration upgrade; assert row's `attributed_day` is correctly Kyiv-derived.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents (researcher, planner, executor) MUST read these before planning or implementing.**

### Project framing
- `.planning/PROJECT.md` — Core Value (visibility, zero manual upkeep, on hardware the user owns), constraints (Python+JS, Postgres in compose, single-user, network-gated, no third-party cloud), in-scope vs out-of-scope. Confirms FX-on-card decision belongs in-scope, not deferred.
- `.planning/REQUIREMENTS.md` — v1 REQ-IDs and per-phase mapping. Phase 3 owns: **FX-02** (NBU rates + 12-month backfill + weekend fallback), **FX-03** (UAH rollup computed on read, never denormalized), **FX-04** (FX-on-card uses Mono's account-currency amount).
- `.planning/ROADMAP.md` — Phase 3 section: goal, success criteria (5 SCs), requirements, notes/risks (**Pitfall 7** weekend gaps, **Pitfall 8** multi-hop FX).
- `.planning/STATE.md` — accumulated decisions and open questions; current position is Phase 3 ready-to-plan.
- `CLAUDE.md` — full stack table, "Money / Decimal Handling" section (BIGINT minor units, NUMERIC(18,8) FX, Decimal-as-string transport), "FX Rate Source: NBU" section, "What NOT to Use" (no float for money, no py-moneyed, no requests).

### Phase 1 + Phase 2 (Phase 3's foundation — must not be broken)
- `.planning/phases/01-first-real-transaction/01-CONTEXT.md` — D-10 (TransactionOut shape — Phase 3 adds five fields additively), schema groundwork (forward-looking `attributed_day DATE NULL` column lands here, Phase 3 backfills and tightens it), `Money` value object pattern, numeric→alpha currency map at importer boundary.
- `.planning/phases/02-reliable-sync/02-CONTEXT.md` — D-04 (APScheduler `AsyncIOScheduler` in `lifespan`), D-10 (hold→cleared upsert frozen-fields invariant — Phase 3 adds `attributed_day` to the frozen-on-first-write set), D-14 (status surface — Phase 3 does NOT extend), `import_runs` audit pattern (Phase 3 deliberately does not mirror it for FX).
- `src/finance_bro/db/models.py` — current schema. Phase 3 adds: `fx_rates`, `tracked_fx_currencies`; backfills + tightens `transactions.attributed_day` to NOT NULL.
- `src/finance_bro/db/transaction_repo.py` — `list_for_account` is replaced with the LATERAL-join query per D-14. The hold→cleared upsert path is extended to include `attributed_day` in the INSERT column list (frozen on conflict).
- `src/finance_bro/db/account_repo.py` — unchanged for Phase 3.
- `src/finance_bro/importers/base.py` — Phase 3 adds `FxRatesPort` protocol next to the existing `ImporterProtocol`.
- `src/finance_bro/importers/monobank.py` — extended to populate `attributed_day = time.astimezone(ZoneInfo("Europe/Kyiv")).date()` when constructing CanonicalTransaction; also detects "Mono converted at card level" via `raw_payload.currencyCode` vs account currency to inform fx_source (only metadata exposure; no math change at the importer).
- `src/finance_bro/importers/currency_map.py` — extended for the long tail of currencies (PLN, GBP, CHF, etc.) — see Claude's Discretion.
- `src/finance_bro/importers/rate_limit.py` — unchanged; NBU calls do NOT go through this gate.
- `src/finance_bro/scheduler/runner.py` — Phase 3 registers a second job (`fx_tick`) in lifespan startup; existing Mono `tick` is untouched.
- `src/finance_bro/main.py` — `lifespan` adds `asyncio.create_task(maybe_bootstrap_fx_all_tracked())` after `scheduler.start()`. The startup task is fire-and-forget.
- `src/finance_bro/api/schemas.py` — `TransactionOut` gains the five FX fields per D-10; no other shape changes.
- `src/finance_bro/api/routes_transactions.py` — calls the new repo method; serialization of the new fields is via Pydantic config.
- `alembic/versions/0002_phase2_sync.py` — Phase 2's revision. Phase 3 adds `0003_fx_truth.py` on top (see Claude's Discretion).

### Research (HIGH confidence, dated 2026-05-10)
- `.planning/research/STACK.md` — APScheduler 3.11.2 (in-process AsyncIOScheduler), httpx 0.28.1, psycopg 3.3.4, Postgres 17, SQLAlchemy 2.0.49 + Alembic 1.18.4. Phase 3 introduces NO new top-level dependency — `tenacity` may already be installed; if not, add it for the NBU retry decorator.
- `.planning/research/ARCHITECTURE.md` — modular monolith; `fx_rates` is mentioned as a canonical entity name (use it verbatim, not `exchange_rates` or `nbu_rates`).
- `.planning/research/FEATURES.md` — Mono `statementItem.currencyCode` (operation), `amount` (account), `operationAmount` (operation). NBU `exchangenew?json` shape.
- `.planning/research/PITFALLS.md` — **Pitfall 1** (no floats for money; banker's rounding for aggregation), **Pitfall 2** (off-by-100 on minor units; named conversion helpers), **Pitfall 7** (NBU weekend gaps; fallback to prior business day; never re-convert Mono-converted amounts), **Pitfall 8** (multi-hop FX; use account-currency × NBU on day; document the three FX scenarios in code comments at the rollup module).

### External (no auth required, fetch on demand)
- NBU Developer API directory: https://bank.gov.ua/en/open-data/api-dev — endpoint catalog. Researcher confirms the exact range endpoint URL.
- NBU range-fetch PDF (English): https://bank.gov.ua/admin_uploads/article/Instr_API_KURS_VAL_Full_eng.pdf — `exchangenew?json&date=YYYYMMDD&valcode=USD` and period variant.
- floatrates.com mirror of NBU rates (weekday-only, confirms gap pattern): https://www.floatrates.com/source/nbu/
- kastaneda/nbu_rates historical archive (showing the gaps): https://github.com/kastaneda/nbu_rates
- Double-conversion explainer (Payoneer) for Pitfall 8 framing: https://www.payoneer.com/resources/what-does-double-conversion-mean/
- Python `zoneinfo` docs (Europe/Kyiv handling, DST): https://docs.python.org/3/library/zoneinfo.html
- APScheduler CronTrigger with timezone: https://apscheduler.readthedocs.io/en/3.x/userguide.html#choosing-the-right-scheduler-job-store-s-executor-s-and-trigger-s

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`AsyncIOScheduler` in `lifespan`** (`src/finance_bro/main.py` / `src/finance_bro/scheduler/runner.py`) — Phase 3 adds one more `scheduler.add_job(...)` call right next to the existing Mono `tick` registration. No new scheduler instance, no new container.
- **`MonobankImporter.discover_accounts` / `fetch_statement`** (`src/finance_bro/importers/monobank.py`) — extended in two small ways: (a) populate `attributed_day` when constructing `CanonicalTransaction`, (b) expose `raw_payload.currencyCode` for the `mono_card` vs `nbu` fx_source distinction. No structural change to the importer port.
- **`TransactionRepo.list_for_account`** (`src/finance_bro/db/transaction_repo.py`) — replaced with the LATERAL-join query. Backward-compatible at the route level (the new columns flow through `TransactionOut`). Existing callers (`routes_transactions.py`) see the same return shape semantically.
- **`TransactionRepo.insert_many` upsert path** — Phase 2's `ON CONFLICT (account_id, source_tx_id) WHERE NOT is_deleted DO UPDATE SET hold=…, amount_minor=…, raw_payload=…` is extended to include `attributed_day` in the INSERT column list and EXPLICITLY OMIT it from the SET clause — preserves the Phase 2 frozen-fields invariant (D-10) for date semantics.
- **`currency_map.py`** (`src/finance_bro/importers/currency_map.py`) — Phase 1's numeric→alpha map. Phase 3 extends with the long tail (PLN/GBP/CHF/etc.) as observed currencies hit the lazy auto-add path.
- **`structlog` redaction** (`src/finance_bro/core/logging.py`) — covers NBU responses incidentally; no new redaction patterns needed (NBU response has no PII).
- **testcontainers + httpx-mock harness** (`tests/conftest.py`) — Phase 3 reuses unchanged. Add an NBU range-endpoint fake fixture; add an "FX-stale through migration" fixture for the attributed_day backfill.

### Established Patterns
- **Importer port + adapter** — `ImporterProtocol` for Mono; Phase 3 introduces sibling `FxRatesPort` for NBU. Same testing pattern (swap a fake adapter).
- **Repository pattern** — Phase 3 adds two new repos: `FxRateRepo` (insert/lookup) and `TrackedFxCurrencyRepo` (CRUD + iterate). Both follow the existing `AsyncSession` constructor / `select`/`insert`/`update` shape with no SQLA leakage outside `db/`.
- **`Money(Decimal, currency)` value object at the application edge** — unchanged. Phase 3's rollup math constructs `Decimal` from `int` + `str` at the repo boundary; the API surface is still `int` (minor units) + `str` (rate).
- **Lazy/lifespan-spawned background tasks** — Phase 1 was synchronous; Phase 2 introduced fire-and-forget via APScheduler. Phase 3 introduces lifespan-spawned `asyncio.create_task(...)` for the bootstrap (different from APScheduler — runs once on startup, not on a cadence).
- **Single FastAPI worker (`--workers 1`)** — REQUIRED for the in-process scheduler. Phase 3 doesn't change this; the FX cron lives in the same single-worker process.

### Integration Points
- **`lifespan` extension** — Phase 3 adds two lines: `scheduler.add_job(fx_tick, ...)` and `asyncio.create_task(maybe_bootstrap_fx_all_tracked())`. Both AFTER `scheduler.start()`. Shutdown is automatic (APScheduler handles its own jobs; the bootstrap task is cancelled by lifespan shutdown).
- **`fx_rates` × `transactions` join** — the LATERAL subquery from D-14. Used by `TransactionRepo.list_for_account` and any other read path (Phase 6 will reuse).
- **`tracked_fx_currencies` ↔ importer** — Mono importer's INSERT path triggers an after-commit check (or a post-insert side-effect in `ImportService`); a new currency adds a row + fires the lazy bootstrap.
- **No FK from `transactions` to `fx_rates`** — the rollup is read-time only. Adding an FK would be wrong (Pitfall 7 wants us to fall back to prior dates).
- **No new API endpoint in Phase 3** — `GET /api/transactions` stays the same URL, gains five new fields in the response body. `POST /api/import` unchanged. No `/api/fx/*` routes in v1.

</code_context>

<specifics>
## Specific Ideas

- **NBU range endpoint over per-day-per-currency.** User specifically chose the less-trodden range endpoint to keep the call shape one-currency-one-call. Researcher must confirm the URL + JSON shape on the NBU dev page before coding; if the range endpoint misbehaves or returns a shape that's hard to ingest, fall back to per-day-per-currency in a loop (semantically equivalent — same SQL upserts; ~500 HTTP calls instead of ~2 on bootstrap, acceptable because NBU has no rate limit).
- **fx_source = "mono_card" exists for audit, not computation.** The math for `mono_card` and `nbu` is identical: `amount_minor × NBU_rate(transactions.currency, attributed_day)`. The two labels exist purely so the Phase 6 detail drawer can show "this row's UAH equivalent traversed Mono's bank-level FX (their spread is included)" vs "this is pure NBU mid-rate". The detection rule is `raw_payload->>'currencyCode' (numeric) → numeric_to_alpha != transactions.currency`. If `raw_payload` lacks `currencyCode` (shouldn't happen for cards but defensive), fall back to `nbu`.
- **`fx_stale` is solely about rate-date mismatch.** A successful 16:00 cron + a Friday rate applied to a Saturday transaction is `fx_stale=true`; a weekday transaction with its own day's rate is `fx_stale=false`. A failed cron does NOT set `fx_stale` globally — only the absence of a usable rate does. This keeps the semantics clean and testable.
- **`attributed_day` becomes NOT NULL in migration 0003.** No future phase needs the nullable. The Phase 2 importer left it NULL; Phase 3's migration backfills (UPDATE … WHERE attributed_day IS NULL using `time AT TIME ZONE 'Europe/Kyiv'`) and then ALTERs to NOT NULL. After this migration, every `transactions` row has a Kyiv calendar date and the LATERAL join never sees a NULL on the left side.
- **No `fx_runs` audit table.** Symmetry with Phase 2's `import_runs` was considered and explicitly rejected — NBU has no rate limit, no resumability problem (range fetch is one HTTP call), no mid-run kill semantics worth modeling. `tracked_fx_currencies.bootstrap_done` + `last_attempted_at` + `last_error` is the audit trail; logs are the rest.
- **Banker's rounding for the rollup.** `Decimal` math with `getcontext().prec = 28` and `ROUND_HALF_EVEN` per CLAUDE.md / Pitfall 1. Final quantize to `Decimal("0.01")` (kopeck precision) before `*100` + `int()` for the BIGINT.
- **Open questions to resolve empirically in Phase 3:**
  1. NBU range endpoint exact URL/parameters and response shape — Researcher confirms via the NBU dev PDF and a real curl before Plan-stage.
  2. NBU response when given a non-business-day date range — is it `[]`, `{"exchangeRate": []}`, or a 4xx? Affects D-16 handling.
  3. Whether NBU publishes a rate for less-common currencies (PLN, GBP, CHF) every business day — affects how aggressive `last_attempted_at` retry should be.
  4. Whether `raw_payload->>'currencyCode'` is present on every Mono `statementItem` — defensive fallback exists, but knowing is better than guessing.

</specifics>

<deferred>
## Deferred Ideas

- **`fx_fallback_kind` enum on `TransactionOut`** — `weekend` | `holiday` | `fetch_failure` | `no_rate`. Strictly more information than `fx_stale + fx_rate_date`; the Phase 6 detail drawer might want it. Deferred to v1.5 if the Phase 6 UX demands it.
- **`/api/fx/rates` listing endpoint** — query rates by currency + date range. No Phase 3 caller; Phase 6 might want it for a settings panel that shows "rates fetched through 2026-05-10". Deferred until a UI surface needs it.
- **`/api/fx/bootstrap` manual trigger** — re-run the 12-month range fetch on demand. The lazy + scheduled path is enough; deferred.
- **Eager fetch of all NBU-published currencies daily** — explicitly rejected in favor of lazy auto-add. Revisit only if a use-case appears (e.g., user wants to see UAH-equivalent for a currency before ever transacting in it).
- **Cross-rate support (`from_currency`, `to_currency` columns)** — USD↔EUR direct cross-rate, not via UAH. Out of scope for v1 (Core Value is "where my money goes in UAH"). The `fx_rates` schema can be extended later.
- **Materialized view for the rollup join** — denormalized, fast reads. Violates FX-03 "computed on read, never denormalized" in spirit; deferred to v2 if performance becomes painful (unlikely at single-user scale).
- **`fx_runs` audit table mirroring `import_runs`** — explicitly rejected per D-03; revisit only if NBU starts rate-limiting or returns partial-range failures that need resumption modeling.
- **Holidays library (`holidays` PyPI package, Ukraine)** — for "is today an expected NBU business day?". Phase 3 doesn't need this — empty NBU response IS the holiday signal. Defer to v1.5 if a UI surface wants to distinguish "weekend" vs "holiday" vs "fetch failure".
- **Per-tx `op_currency` + `op_amount_minor` exposure on `TransactionOut`** — surface Mono's `operationAmount` and `currencyCode` distinctly. Useful for "what did this cost in USD" displays. Defer to Phase 6 if the detail drawer wants it; `raw_payload` already carries it.
- **API-level `fx_freshness` global header / status** — "are FX rates up-to-date?" as a single GET. Operators can `psql` in v1; deferred to Phase 6 if needed.
- **NBU 5xx retry-budget telemetry** — track success/failure rates on `tracked_fx_currencies.last_error`. v1 leaves the field as last-error only.
- **Extending `/api/import/status` with `fx:` block** — considered and rejected per D-08. Different subsystem, different failure model. Deferred unless v1.5 surfaces an operator complaint.
- **`scheduler_state` entry for FX failures** — considered and rejected per D-08. Phase 2's auth-failed semantics don't generalize.

### Reviewed Todos (not folded)

None — Phase 3's scope was clear from the roadmap and the SC list; no STATE.md TODOs surfaced in this discussion.

</deferred>

---

*Phase: 03-uah-truth*
*Context gathered: 2026-05-12*
