# Architecture Research

**Domain:** Self-hosted single-user personal-finance app — Python backend, JS frontend, SQL store, Docker on homelab/NAS, Monobank polling integrator, multi-currency, hybrid (rules-now / LLM-later) categorization
**Researched:** 2026-05-10
**Confidence:** HIGH

> Prescriptive architecture for finance-bro. One primary design is named; alternatives are listed only where the trade-off is real. Component names introduced here are the canonical names downstream phases (roadmap, planners, schema design) will reuse — do not rename them lightly.

---

## 1. Primary Architecture: Modular Monolith, Single Process, "Ports & Adapters" Lite

**One Python process. Two compose services: `app` and (optional) `db`. SQLite on a Docker volume by default; Postgres only if you outgrow it.**

The whole backend — HTTP API, scheduler, poller, categorizer, reconciler, FX fetcher — lives in **one FastAPI process**. APScheduler's `AsyncIOScheduler` runs inside the same event loop; jobs share the SQLAlchemy engine and the connection pool with the API. No worker queue, no Celery, no Redis. The frontend is a separate single-page JS bundle served either by FastAPI's static mount or by an `nginx` sidecar (your call; the architecture doesn't depend on it).

The internal organization is **modular monolith with port/adapter seams** at exactly two places where extensibility is required by spec:

1. The **Importer** seam — so PrivatBank/Wise can be added later without rewriting the model layer.
2. The **Categorizer** seam — so an LLM categorizer plugs in next to the rules engine.

Everywhere else, prefer plain functions over interfaces. This is a 1–2 month build, not a layered-DDD exercise.

**Why one process, not multiple.**

- Mono's rate budget is **1 request / 60 s per token**. There is literally one poller doing one thing every minute. There is nothing to parallelize.
- SQLite is single-writer; running the API and a separate worker container against the same SQLite file invites `SQLITE_BUSY` headaches without buying anything ([SQLite WAL — single writer at a time](https://sqlite.org/wal.html)).
- Crash semantics are simpler: one container restarts, one set of logs, one PID to reason about.
- Sidecar workers exist to either (a) fan out CPU-bound work or (b) survive web-request timeouts. Neither applies here.

**When you'd split.** If you ever add a local LLM categorizer that does CPU-heavy embeddings or runs in-process Ollama, move that *one job* to a sidecar container behind an HTTP/gRPC port and keep the rest of the monolith intact. That's the only scaling story we need to support, and the Categorizer port is designed for it.

### Alternatives Considered (and rejected for v1)

| Alternative | Why rejected |
|---|---|
| FastAPI + Celery + Redis | Three containers and a broker for one job that runs every 60 s. Pure ceremony. |
| FastAPI + RQ | Same problem in lighter clothing. SQLite-native job stores work fine. |
| Two compose services (api + worker) sharing SQLite | SQLite single-writer + cross-process locking adds risk for zero payoff. |
| Postgres-by-default | Operational tax (backup story, vacuum, container ordering, more RAM) for a single-user app. SQLite is the default; Postgres is a documented escape hatch. |
| Pure microservices (poller, fx, categorizer as services) | Overkill at this scale; HTTP between localhost components is just slower function calls with worse error semantics. |

---

## 2. Component Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Browser (LAN/Tailscale)                         │
│                          ┌──────────────────────────┐                        │
│                          │   Web UI (JS SPA)        │                        │
│                          │   - Dashboard            │                        │
│                          │   - Transaction feed     │                        │
│                          │   - Rule editor          │                        │
│                          └─────────────┬────────────┘                        │
└─────────────────────────────────────────┼────────────────────────────────────┘
                                          │ HTTPS (or HTTP behind Tailscale)
                                          │ JSON REST + SSE
┌─────────────────────────────────────────┼────────────────────────────────────┐
│  Container: app   (single Python proc, FastAPI + AsyncIOScheduler)          │
│                                          │                                    │
│   ┌──────────────────────────────────────▼──────────────────────────────┐    │
│   │                          API Layer (FastAPI)                         │    │
│   │   /accounts  /transactions  /categories  /rules  /import  /events    │    │
│   └────────────┬───────────────────────────────────────────────┬─────────┘    │
│                │                                               │              │
│   ┌────────────▼─────────────┐   ┌────────────────┐   ┌───────▼──────────┐   │
│   │   Application Services    │   │  Scheduler     │   │  Event Bus       │   │
│   │   (use cases / handlers)  │◄──┤  (APScheduler) │──►│  (in-process     │   │
│   └─┬────────┬──────────┬─────┘   └────────┬───────┘   │   pub/sub for    │   │
│     │        │          │                  │           │   SSE feed)      │   │
│     │   ┌────▼─────┐  ┌─▼────────┐  ┌──────▼──────┐    └──────────────────┘   │
│     │   │Categorizer│  │Reconciler│  │   Poller    │                          │
│     │   │  Engine   │  │   Engine │  │ (orchestr.) │                          │
│     │   └────┬─────┘  └─┬────────┘  └──────┬──────┘                           │
│     │        │          │                  │                                  │
│     │        │          │            ┌─────▼──────────────┐                   │
│     │        │          │            │  Importer Port     │                   │
│     │        │          │            │  (abstract)        │                   │
│     │        │          │            └─────┬──────────────┘                   │
│     │        │          │                  │                                  │
│     │        │          │            ┌─────▼──────────────┐  ┌─────────────┐  │
│     │        │          │            │ MonobankImporter   │──►  FX Service │  │
│     │        │          │            │ (HTTP adapter)     │  │ (NBU client │  │
│     │        │          │            └─────┬──────────────┘  │  + cache)   │  │
│     │        │          │                  │                 └──────┬──────┘  │
│     │        │          │                  │                        │         │
│     ▼        ▼          ▼                  ▼                        ▼         │
│   ┌──────────────────────────────────────────────────────────────────────┐    │
│   │                   Repository Layer (SQLAlchemy)                       │    │
│   │   AccountRepo  TransactionRepo  RuleRepo  ImportRunRepo  FxRateRepo  │    │
│   └──────────────────────────────────┬───────────────────────────────────┘    │
│                                      │                                        │
└──────────────────────────────────────┼────────────────────────────────────────┘
                                       │
                          ┌────────────▼────────────┐
                          │  SQLite (WAL mode)       │
                          │  /data/finance.db        │
                          │  Docker volume           │
                          └─────────────────────────┘
                                       ▲
                                       │ external
              ┌────────────────────────┴─────────────────────────┐
              │                                                  │
       api.monobank.ua/personal/                          bank.gov.ua/NBU
       (1 req / 60 s per token)                           (daily FX rates)
```

### Component Responsibilities

| Component | Responsibility | Owns |
|---|---|---|
| **API Layer** | Translate HTTP to use cases. No business logic. Validation via Pydantic. | Routes, request/response schemas |
| **Application Services** | Use cases: `import_now`, `recategorize_tx`, `mark_internal_transfer`, `add_cash_tx`. Compose engines + repos. | Use-case orchestration |
| **Scheduler** | APScheduler `AsyncIOScheduler`, persisted via `SQLAlchemyJobStore`. Triggers polling and FX fetch on cron. | Cron jobs, lock against re-entry |
| **Poller** (orchestrator) | Owns the **rate-limit budget**. Decides which account to poll next. Calls Importer. Writes `ImportRun`. | Budget allocation, backfill state |
| **Importer Port** (abstract) | `class Importer(Protocol)` — see §3. | Source-agnostic contract |
| **MonobankImporter** (adapter) | Concrete adapter: `client-info`, `statement`, token mgmt, rate-limit-aware HTTP. Returns canonical + raw payload. | Mono-specific quirks |
| **FX Service** | NBU fetcher with on-disk cache. Provides `get_rate(date, currency) -> Decimal`. Idempotent. | NBU adapter, FX cache |
| **Categorizer Engine** | Plugin registry of `Categorizer` ports. Runs them in priority order. Stores `CategorySuggestion` rows. | Categorization pipeline |
| **RulesCategorizer** (default plugin) | Deterministic predicate matcher. Stored in DB. | Rule evaluation |
| **Reconciler** | Multi-pass: dedup → internal-transfer → refund — writes link rows, never deletes. | Pair detection |
| **Repository Layer** | SQLAlchemy 2.x sessions. Encapsulates all SQL. Idempotent upserts via natural keys. | DB I/O |
| **Event Bus** | Tiny in-process `asyncio.Queue` per SSE subscriber. Used to push `transaction.imported`, `import.completed` to UI. | UI feedback channel |
| **Web UI (SPA)** | View layer. No business logic beyond formatting and form validation. | Rendering, user interaction |

---

## 3. Importer Interface — the contract that lets PrivatBank slot in later

The single most important seam. Designed so v2 is "implement an interface", not "rewrite the model layer."

```python
# app/domain/ports/importer.py
from typing import Protocol, AsyncIterator
from datetime import datetime
from decimal import Decimal
from dataclasses import dataclass

@dataclass(frozen=True)
class CanonicalAccount:
    source_account_id: str           # Mono: account.id; Privat: IBAN; etc.
    source_kind: str                 # "mono.card" | "mono.jar" | "mono.fop" | "privat.card"
    display_name: str
    currency: str                    # ISO-4217 alpha
    balance_minor: int               # current balance in minor units, source currency
    raw: dict                        # full source payload, opaque to core

@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str                # Mono: id; the natural dedup key WITH source_account_id
    source_account_id: str
    occurred_at: datetime            # UTC
    amount_minor: int                # signed; negative = outflow; in source currency
    currency: str                    # txn currency, may differ from account currency (FX-on-card)
    operation_amount_minor: int | None  # for FX txns: amount in original currency before bank's conversion
    operation_currency: str | None
    description: str
    counterparty: str | None
    mcc: int | None
    hints: dict                      # source-specific signals the reconciler/categorizer can use
                                     # without polluting columns: e.g. {"mono.commissionRate": 0,
                                     # "mono.cashbackAmount": 50, "mono.receiptId": "..."}
    raw: dict                        # full source payload

class Importer(Protocol):
    source_kind: str                  # "mono", "privat24", "wise"

    async def discover_accounts(self) -> list[CanonicalAccount]: ...
        # Lists accounts/jars/cards visible to this token/credentials.

    async def fetch_transactions(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]: ...
        # Yields canonical transactions in [since, until]. Adapter handles paging,
        # rate limiting, and the API's specific window cap.

    @property
    def request_budget(self) -> "RequestBudget": ...
        # Returns the rate-limit policy this importer enforces (see Polling Architecture).
```

### How quirks are preserved without polluting the canonical schema

- **`raw: dict`** — the entire source payload, stored as JSON in `transactions.raw_payload` and `accounts.raw_payload`. Never queried by core logic; available for migrations and audit.
- **`hints: dict`** — *promoted* source signals used by reconciler/categorizer (MCC, cashback, commission, FOP flag, jar id). Stored as JSON in `transactions.hints`, indexed only on the keys that get queried (e.g. `mcc` via a generated column or a helper column).
- **`source_kind`** on accounts — distinguishes `mono.card` / `mono.jar` / `mono.fop`. Reconciler and UI can branch on this without knowing it's Mono-specific.
- **No `mono_*` columns on the canonical tables.** Mono-only fields live in `raw_payload` or `hints`. If a future importer needs the same concept (cashback), it uses the same `hints` key.

### Anti-pattern explicitly avoided

> Adding `mono_jar_id`, `mono_cashback_amount`, `mono_commission_rate` columns to `transactions`. This is how source-coupling rots a schema. Use `hints` JSON.

---

## 4. Storage Spine

### 4.1 Database choice — SQLite, WAL mode, on a Docker volume

**Default: SQLite (single file at `/data/finance.db`), WAL journal mode, foreign keys ON, 5 s busy timeout.**

For a single-user app with one writer (the poller, every 60 s) and a handful of readers (the API), SQLite + WAL is the right answer ([Solo founder SQLite case](https://abhishekchaudhary.com/blog/sqlite-vs-postgres-solo-founder), [SQLite WAL docs](https://sqlite.org/wal.html)). One container, one file, trivial backup, zero ops.

**Postgres is the documented escape hatch**, not the default. Move to Postgres if and only if (a) you add a sidecar that writes concurrently, or (b) you want first-class JSONB indexing on `raw_payload`. The repository layer hides the choice; SQLAlchemy 2.x makes the swap mostly painless.

**Required PRAGMAs at every connection:**

```
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

**Migration tool:** Alembic with **batch operations** for SQLite — non-negotiable, since SQLite has near-zero `ALTER TABLE` support and Alembic's batch mode is the workaround ([Alembic batch mode](https://alembic.sqlalchemy.org/en/latest/batch.html)).

### 4.2 Core entities

```
┌─────────────┐          ┌───────────────────┐
│  accounts   │1────────*│   transactions    │*──────┐
└─────────────┘          └───────────────────┘       │
                                  │  *                │
                                  │                   │ many-to-one
                                  │                   ▼
                          ┌───────▼───────┐    ┌──────────────┐
                          │transaction_   │    │  categories  │
                          │  links        │    └──────────────┘
                          │ (transfer/    │           ▲
                          │  refund pairs)│           │
                          └───────────────┘           │
                                                ┌─────┴────────┐
┌─────────────┐    ┌─────────────┐              │    rules     │
│ import_runs │    │  fx_rates   │              └──────────────┘
└─────────────┘    └─────────────┘
```

### 4.3 Schema spine (named for downstream phases — keep these names)

> Money rule: **all monetary amounts stored as signed BIGINT in minor units alongside an ISO-4217 `currency` column**. No floats, no `NUMERIC` for transactional rows. Decimal happens at the edges (FX math, display). This is the well-trodden Stripe-style approach ([best practices](https://cardinalby.github.io/blog/post/best-practices/storing-currency-values-data-types/)).

#### `accounts`
| col | type | notes |
|---|---|---|
| `id` | INTEGER PK | internal surrogate |
| `source_kind` | TEXT NOT NULL | `mono.card` / `mono.jar` / `mono.fop` / `cash` / future |
| `source_account_id` | TEXT NOT NULL | Mono `account.id` or `jar.id`; `cash` for the manual cash account |
| `display_name` | TEXT | user-editable |
| `currency` | TEXT NOT NULL | ISO-4217 |
| `balance_minor` | BIGINT | last-seen, signed |
| `is_archived` | BOOLEAN DEFAULT 0 | soft-hide |
| `raw_payload` | JSON | last-seen `client-info` slice for this account |
| `created_at` / `updated_at` | TIMESTAMP | |
| **UNIQUE** | `(source_kind, source_account_id)` | natural key |

#### `transactions`
| col | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `account_id` | FK → accounts.id NOT NULL | |
| `source_tx_id` | TEXT | Mono `id`. NULL allowed for `cash` accounts. |
| `occurred_at` | TIMESTAMP NOT NULL | UTC |
| `amount_minor` | BIGINT NOT NULL | signed; in `currency` |
| `currency` | TEXT NOT NULL | txn currency |
| `operation_amount_minor` | BIGINT NULL | original-currency amount for FX-on-card |
| `operation_currency` | TEXT NULL | |
| `description` | TEXT | |
| `counterparty` | TEXT | |
| `mcc` | INTEGER | indexed; promoted out of `hints` for query speed |
| `category_id` | FK → categories.id NULL | |
| `category_source` | TEXT | `auto.rules` / `auto.llm` / `manual` — see §6 |
| `is_user_locked` | BOOLEAN DEFAULT 0 | true when user manually set category — re-runs MUST NOT overwrite |
| `is_excluded` | BOOLEAN DEFAULT 0 | "ignore from spending" toggle (e.g. own-investment buys) |
| `is_deleted` | BOOLEAN DEFAULT 0 | soft-delete; row is kept for audit |
| `hints` | JSON | promoted source signals (cashback, commissionRate, jar context) |
| `raw_payload` | JSON | full source payload |
| `import_run_id` | FK → import_runs.id NULL | provenance |
| `created_at` / `updated_at` | TIMESTAMP | |
| **UNIQUE** | `(account_id, source_tx_id)` WHERE `source_tx_id IS NOT NULL` | partial unique index — the idempotency key |

> **Idempotency:** `(account_id, source_tx_id)` is the dedup key on import. Re-imports become `INSERT ... ON CONFLICT DO NOTHING` (SQLite) or `INSERT ... ON CONFLICT (account_id, source_tx_id) DO UPDATE SET raw_payload=excluded.raw_payload, updated_at=...` if you want self-healing.

> **Cash transactions** have `source_tx_id = NULL`, so the partial unique index lets multiple cash entries with no source id coexist. Cash dedup is the user's problem (we don't try to detect it).

> **Storage of raw_payload alongside canonical:** in the same row, in a JSON column. Rejected the alternative `raw_payloads` side-table because it just adds joins and a foreign key to maintain for zero current benefit. If `raw_payload` becomes a size problem (it won't at this scale), move it to a side table later — Alembic batch migration handles it.

#### `categories`
| col | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE NOT NULL | |
| `parent_id` | FK → categories.id NULL | flat-with-optional-tree; resist deep hierarchies |
| `color` / `icon` | TEXT | UI |
| `is_internal` | BOOLEAN DEFAULT 0 | for the special "Internal Transfer" / "Refund" pseudo-categories |

#### `rules`
| col | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `priority` | INTEGER NOT NULL | lower = higher priority; first-match wins |
| `name` | TEXT | |
| `predicate` | JSON | structured predicate, see §6 |
| `category_id` | FK → categories.id NOT NULL | |
| `is_enabled` | BOOLEAN DEFAULT 1 | |
| `created_at` / `updated_at` | TIMESTAMP | |

#### `transaction_links`  (transfer + refund pairs in one table)
| col | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `link_kind` | TEXT NOT NULL | `internal_transfer` / `refund` |
| `source_tx_id` | FK → transactions.id NOT NULL | the "from" leg (outflow) |
| `target_tx_id` | FK → transactions.id NOT NULL | the "to" leg (inflow / original-charge for refund) |
| `confidence` | REAL | 0..1; 1.0 = user-confirmed |
| `detected_by` | TEXT | `auto.rule` / `manual` |
| `created_at` | TIMESTAMP | |
| **UNIQUE** | `(source_tx_id, target_tx_id, link_kind)` | |

> **One table, two link kinds.** Transfer and refund are structurally the same: a pair of transactions, oriented. Different `link_kind` keeps the queries clean and the schema boring.

> **Why not a `transfer_group` table?** Mono internal transfers are always pairs. Adding a group table to handle "what if 3-way" is YAGNI for this domain.

#### `fx_rates`
| col | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `rate_date` | DATE NOT NULL | NBU rate date |
| `from_currency` | TEXT NOT NULL | always `UAH` for v1 (NBU semantics) |
| `to_currency` | TEXT NOT NULL | `USD`, `EUR`, … |
| `rate` | NUMERIC(18,8) NOT NULL | use Decimal here; it's read for math, not money storage |
| `source` | TEXT DEFAULT 'NBU' | |
| `fetched_at` | TIMESTAMP | |
| **UNIQUE** | `(rate_date, from_currency, to_currency, source)` | |

#### `import_runs`
| col | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `source_kind` | TEXT NOT NULL | |
| `account_id` | FK → accounts.id NULL | NULL for "discover all accounts" runs |
| `window_start` / `window_end` | TIMESTAMP | requested window |
| `status` | TEXT | `pending` / `running` / `success` / `partial` / `failed` |
| `requests_used` | INTEGER | mono budget consumed |
| `txs_inserted` / `txs_updated` / `txs_skipped` | INTEGER | |
| `last_cursor` | TEXT NULL | for resumable backfill: last successful chunk end-time |
| `error` | TEXT NULL | |
| `started_at` / `finished_at` | TIMESTAMP | |

> `import_runs` is **not** the same as APScheduler's job store. APScheduler tracks "should this job run?"; `import_runs` is the audit trail of what each run did, and the resumability state for backfill.

### 4.4 UAH rollup: computed-on-read, not denormalized

Do **not** add a `uah_amount_minor` column to `transactions`. Reasons:

- It pre-commits to a base currency. The UAH rollup is a **view concern**, not a fact.
- It must be recomputed if FX rates are corrected or backfilled.
- It tempts code to query `uah_amount` and silently use stale rates.

**Instead:** compute on read via a SQL view or query helper that joins `transactions` to `fx_rates` on `transactions.currency` and `transactions.occurred_at::date`. Cache the rendered result in the API response, not the database.

```sql
-- conceptually
SELECT t.*,
       CASE WHEN t.currency = 'UAH' THEN t.amount_minor
            ELSE round(t.amount_minor * fx.rate)
       END AS uah_amount_minor
FROM transactions t
LEFT JOIN fx_rates fx
  ON fx.to_currency = t.currency
 AND fx.from_currency = 'UAH'
 AND fx.rate_date = date(t.occurred_at);
```

If perf becomes a problem (it won't for one user with 5 years of transactions), materialize this as a view, then later as a `uah_rollup_cache` table populated by a job. Don't pre-optimize.

### 4.5 Soft-delete vs hard-delete

- User-visible delete = **soft-delete** (`is_deleted = 1`). Source data is auditable; user can undo; re-import won't resurrect a "deleted" tx because the unique index still hits.
- Edits to user-mutable fields (`category_id`, `description` if we let them edit it, `is_excluded`) are **destructive**, with the caveat that `raw_payload` preserves the original. A future "reset to source" feature is one update statement away.

---

## 5. Process Topology & Polling Architecture

### 5.1 One process, lifespan-managed scheduler

```python
# app/main.py
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    engine = create_engine(...)
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=engine)},
    )
    scheduler.add_job(poll_next_account, "interval", seconds=60, id="mono_poller",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(fetch_nbu_rates, "cron", hour=16, id="nbu_fetcher",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()
    app.state.engine = engine
    app.state.scheduler = scheduler
    yield
    # Shutdown
    scheduler.shutdown(wait=True)
    engine.dispose()

app = FastAPI(lifespan=lifespan)
```

This is the canonical pattern ([FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/), [APScheduler integration](https://medium.com/@rasifrazak123/fastapi-scheduling-background-tasks-backgroundtasks-vs-apscheduler-vs-celery-complete-guide-ff90d6be524b)). Key flags:

- `max_instances=1` + `coalesce=True` — if the previous tick hasn't finished, don't stack a second copy. This is your concurrency control for "user clicked Import while the poller was already running."
- `SQLAlchemyJobStore` — survives container restart so cron schedules persist. Note: APScheduler's job store is for *scheduling*, not for `import_runs` audit. Don't conflate.
- **Run with one Uvicorn worker.** `gunicorn --workers 4` will spawn four schedulers. Use `uvicorn --workers 1`. If you need horizontal API scale (you don't), move the scheduler to a thin sidecar.

### 5.2 Connection pool sharing

API requests, scheduler jobs, SSE handlers all use the same SQLAlchemy engine. With SQLite + WAL the model is "many readers, one writer at any moment" — fine here because writes are rare (one poll cycle per minute). Use a session-per-request for the API and a session-per-job for the scheduler; never share a session across the two.

### 5.3 Mono rate-limit budget — concrete design

**The budget is a single token bucket: capacity 1, refill 1 per 60 s, owned by `MonobankImporter`.**

```python
class RequestBudget:
    """Token bucket. Persisted last-call timestamp survives restart."""
    capacity: int = 1
    refill_seconds: int = 60

    async def acquire(self) -> None: ...   # awaits if budget exhausted
```

The bucket's "last-acquired-at" is **persisted to disk** (a single row in a tiny `kv_store` table or written to the import_runs trail). Otherwise a container restart resets the limiter and you slam the API right after a previous call — instant 429.

**Allocation across N accounts:** round-robin over **active** accounts, with priority bumps:

1. **Backfill > steady-state > on-demand-statement.** A backfill in progress consumes the budget until done, *unless* the user clicks "import now" — then the next slot is given to the on-demand request and backfill resumes after.
2. Within steady-state, round-robin: account A this minute, B next, C next, A again. With 3 accounts, each account refreshes every 3 minutes.
3. `client-info` (account discovery / balance refresh) costs 1 budget slot — schedule it once an hour, not every minute.

**Do not parallelize across accounts to "use the budget more efficiently."** The 60 s limit is per-token, not per-endpoint. One poll = one budget slot.

### 5.4 Backfill orchestration (12 months on a 31-day window)

**Design: chunked, resumable, idempotent.**

For a 12-month initial backfill across 3 accounts at 1 req/60 s with a 31-day max window:

- 12 months ÷ 31 days ≈ **12 chunks per account**, plus 1 `client-info` discovery call. With 3 accounts, **~37 budget slots**, i.e. **~37 minutes** of wall-clock time worst case.
- Walk **newest-to-oldest** so the user sees recent history populating first while older months trickle in. (Mono returns transactions inside the window in some order; the chunking is what we control.)
- Persist each chunk's `(account_id, window_end)` to `import_runs.last_cursor` on success. Resume from `last_cursor` on restart.
- One backfill job per account; the poller orchestrator picks the next chunk to fetch when its turn comes up.
- Idempotency under crash mid-import is owned by the unique index on `(account_id, source_tx_id)` — re-running a chunk that was partially committed is safe.

**Catch-up after extended downtime** is just "another backfill, but the window starts at `last successful poll time` and walks forward." Same machinery.

### 5.5 What if user runs importer simultaneously twice?

- The `mono_poller` APScheduler job has `max_instances=1` — the second invocation is dropped.
- The `/import/now` endpoint enqueues an APScheduler one-shot with the same coalesce semantics, or simply sets a flag on `import_runs` and lets the scheduled tick service it.
- The token bucket itself prevents two HTTP calls to Mono within 60 s regardless.

---

## 6. Categorization Pipeline

### 6.1 Categorizer port

```python
class Categorizer(Protocol):
    name: str                         # "rules" | "llm.local" | "llm.api"
    priority: int                     # higher runs first

    async def categorize(self, tx: Transaction) -> CategorySuggestion | None: ...
        # Returns a suggestion with confidence ∈ [0, 1], or None if no opinion.

@dataclass
class CategorySuggestion:
    category_id: int
    confidence: float
    reasoning: str | None             # optional, for audit / UI hover
    categorizer_name: str
```

### 6.2 When categorization runs

- **On import**, immediately after a transaction is upserted.
- **On rule change**, the user can request "re-run rules" — affects only transactions where `is_user_locked = 0`.
- **On demand**, from the transaction-edit UI (re-categorize this one).

### 6.3 Pipeline behavior

1. Pipeline iterates registered categorizers in `priority` order.
2. **First categorizer with `confidence >= threshold` wins.** Threshold is per-categorizer (rules: 1.0 since deterministic; LLM later: 0.7 default, configurable).
3. The winning suggestion writes `category_id`, `category_source = "auto.<categorizer.name>"`.
4. **`is_user_locked = 1` short-circuits the pipeline.** Manual edits are sacred.
5. If no categorizer matches: `category_id = NULL`, `category_source = "uncategorized"`. UI shows these prominently.

### 6.4 Rule storage and matching

A rule is a **structured predicate** (no eval, no string templating, no regex-of-doom):

```json
{
  "all": [
    {"field": "mcc", "op": "in", "value": [5411, 5499]},
    {"field": "amount_minor", "op": "<", "value": 0},
    {"field": "description", "op": "icontains", "value": "ATB"}
  ]
}
```

Supported ops (v1): `=`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not_in`, `icontains`, `iequals`, `regex`. Supported fields: `mcc`, `amount_minor`, `currency`, `description`, `counterparty`, `account_id`, `hints.<key>`. Combine with `all` / `any` (no nesting beyond two levels in v1).

**Matching algorithm:** load enabled rules ordered by `priority ASC`, evaluate predicate, **first match wins**, return suggestion with `confidence = 1.0`. No "all-match" mode in v1; it complicates UX without payoff.

### 6.5 LLM-later, without coupling

The Categorizer port is the only seam needed. To add an LLM categorizer in v1.5:

1. Implement `LlmCategorizer(Categorizer)` in `app/adapters/categorizer/llm.py`.
2. Register it in the categorizer registry with a lower priority than `RulesCategorizer`.
3. Done. Existing rows are untouched. New imports route through both; rules still win when they match.

Optional: provide a `/categorizer/run` endpoint that re-categorizes only `is_user_locked = 0` rows where `category_source = "uncategorized"` — useful for backfilling LLM categorization over historical rows.

If the LLM is CPU-heavy (Ollama), it moves to a sidecar container and the adapter becomes an HTTP client. The port doesn't change.

### 6.6 User edits vs re-running rules

- User edits a category in the UI → sets `category_id = X`, `category_source = "manual"`, `is_user_locked = 1`.
- "Re-run rules" sweep iterates only `is_user_locked = 0`. Manual edits are preserved by definition.
- "Reset this transaction to auto" UI action → `is_user_locked = 0`, then run categorizer pipeline.

---

## 7. Reconciliation Pipeline

### 7.1 Pass order

```
Import          →  Dedup        →  Internal-Transfer  →  Refund         →  Categorize
(unique idx)       (the upsert)    (link table)          (link table)      (pipeline)
```

Dedup is structural — handled by `(account_id, source_tx_id)` unique index on insert. No separate pass needed.

The remaining passes run sequentially after each import chunk completes. They're safe to re-run; they're idempotent because they upsert into `transaction_links` with a unique constraint.

### 7.2 Internal-transfer detection

**Window:** ±2 days around the candidate transaction.

**Signal set (deterministic; v1):**

A pair (tx_a, tx_b) is an internal transfer if **all** of:

1. Both `account_id`s belong to the same user (always true here — single user).
2. `tx_a.amount_minor < 0` (outflow), `tx_b.amount_minor > 0` (inflow).
3. `|tx_a.amount_minor| == tx_b.amount_minor` if same currency. If different currency, fall back to FX-converted match within a tolerance (±2%).
4. `|tx_a.occurred_at - tx_b.occurred_at| <= 2 days`.
5. Both accounts are present in our `accounts` table (i.e., not "external").
6. **Strong signal**: Mono `hints` mention a sibling account. Mono's `comment`/`description` for jar transfers contains the jar name. Use it when present.

If multiple candidates match, prefer the one with the smallest time delta and same currency.

Confidence: 1.0 when same-currency exact match within 24 h; 0.8 for FX-converted; 0.6 for fuzzy. Anything < 0.8 surfaces in the UI for user confirmation.

### 7.3 Refund / reversal detection

**Window:** ±60 days.

**Signal set:**

A pair (orig, refund) is a refund if:

1. Same `account_id` (refunds come back to the source).
2. `orig.amount_minor < 0`, `refund.amount_minor > 0`.
3. `|orig.amount_minor| == refund.amount_minor` (same currency by definition).
4. `refund.occurred_at > orig.occurred_at`.
5. Same or strongly-overlapping `counterparty` / `description`.
6. Same `mcc` if both have one.

Confidence model is the same as transfers. Below 0.8 surfaces for confirmation.

### 7.4 Effect on views

Linked legs are still individual rows. Spending/dashboard views **filter out linked legs**:

- For `internal_transfer`: exclude both legs from spending math (UAH rollup ignores them).
- For `refund`: net to zero — show as a single line item in detail view, optional.

`is_excluded` and `transaction_links` are both used: `is_excluded` for user-flagged exclusions (e.g. "this is my brokerage funding, ignore from spending"), `transaction_links` for system-detected pairs.

---

## 8. FX Service — graceful degradation is the design

### 8.1 Behavior

- One scheduled job at **16:00 Europe/Kyiv** (NBU publishes daily at 15:30 — give 30 min margin).
- Fetches today's rate for `USD`, `EUR` (and any other currency that appears in `transactions.currency`).
- Idempotent upsert into `fx_rates`.
- On first run, also backfills 12 months of historical rates (NBU's API supports date queries).

### 8.2 Failure modes

- **NBU down on import day.** The transaction is imported; UAH rollup is computed-on-read using the most recent available rate (`fx_rates` query: `MAX(rate_date) WHERE rate_date <= occurred_at::date`). Mark the rollup row in API response with `fx_stale: true` so the UI can hint. Do NOT block import on FX availability.
- **Currency we've never seen before** (e.g. user opens GBP card). FX fetcher logs a warning, schedules a one-off fetch for that currency. Until that succeeds, the txn shows native currency only in the UI; rollup falls back to "—".

---

## 9. API Shape

**Style: REST, resource-oriented, plus an SSE stream.** Not RPC, not "endpoints-by-view". Reasons: small surface, pre-built FastAPI tooling, the frontend is one client we own — REST is the path of least surprise.

### 9.1 Endpoints (v1, indicative)

```
GET    /api/accounts
PATCH  /api/accounts/{id}                     # rename, archive

GET    /api/transactions?cursor=...&limit=50  # cursor pagination, see §9.2
GET    /api/transactions/{id}
PATCH  /api/transactions/{id}                 # set category, exclude, lock
POST   /api/transactions/cash                 # add cash transaction
POST   /api/transactions/{id}/split           # v1 if time, else v1.1
POST   /api/transactions/{id}/merge           # v1.1
POST   /api/transactions/{id}/link            # manually link transfer/refund
DELETE /api/transactions/{id}/link/{link_id}

GET    /api/categories
POST   /api/categories
PATCH  /api/categories/{id}
DELETE /api/categories/{id}

GET    /api/rules
POST   /api/rules
PATCH  /api/rules/{id}
POST   /api/rules/{id}/test                   # dry-run match against last N txs
POST   /api/rules/run                         # re-run rules over unlocked txs

POST   /api/import/now                        # trigger ad-hoc poll
POST   /api/import/backfill                   # kick off a 12-month backfill
GET    /api/import/runs                       # audit trail

GET    /api/dashboard/this-month              # pre-aggregated for dashboard
GET    /api/fx/rates?date=YYYY-MM-DD

GET    /api/events                            # SSE: transaction.imported, import.completed,
                                              #       link.detected (low-conf), rule.matched
```

### 9.2 Pagination — cursor (keyset), not offset

Transaction feed grows monotonically and gets imported into. Offset pagination causes duplicates and skips when new rows arrive between page loads ([API pagination guide](https://www.getknit.dev/blog/api-pagination-best-practices)). Use a cursor over `(occurred_at DESC, id DESC)`:

```json
{
  "items": [...],
  "next_cursor": "eyJvY2N1cnJlZF9hdCI6IjIwMjYtMDUtMDlUMTI6MDA6MDBaIiwiaWQiOjEyMzR9",
  "has_more": true
}
```

Default limit 50, max 200. Cursor is base64(JSON) of `{occurred_at, id}` — opaque to the client.

### 9.3 Real-time UI feedback — SSE, not WebSockets

One direction (server → client), HTTP semantics, automatic browser reconnection, trivial to terminate at nginx, and "the client doesn't need to send messages" is exactly this app's shape ([SSE vs WebSockets](https://websocket.org/comparisons/sse/), [FastAPI SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/)).

Events emitted:

```
event: import.started      data: {"run_id": 17, "account_id": 3}
event: transaction.upserted data: {"id": 1042, "account_id": 3}
event: import.completed    data: {"run_id": 17, "inserted": 12, "updated": 0}
event: link.detected       data: {"link_id": 88, "kind": "internal_transfer", "confidence": 0.82}
event: error               data: {"code": "mono_429", "retry_in": 45}
```

The Event Bus is just an in-process `asyncio.Queue` per connected client — no Redis pub/sub needed for one user.

**Polling fallback** (UI long-polls `/api/import/runs?since=...` if SSE drops) is one extra endpoint and zero client-side complexity. Add only if SSE causes problems behind Tailscale (it shouldn't).

---

## 10. Data Flow — End-to-end Walkthrough

```
[T = every 60s]                                                              [T+ε]                       [T+ε]
────────────────────────────────────────────────────────────────────────────────────────────────────────
  1.  APScheduler tick fires                                                                              .
        │                                                                                                  .
        ▼                                                                                                  .
  2.  Poller.next() picks an account (round-robin, prioritized)                                            .
        │                                                                                                  .
        ▼                                                                                                  .
  3.  RequestBudget.acquire() — succeeds (last call was ≥ 60s ago)                                         .
        │                                                                                                  .
        ▼                                                                                                  .
  4.  MonobankImporter.fetch_transactions(account, since, until)                                           .
        │   - resolves chunk window (≤ 31d)                                                                .
        │   - HTTP GET api.monobank.ua/personal/statement/...                                              .
        │   - yields CanonicalTransaction stream                                                           .
        ▼                                                                                                  .
  5.  TransactionRepo.upsert_many(canonicals)                                                              .
        │   - INSERT ... ON CONFLICT (account_id, source_tx_id) DO NOTHING                                 .
        │   - raw_payload + hints stored as JSON                                                           .
        │   - returns ids of newly-inserted rows                                                           .
        ▼                                                                                                  .
  6.  ReconcilerEngine.run_passes(new_txs)                                                                 .
        │   - internal_transfer matcher (±2d window)                                                       .
        │   - refund matcher (±60d window)                                                                 .
        │   - writes to transaction_links                                                                  .
        ▼                                                                                                  .
  7.  CategorizerEngine.categorize_each(new_txs)                                                           .
        │   - skips is_user_locked=1                                                                       .
        │   - rules first; LLM later when present                                                          .
        │   - writes category_id, category_source                                                          .
        ▼                                                                                                  .
  8.  ImportRun marked success; EventBus.publish(import.completed)                                         .
        │                                                                                                  .
        ▼                                                                                                  .
  9.  SSE handler streams events to UI ──────────────────────────────────────►  Browser renders new txs   .
        │                                                                                                  .
        ▼                                                                                                  .
 10.  Dashboard endpoint, on next user request, joins transactions × fx_rates                              .
        for UAH rollup (computed-on-read).                                                                .
```

**FX flow (parallel, independent):**

```
[Daily cron 16:00 Kyiv]
  ▸ FxService.fetch(today, [USD, EUR, ...]) → upsert fx_rates
  ▸ Backfill job (one-time, on first start): fetch 12 months of historical rates
```

---

## 11. Recommended Project Structure

```
finance-bro/
├── docker-compose.yml             # one service: app. (Postgres added later if needed.)
├── Dockerfile                     # multi-stage: backend deps, frontend build, copy assets
├── pyproject.toml
├── alembic.ini
├── .env.example                   # MONOBANK_TOKEN=, BASE_CURRENCY=UAH, ...
│
├── data/                          # gitignored; mounted to /data in container
│
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI + lifespan
│   │   ├── config.py              # pydantic-settings; env vars
│   │   ├── domain/                # pure business logic, no I/O
│   │   │   ├── models.py          # dataclasses: CanonicalTransaction, Suggestion, ...
│   │   │   ├── ports/
│   │   │   │   ├── importer.py    # Importer Protocol
│   │   │   │   └── categorizer.py # Categorizer Protocol
│   │   │   └── services/          # use cases
│   │   │       ├── import_service.py
│   │   │       ├── reconcile_service.py
│   │   │       └── categorize_service.py
│   │   ├── adapters/
│   │   │   ├── importers/
│   │   │   │   └── monobank.py    # MonobankImporter
│   │   │   ├── categorizers/
│   │   │   │   └── rules.py       # RulesCategorizer
│   │   │   └── fx/
│   │   │       └── nbu.py         # NBU client + cache
│   │   ├── infra/
│   │   │   ├── db.py              # SQLAlchemy engine, session, PRAGMAs
│   │   │   ├── repositories/      # SQLAlchemy implementations
│   │   │   ├── scheduler.py       # APScheduler wiring
│   │   │   └── event_bus.py       # in-process SSE pub/sub
│   │   └── api/
│   │       ├── deps.py
│   │       ├── routers/
│   │       │   ├── transactions.py
│   │       │   ├── accounts.py
│   │       │   ├── categories.py
│   │       │   ├── rules.py
│   │       │   ├── import_.py
│   │       │   ├── dashboard.py
│   │       │   └── events.py      # SSE endpoint
│   │       └── schemas.py         # Pydantic
│   ├── alembic/
│   │   ├── env.py                 # render_as_batch=True for SQLite
│   │   └── versions/
│   └── tests/
│       ├── unit/
│       └── integration/           # docker compose up + sqlite tmp
│
└── frontend/
    ├── package.json
    ├── vite.config.ts             # or whatever — JS choice is Stack's call
    ├── src/
    │   ├── api/                   # generated/typed client of the REST API
    │   ├── pages/
    │   ├── components/
    │   └── stores/
    └── public/
```

### Structure rationale

- **`domain/` is dependency-free.** No SQLAlchemy, no FastAPI, no requests. Pure Python. This is what makes the importer/categorizer ports actually swappable.
- **`adapters/` is the only place external SDKs land.** `monobank.py`, `nbu.py`. When adding `privatbank.py`, it goes here and nowhere else.
- **`infra/` is "things that talk to the world but aren't business-meaningful":** DB session, scheduler, event bus.
- **`api/` is thin.** Routers parse, call services, return schemas. No SQL.
- **One `app` package, not split into `core`/`worker`/`api`.** They're all the same process; splitting them is fictional separation.

---

## 12. Build Order — feeds the roadmap directly

Dependency graph dictates this ordering. Each phase is shippable: at the end of each, you can `docker compose up` and use the bit that's done.

| # | Phase | What's built | Depends on |
|---|---|---|---|
| 1 | **Skeleton + storage spine** | FastAPI app, lifespan, SQLAlchemy + Alembic with batch mode, SQLite WAL, models for `accounts`, `transactions`, `categories`, `import_runs`. Health endpoint. Docker compose runs. | — |
| 2 | **Monobank importer (manual trigger)** | `Importer` port, `MonobankImporter` adapter, `RequestBudget` token bucket, `/api/import/now` endpoint, `import_runs` audit. No scheduler yet — user clicks the button. | 1 |
| 3 | **Backfill orchestration** | Chunked, resumable backfill over the 31-day window. `/api/import/backfill`. Crash-safe via unique index + `last_cursor`. | 2 |
| 4 | **Scheduler + steady-state polling** | APScheduler in lifespan, 60 s tick, round-robin across accounts, persisted last-call timestamp. | 2 |
| 5 | **FX service (NBU)** | `fx_rates` table, NBU adapter, daily cron, 12 month historical backfill, computed-on-read UAH rollup helper. | 1 |
| 6 | **Categorization (rules only)** | `Categorizer` port, `RulesCategorizer`, rule predicate language, registry, `/api/rules` endpoints, on-import categorization, "re-run rules" sweep, `is_user_locked` semantics. | 2 |
| 7 | **Reconciler** | `transaction_links` table, internal-transfer matcher (±2 d), refund matcher (±60 d), confidence model, manual link/unlink endpoints. | 6 |
| 8 | **Read API + cursor pagination** | `/api/transactions` cursor pagination, `/api/dashboard/this-month`, account/category endpoints. | 5, 6 |
| 9 | **SSE event stream** | `/api/events`, in-process bus, emit on import/link/error. | 8 |
| 10 | **Frontend MVP** | SPA, dashboard, transaction feed, quick-recategorize, rule editor, manual cash entry, manual link UI. | 8, 9 |
| 11 | **Manual edits: cash add, exclude, split/merge** | UI + endpoints. Soft-delete. | 10 |
| 12 | **Backup/restore UX** | "Download DB" endpoint that runs `VACUUM INTO`; "Export JSON" endpoint that streams full data; restore is documented (stop container, replace file, start). | 1 |
| 13 | **Polish** | Retries, error UI, empty states, mobile breakpoints, the rest of the long tail. | 10 |

**Recommended phase boundaries for the 1-2 month build:**

- **Weeks 1-2:** phases 1-4 — "I can poll Mono and see rows in SQLite."
- **Weeks 3-4:** phases 5-7 — "I can see UAH rollups and detected transfers."
- **Weeks 5-6:** phases 8-10 — "I have a usable web UI."
- **Weeks 7-8:** phases 11-13 — "Manual edits work, I can back up, polish."

LLM categorizer is **explicitly out of v1** (PROJECT.md confirms). Reserved for a v1.5 phase that adds `LlmCategorizer` and registers it; no other code changes required.

---

## 13. Failure Modes by Design

| Failure | Detection | Mitigation |
|---|---|---|
| **NBU FX endpoint down on import day** | NBU fetcher logs HTTP error | Import proceeds; rollup uses most recent rate; API marks responses `fx_stale: true`; retry on next cron tick. |
| **Mono token revoked / expired** | `MonobankImporter` gets 401/403 | `import_runs.status = 'failed'`, error stored, SSE `error` event with code `mono_unauthorized`, scheduler keeps trying every 60 s (cheap), UI shows banner "Reconnect Monobank token." |
| **Mono returns 429** | adapter catches `TooManyRequests` | Token bucket should prevent this; if it happens (clock skew), back off to next minute, log warning. |
| **DB volume out of space** | SQLite raises `disk I/O error` on insert | Health endpoint returns 503; SSE pushes `error` event with `code: storage_full`; docs include "exec into container, run `VACUUM`, prune `import_runs` older than N days." |
| **User runs importer twice at once** | APScheduler `max_instances=1` drops the second; if both come from `/api/import/now`, the second one's job is coalesced. Token bucket is the final safety net. | Idempotent unique index means even if both DID run, no duplicate rows. |
| **Crash mid-backfill chunk** | `import_runs` left in `running` | Startup recovery: any `running` run older than 5 min is marked `failed`; backfill resumes from `last_cursor` which only advances on chunk success. |
| **Mono returns malformed payload** | adapter validation fails | Whole txn skipped; `import_runs` records `txs_skipped += 1` and the error; raw_payload of the offending response logged. Other txs in the same window still process. |
| **NBU rate missing for a transaction date** | rollup query returns NULL rate | Fall back to most recent prior rate; mark `fx_stale: true`; never block. Schedule a backfill fetch for that date. |
| **Schema migration fails on container start** | Alembic exits non-zero | Container fails to start. Operator notified via container restart loop. Always test migrations against a copy of `/data/finance.db` before deploying. |
| **Tailscale dies, user can't reach app** | not our problem | Document: Mono polling continues; data accumulates locally; UI returns when network does. |

---

## 14. Backup / Restore

**Single user, single file, single ops surface. Make it boring.**

### 14.1 Backup options (offer all three, default to one)

1. **Online backup endpoint (default).** `POST /api/backup` runs `VACUUM INTO '/data/backups/finance-YYYYMMDD-HHMMSS.db'` and returns the file. Safe under concurrent writes ([SQLite VACUUM INTO is transactional](https://oldmoe.blog/2024/04/30/backup-strategies-for-sqlite-in-production/)). Optionally hash the file and include the digest in the response.
2. **Cron-scheduled local snapshot.** APScheduler nightly job runs `VACUUM INTO` to `/data/backups/`, keeping last 7 dailies + 4 weeklies + 3 monthlies. Pure local, no third-party.
3. **JSON export** (`GET /api/export`). Streams full data as JSON for portability/inspection. Slower, larger, but human-readable and not bound to SQLite specifically. Useful if user ever migrates off the app.

**Do not** use `cp /data/finance.db backup.db` — not transactionally safe with WAL.

### 14.2 Restore

1. `docker compose down`
2. Replace `data/finance.db` with the backup file (and delete `finance.db-wal`, `finance.db-shm` siblings if present).
3. `docker compose up`

Document this in the README. No restore endpoint in v1 — it's a footgun for one user.

### 14.3 Data ownership story

The user owns `/data/`. Period. Mounting `./data:/data` in compose means:

- `docker compose down -v` does **not** wipe data (no named volume — bind mount).
- `tar -czf finance-backup.tgz ./data/` after stopping the app is a perfect cold backup.
- `git init` in `./data/` and commit the JSON export weekly = poor man's history (don't commit the `.db` file; binary diffs are useless).

---

## 15. Anti-Patterns to Avoid

### Anti-Pattern 1: Source-coupled columns
**What people do:** add `mono_jar_id`, `mono_cashback_minor`, `mono_commission_rate` to `transactions`.
**Why wrong:** rotates schema every time you add a source.
**Do instead:** `hints` JSON for promoted source signals; `raw_payload` JSON for the rest.

### Anti-Pattern 2: Storing money as float / Decimal in DB
**What people do:** `amount NUMERIC(18,2)` or worse `amount FLOAT`.
**Why wrong:** rounding bugs, locale issues, cross-language portability ([money handling best practices](https://cardinalby.github.io/blog/post/best-practices/storing-currency-values-data-types/)).
**Do instead:** `amount_minor BIGINT` + `currency TEXT`. Decimal at edges.

### Anti-Pattern 3: Denormalized UAH rollup column
**What people do:** add `uah_amount_minor` and write it on import.
**Why wrong:** stale when FX backfills/corrects; misses historic rate updates.
**Do instead:** computed-on-read. Materialize later if perf demands.

### Anti-Pattern 4: Celery / RQ / Redis for one cron job
**What people do:** "let's do this properly" with a full task queue.
**Why wrong:** three more containers, broker to maintain, for one job that runs once a minute.
**Do instead:** APScheduler in the FastAPI lifespan.

### Anti-Pattern 5: Adding a `users` table "just in case"
**What people do:** schema with `user_id` everywhere because "we might add multi-user later."
**Why wrong:** PROJECT.md explicitly forbids it; carrying user_id columns through a 1-month build is pure dead weight.
**Do instead:** no `users` table. If multi-user ever happens, it's a schema migration and an auth subsystem; the YAGNI cost of waiting is approximately zero.

### Anti-Pattern 6: Tight coupling of import to webhook semantics
**What people do:** model `webhook_payload`, `event_received_at`, push-style state machine.
**Why wrong:** v1 is poll-only; webhook may never come.
**Do instead:** poll-driven import; if webhooks ever land, the webhook handler is just another caller of `TransactionRepo.upsert_many()`. The Importer port doesn't need to know.

### Anti-Pattern 7: Hard-deleting transactions
**What people do:** user clicks delete → row gone.
**Why wrong:** re-import resurrects it; audit trail destroyed.
**Do instead:** soft-delete (`is_deleted = 1`); upsert respects the flag.

### Anti-Pattern 8: Eval-based rule predicates
**What people do:** store rule body as a Python expression string and `eval` it.
**Why wrong:** unsafe, unportable, can't be edited in UI.
**Do instead:** structured JSON predicates with a fixed op vocabulary.

---

## 16. Integration Points

### External Services

| Service | Pattern | Notes |
|---|---|---|
| Monobank `api.monobank.ua/personal/` | HTTP polling, token in header `X-Token` | Hard 1 req/60 s per token. 31-day max statement window. 500 tx default per response (`paginate=false` available). Token in env, never in code. |
| NBU `bank.gov.ua` | HTTP polling, daily cron | Public endpoint, no auth. Published 15:30 Kyiv daily. Cache aggressively; responses are stable. |

### Internal Boundaries

| Boundary | Communication | Notes |
|---|---|---|
| API ↔ Application Services | direct function calls | Pydantic at the edge; dataclasses internally. |
| Application Services ↔ Categorizer / Reconciler engines | direct function calls | Engines depend on `domain.ports`, not on each other. |
| Engines ↔ Adapters | through ports (Protocol) | Only place dependency inversion is enforced. |
| Adapters ↔ external services | HTTP via `httpx.AsyncClient` | Connection pool reused; per-request timeouts; structured error mapping. |
| Scheduler → Application Services | direct function calls inside the same process | `max_instances=1` per job, `coalesce=True`. |
| API → Frontend | REST + SSE over JSON | One client, no need for GraphQL or RPC. |

---

## 17. Scaling Considerations (mostly: don't bother)

| Scale | What's there | Adjustments |
|---|---|---|
| **One user (v1)** | the design above | none |
| **One user, 5+ years of txs (~50k rows)** | same | add indexes that materialized as needed (`occurred_at DESC`, `category_id`, `account_id`); already in spec |
| **Family-of-3 multi-user (hypothetical)** | not in scope per PROJECT.md | introduce `users` table, scope every read by `user_id`, add auth. This is a v3 conversation, not v1.5. |
| **CPU-heavy local LLM categorizer** | port exists | move to sidecar container, swap adapter implementation. No API changes. |

**The first bottleneck is FX rate gaps for esoteric currencies, not DB performance.** SQLite handles this scale trivially. Don't preoptimize.

---

## Sources

- [Monobank Open API v250818 (official)](https://api.monobank.ua/docs/index.html) — verified 1 req/60 s rate limit, 31-day statement window, 500-tx pagination
- [SQLite WAL documentation](https://sqlite.org/wal.html) — single-writer, multi-reader semantics
- [Alembic batch operations docs](https://alembic.sqlalchemy.org/en/latest/batch.html) — required for SQLite migrations
- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) — canonical startup/shutdown pattern
- [FastAPI Server-Sent Events](https://fastapi.tiangolo.com/tutorial/server-sent-events/) — SSE in FastAPI
- [SQLite vs Postgres for solo founders](https://abhishekchaudhary.com/blog/sqlite-vs-postgres-solo-founder) — single-user case for SQLite
- [Storing currency values: best practices](https://cardinalby.github.io/blog/post/best-practices/storing-currency-values-data-types/) — minor units integer pattern
- [API pagination: cursor vs offset](https://www.getknit.dev/blog/api-pagination-best-practices) — cursor pagination justification
- [SQLite backup strategies (oldmoe)](https://oldmoe.blog/2024/04/30/backup-strategies-for-sqlite-in-production/) — VACUUM INTO is transactionally safe
- [APScheduler with FastAPI (single-process)](https://medium.com/@rasifrazak123/fastapi-scheduling-background-tasks-backgroundtasks-vs-apscheduler-vs-celery-complete-guide-ff90d6be524b) — when not to reach for Celery
- [NBU Developer API](https://bank.gov.ua/en/open-data/api-dev) — official UAH FX rates
- [Hexagonal architecture in Python](https://blog.szymonmiks.pl/p/hexagonal-architecture-in-python/) — ports/adapters reference

---

*Architecture research for: self-hosted single-user personal finance app (finance-bro)*
*Researched: 2026-05-10*
