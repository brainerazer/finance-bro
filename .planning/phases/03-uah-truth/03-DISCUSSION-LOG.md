# Phase 3: UAH Truth - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-12
**Phase:** 03-uah-truth
**Areas discussed:** NBU client & 12mo bootstrap, FX cron lifecycle, TransactionOut shape, Currency scope policy

---

## Gray-area selection

| Option | Description | Selected |
|--------|-------------|----------|
| NBU client & 12mo bootstrap | Endpoint shape, where the client lives, 12-month backfill orchestration, fx_rates schema | ✓ |
| FX cron lifecycle | Daily fetch wiring into AsyncIOScheduler, first-boot bootstrap path, failure surface, attributed_day fill | ✓ |
| TransactionOut shape | New FX fields, fx_source semantics, fallback behavior, fx_stale definition, SQL JOIN vs Python rollup | ✓ |
| Currency scope policy | USD/EUR only vs eager vs lazy; lazy mechanism; NBU-unsupported currency handling | ✓ |

**User's choice:** All four areas discussed.

---

## NBU client & 12mo bootstrap

### Endpoint shape

| Option | Description | Selected |
|--------|-------------|----------|
| Per-day, all currencies | GET exchangenew?json&date=YYYYMMDD; ~250 calls on bootstrap | |
| Per-currency, per-day | GET exchangenew?json&date=YYYYMMDD&valcode=USD; ~500 calls on bootstrap | |
| Range endpoint | One call per currency over the year; less battle-tested in the Mono ecosystem | ✓ |

**User's choice:** Range endpoint.
**Notes:** Chosen despite "less battle-tested" — research must confirm exact URL/shape on NBU dev page before Plan-stage. Fallback to per-currency-per-day is fine if range behaves weirdly.

### Module location

| Option | Description | Selected |
|--------|-------------|----------|
| New importers/nbu.py | Mirror MonobankImporter pattern; FxRatesPort protocol; future Wise/Privatbank slot in | ✓ |
| services/fx_service.py with inline httpx | Skip the port abstraction | |
| core/nbu_client.py utility | Treat NBU as plumbing rather than an importer | |

**User's choice:** New importers/nbu.py (recommended).

### Bootstrap orchestration

| Option | Description | Selected |
|--------|-------------|----------|
| Lazy on-first-tick, no audit table | Cron tick checks row count; runs range fetch sync; no fx_runs table | ✓ |
| Lifespan startup task | Background asyncio.task on container start | |
| fx_runs table mirroring import_runs | Per-chunk persisted rows like Mono backfill | |

**User's choice:** Lazy on-first-tick, no audit table (recommended).
**Notes:** ON CONFLICT DO NOTHING makes range fetch idempotent. No new audit table.

### Schema for fx_rates

| Option | Description | Selected |
|--------|-------------|----------|
| (rate_date, currency) PK, UAH implicit | UAH always the to-currency in v1; smaller index, simpler queries | ✓ |
| (rate_date, from_ccy, to_ccy) PK, ROADMAP literal | Future-proof for cross-rates; always 'UAH' in v1 | |
| Add nbu_response_hash for re-fetch invariance | Diagnostic-only audit column | |

**User's choice:** (rate_date, currency) PK, UAH implicit (recommended).

---

## FX cron lifecycle

### Cron registration

| Option | Description | Selected |
|--------|-------------|----------|
| Second job: cron 16:00 Europe/Kyiv | CronTrigger with timezone; max_instances=1, coalesce=True | ✓ |
| Hourly tick that checks "have we fetched today?" | More resilient to NBU 16:00 outage; adds DB chatter | |
| Piggyback on the 10s mono tick | Conflates two subsystems | |

**User's choice:** Second job: cron 16:00 Europe/Kyiv (recommended).

### First-boot bootstrap timing

| Option | Description | Selected |
|--------|-------------|----------|
| Lifespan startup, fire-and-forget asyncio task | Doesn't block startup; runs in background | ✓ |
| Synchronous in lifespan startup | Refuses to serve until FX is loaded; crash-loop on NBU failure | |
| Wait for the 16:00 cron tick | Fresh-install pre-16:00 sees fx_stale=true everywhere | |

**User's choice:** Lifespan startup, fire-and-forget asyncio task (recommended).

### Failure surface

| Option | Description | Selected |
|--------|-------------|----------|
| Logs only + per-tx fx_stale percolation | Self-healing; rollups fall back; per-row fx_stale=true | ✓ |
| Add to existing /api/import/status | Reshape ImportStatusOut; more observable | |
| New scheduler_state entry for FX | Heavier coupling; Mono auth-failed semantics don't map | |

**User's choice:** Logs only + per-tx fx_stale percolation (recommended).

### attributed_day population

| Option | Description | Selected |
|--------|-------------|----------|
| On insert/upsert + Alembic backfill | Compute at importer boundary; backfill existing rows; make NOT NULL | ✓ |
| Compute on read in the SQL join | Don't store; derive inline | |
| Compute on read, cache via generated column | PostgreSQL GENERATED ALWAYS AS column STORED | |

**User's choice:** On insert/upsert + Alembic backfill (recommended).
**Notes:** Migration upgrades the Phase 1 nullable column to NOT NULL after backfill.

---

## TransactionOut shape

### New fields

| Option | Description | Selected |
|--------|-------------|----------|
| Full quartet | uah_amount_minor, fx_rate (str), fx_rate_date, fx_source, fx_stale | ✓ |
| Minimal: uah_amount_minor + fx_stale | Smaller payload; loses audit trail | |
| Quartet plus uah_amount_decimal as transport | Duplication of representations | |

**User's choice:** Full quartet (recommended).

### fx_source semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Three-way: native_uah \| mono_card \| nbu | Explicit Pitfall 8 audit trail | ✓ |
| Two-way: native_uah \| nbu | Loses Mono-converted distinction | |
| Computed on the fly per row, no enum | Boolean is_mono_converted; less rich | |

**User's choice:** Three-way (recommended).
**Notes:** Computation for mono_card and nbu is mathematically identical — both use account-currency amount × NBU rate. The label is for audit/UI clarity only.

### No-rate-available fallback

| Option | Description | Selected |
|--------|-------------|----------|
| Nulls + fx_stale=true | Row appears in feed; UI renders "—" | ✓ |
| Block the response entirely (503 / partial) | Heavy-handed for single-user app | |
| Fall back to last-known rate of ANY date | Violates Pitfall 7 | |

**User's choice:** Nulls + fx_stale=true (recommended).

### fx_stale definition

| Option | Description | Selected |
|--------|-------------|----------|
| Only when fx_rate_date < attributed_day | Weekend/holiday fallback OR no rate at all | ✓ |
| Also when today's cron failed (any-row, today) | Conflates failure modes | |
| Plus a separate fx_fallback_kind field | Strictly more info; YAGNI v1 | |

**User's choice:** Only when fx_rate_date < attributed_day (recommended).

### Computation strategy

| Option | Description | Selected |
|--------|-------------|----------|
| SQL LATERAL join with MAX(rate_date) | Postgres-native; single round-trip; matches ROADMAP SC#3 phrasing | ✓ |
| Two queries + Python composition | Simpler SQL, more code | |
| Materialized view refreshed on FX cron | Violates FX-03 "computed on read" | |

**User's choice:** SQL LATERAL join with MAX(rate_date) (recommended).

---

## Currency scope policy

### Tracked-currency strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Lazy auto-add on first observation | Self-adjusts to what user spends in; range-fetch new currency for 12mo | ✓ |
| Hardcoded USD + EUR only (fail-closed) | Roadmap-literal; loses correctness on travel | |
| Eager: fetch all NBU-published currencies daily | ~7,500 rows on bootstrap; storage + HTTP overhead | |

**User's choice:** Lazy auto-add on first observation (recommended).

### Lazy mechanism

| Option | Description | Selected |
|--------|-------------|----------|
| tracked_fx_currencies table | Explicit, queryable, easy to test | ✓ |
| Derive on the fly from SELECT DISTINCT currency FROM transactions | No new table; less code; lazier UX | |
| Hardcoded seed list, expandable via config | Contradicts "zero manual upkeep" | |

**User's choice:** tracked_fx_currencies table (recommended).

### NBU-unsupported currency

| Option | Description | Selected |
|--------|-------------|----------|
| Row stays in tracked_fx_currencies + fx_stale=true forever | Self-recovers if NBU adds rate later | ✓ |
| Hard 4xx the import | Loses transaction; violates ING-03 | |
| Mark as untracked, never retry | Cleaner DB; loses self-recovery | |

**User's choice:** Row stays in tracked_fx_currencies + fx_stale=true forever (recommended).

---

## Claude's Discretion

User left these to Claude — captured in CONTEXT.md `<decisions>` § "Claude's Discretion":

- NBU range endpoint exact URL & response shape (researcher confirms before Plan-stage)
- httpx client reuse policy (separate instance from MonobankImporter; tenacity retry with exponential backoff capped at 3 attempts)
- numeric_to_alpha map extension (PLN/GBP/CHF/etc. on first observation; numeric fallback if unmapped)
- structlog redaction (no new patterns — NBU responses contain no PII)
- Alembic migration shape (0003_fx_truth.py — create fx_rates, create tracked_fx_currencies seeded with USD+EUR, backfill attributed_day, ALTER to NOT NULL)
- No new API endpoint in Phase 3 (GET /api/transactions stays; gains 5 fields)
- Money value object usage (optional in the rollup math; API surface stays int+str)
- Testing fixtures and harness reuse

---

## Deferred Ideas

Captured in CONTEXT.md `<deferred>`:

- fx_fallback_kind enum on TransactionOut (weekend/holiday/fetch_failure/no_rate)
- /api/fx/rates and /api/fx/bootstrap endpoints
- Eager fetch of all NBU-published currencies
- Cross-rate support (from_currency, to_currency columns)
- Materialized view for the rollup join
- fx_runs audit table mirroring import_runs
- Holidays library (Ukraine) for explicit weekend/holiday distinction
- Per-tx op_currency + op_amount_minor exposure on TransactionOut
- API-level fx_freshness global header / status
- NBU 5xx retry-budget telemetry
- Extending /api/import/status with fx: block
- scheduler_state entry for FX failures
