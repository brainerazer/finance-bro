# Phase 1: First Real Transaction - Research

**Researched:** 2026-05-10
**Domain:** Walking Skeleton — FastAPI + Postgres + Mono importer + rate-limit gate (single-user, homelab Docker)
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Token entry surface**

- **D-01:** Token enters the running app via the `MONO_TOKEN` environment variable only. No HTML form, no `POST /api/token`, no DB row for the token, no encryption code in Phase 1. The token never touches disk inside the app — the `.env` file (or compose env) is the at-rest substrate. This satisfies **OPS-01** (token at rest) via the filesystem + LAN/Tailscale trust boundary that the project already accepts; encryption-in-DB would be theatre when the master key would have to live in the same env. Rotation = edit `.env` + `docker compose up -d`.
- **D-02:** Manual import is triggered by `POST /api/import` (curl-able from the LAN). No scheduler in Phase 1 — APScheduler lands in Phase 2. The endpoint takes no body in Phase 1 (the polled account is fixed by D-04).
- **D-03:** Token validation is **lazy** — on the first `POST /api/import` the importer calls `/personal/client-info` (using one rate-limit slot), persists the discovered accounts, then proceeds to call `/personal/statement/...` after the gate releases. App startup is silent; no rate-budget burned just to verify the token is valid before the user has asked for anything.

**Account pick in Phase 1**

- **D-04:** The polled account in Phase 1 is **the first item with `type = card`** in the `/personal/client-info` response. Zero config — Bohdan does not need to know account IDs. Subsequent imports re-poll the same card. Multi-card / round-robin is Phase 2's problem.
- **D-05:** When `client-info` is fetched, **all** accounts Mono returned are persisted to the `accounts` table — cards, jars, and any FOP accounts. `source_kind` distinguishes them (`mono.card` / `mono.jar` / `mono.fop`). Only the picked card is polled in Phase 1, but the rest of the schema is honest from day one and Phase 2's round-robin doesn't need a re-discovery migration.
- **D-06:** Account discovery is a **one-shot on first import**. After the initial `client-info` call, accounts are read from the DB on every subsequent import; `client-info` is not re-called automatically. This protects the rate-limit budget — every additional `client-info` call is a slot that could have been a statement call. A manual refresh endpoint is deferred to Phase 2+.

**API surface scope**

- **D-07:** `GET /api/transactions` returns a plain list of transactions for the polled account, ordered by `time` desc. **No pagination, no filtering, no query params** in Phase 1. Volumes are tiny (one card, recent slice). Cursor pagination + filter + search land in Phase 6 (UI-02).
- **D-08:** `POST /api/import` is **synchronous**: it blocks until the rate gate releases and the statement call returns, then responds `200 OK` with JSON `{ "polled_account_id": "...", "statement_count": N, "inserted": M, "skipped_duplicates": K }`. Block time is up to ~60s if the gate is held. Single-user homelab — the wait is fine. SC#3 (two rapid POSTs → no duplicates) is testable directly from the response numbers.
- **D-09:** Phase 1 also ships these supporting endpoints:
  - `GET /api/health` — `{ "status": "ok", "db": "ok" }`. Used by the compose healthcheck. Zero domain logic.
  - `GET /api/accounts` — read-only list of discovered accounts (id, source_kind, currency, masked card-number tail when applicable). Confirms `client-info` worked; no rate-limit cost.
  - FastAPI `/docs` Swagger UI is enabled. With no real frontend, `/docs` is the human entry point to drive the API in a browser.
  - **Not** in Phase 1: `GET /api/import/status` (last poll, last error, 401/429 surface) — that is **ING-08**, owned by Phase 2.
- **D-10:** Each row in `GET /api/transactions` exposes:
  - `id` (DB-internal UUID/bigint)
  - `account_id` (FK to `accounts`)
  - `source_tx_id` (Mono `statementItem.id`)
  - `amount_minor` (BIGINT as a plain JSON integer — exact, no string wrapping needed at this magnitude)
  - `currency` (ISO-4217 alpha — `UAH` / `USD` / `EUR`, mapped from Mono's numeric `currencyCode` at the importer boundary)
  - `time` (Unix UTC posting time as ISO-8601)
  - `raw_payload` (Mono's `statementItem` JSON verbatim — SC#2's "verbatim raw_payload" clause)
  - `description`, `mcc`, `attributed_day` are deferred to Phase 2/3/6.

### Claude's Discretion

The user did not select these gray areas in `present_gray_areas`; they are explicitly Claude's call within the framing already established by `PROJECT.md` and `research/`:

- **Schema groundwork breadth** — the first Alembic migration ships:
  - `accounts` (`id`, `source_kind`, `source_account_id`, `currency`, `created_at`; unique on `(source_kind, source_account_id)`)
  - `transactions` (`id`, `account_id` FK, `source_tx_id`, `amount_minor BIGINT`, `currency CHAR(3)`, `time TIMESTAMPTZ`, `raw_payload JSONB`, `is_deleted BOOL DEFAULT FALSE`, `created_at`; **partial unique index** on `(account_id, source_tx_id) WHERE NOT is_deleted` — composite idempotency key per **ING-04**)
  - **Forward-looking columns on `transactions`** that Phase 1 doesn't read but later phases retrofit-painfully: `hold BOOL DEFAULT FALSE` (Phase 2 / **ING-05**), `category_id BIGINT NULL`, `category_source TEXT NULL`, `is_user_locked BOOL DEFAULT FALSE` (Phase 4 / **CAT-04** — schema groundwork called out in ROADMAP.md Phase 1 notes), `mcc INTEGER NULL`, `description TEXT NULL`, `attributed_day DATE NULL` (computed/cached in Phase 3+).
  - **Not** in the first migration: `categories`, `rules`, `fx_rates`, `transaction_links`, `import_runs` — each lands in its owning phase. Adding tables is cheap; adding columns to a hot table later is not, hence the asymmetry.
- **Rate-limit bucket persistence** — single-row Postgres table `mono_rate_state(token_hash TEXT PRIMARY KEY, last_acquired_at TIMESTAMPTZ NOT NULL)`, mutated under `SELECT ... FOR UPDATE`. Single transaction, no JSON-file race, fits the existing DB-as-state-of-truth model. The `MonobankImporter` is the sole owner of writes.
- **Token encryption mechanism** — moot (D-01 chose env-only).
- **Project layout** — `src/finance_bro/{api,core,db,importers,services}/` Python package, `frontend/` reserved-but-empty for Phase 6, `alembic/` at repo root, `compose.yml` at repo root, `Dockerfile` multi-stage. Backend serves `/api/*` only in Phase 1 (no static-files mount yet).
- **Log redaction** — `structlog` with a redaction processor that masks Mono token, `X-Token` header, and transaction `amount_minor` at `INFO` level and above. `DEBUG` shows raw values for local debugging. Redaction is **on by default**, controlled via `LOG_LEVEL` env var.
- **Mono numeric currencyCode → ISO alpha mapping** — implemented at the importer boundary using `iso4217` package or a hand-rolled lookup for `980 → UAH`, `840 → USD`, `978 → EUR`; everything downstream uses alpha.
- **Timezone handling** — `time` field is stored as UTC `TIMESTAMPTZ`. `attributed_day` derivation via `zoneinfo.ZoneInfo("Europe/Kyiv")` is a Phase 3 concern but the column lands in the first migration (nullable) so Phase 3 only needs a backfill query.
- **Dev ergonomics** — `uv` + `ruff` + `pytest` + `basedpyright` per `research/STACK.md`; `pre-commit` hooks for ruff. No frontend tooling installed in Phase 1 (deferred to Phase 6).

### Deferred Ideas (OUT OF SCOPE)

These came up in discussion or are implied by the env-var-only choice; they belong in later phases, not Phase 1:

- **HTML form for token entry / token rotation UI** — not in v1's roadmap (the network-gated model accepts env-var rotation). Revisit only if hosting model changes.
- **Token encryption-at-rest in the DB (Fernet / NaCl secretbox)** — moot under env-only storage. If a future phase introduces UI-driven rotation, encryption returns with it.
- **Multi-account polling / round-robin across cards + jars + FOPs** — Phase 2 (ING-05/06).
- **Manual `POST /api/accounts/refresh` endpoint to re-fetch `client-info`** — Phase 2+, when the cost of one extra rate slot is justified by the multi-account UX.
- **Cursor pagination + filter + search on `/api/transactions`** — Phase 6 (UI-02).
- **Async import via `import_runs` + `GET /api/imports/{run_id}`** — Phase 2 introduces `import_runs` for backfill resumability anyway; Phase 1's sync endpoint stays.
- **`GET /api/import/status` (last poll, last error, 401/429 distinction)** — Phase 2 (ING-08).
- **`category_id` / `category_source` / `is_user_locked` writes** — columns land in Phase 1's first migration as groundwork, but only Phase 4 reads/writes them.
- **`fx_rates` table + UAH rollup join** — Phase 3 (FX-02/03/04).
- **`transaction_links` table for transfer/refund pairing** — Phase 5 (REC-01/02).
- **Frontend (React + Vite + Tailwind + shadcn/ui)** — Phase 6 (UI-01..05). Reserved-but-empty `frontend/` directory in Phase 1 is acceptable but optional.
- **Daily `pg_dump` backup + restore drill** — Phase 7 (OPS-02).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ING-01 | Pull transactions from Monobank personal API across cards, jars, FOPs via `/personal/client-info` and `/personal/statement` | "Mono API Mechanics" (endpoint shapes, field map, numeric→alpha currency mapping). Phase 1 only polls the first card per D-04 but persists every account discovered per D-05. |
| ING-02 | Single token-bucket gate enforces 1 req/60s across all Mono callers; persisted to disk so restarts cannot violate the rate limit | "Persistent Rate-Limit Gate" (Postgres `mono_rate_state` table + `SELECT ... FOR UPDATE`). Pattern verified against [PostgreSQL 17 docs on row-level locking](https://www.postgresql.org/docs/17/explicit-locking.html). |
| ING-03 | Persist accounts and transactions in Postgres with the full Mono `statementItem` retained as `raw_payload` JSON per row | "Schema Invariants" (table DDL with `raw_payload JSONB NOT NULL` and verbatim importer round-trip). |
| ING-04 | Composite idempotency key `(account_id, source_tx_id)` prevents duplicate inserts on re-import | "Schema Invariants" — partial unique index DDL `WHERE NOT is_deleted`; ON CONFLICT DO NOTHING insert path. |
| ING-07 | Soft-delete model for transactions; `raw_payload` is immutable | `is_deleted BOOL DEFAULT FALSE` column + partial-unique-index design. Phase 1 never sets `is_deleted=true` but the column exists. |
| FX-01 | Store transaction amount in original currency (UAH/USD/EUR distinct, signed minor units `BIGINT` + ISO-4217 alpha currency) | "Money Handling" (BIGINT minor units, CHAR(3) alpha, `Decimal` only at edges). Pitfall 1 + 2 explicitly addressed. |
| OPS-01 | Token entry, validation, rotation; token encrypted at rest | D-01 narrows this to env-var-only for Phase 1 (rotation = edit `.env` + restart). Encryption-at-rest is moot in this phase by user decision. |
| OPS-04 | Log redaction on by default (Mono token, `X-Token` header, transaction amounts at `INFO+`) | "Log Redaction" (structlog processor, default-on, opt-out via `LOG_LEVEL=DEBUG`). |
| DEP-01 | Single-compose deploy (`app` + `db` services); bind-mount data directory; documented `PUID`/`PGID` | "Container / Compose" (compose.yml shape, bind mount, fixed UID, env-driven config). |
| DEP-02 | Network-gated access only — no app-level authentication; Tailscale/LAN is the trust boundary | "API Surface" (no auth middleware, no login, no cookies; `/docs` open; documented in README). |
</phase_requirements>

## Summary

Phase 1 is the **walking skeleton** — the thinnest possible vertical slice that proves the spine works end-to-end on the correctness invariants the rest of the project will inherit. Everything in scope serves four properties at once: (1) `docker compose up` produces a running app on the LAN, (2) `POST /api/import` does one rate-limit-gated round trip to Monobank and writes idempotent rows, (3) `GET /api/transactions` echoes those rows on the canonical schema (BIGINT minor units, ISO-4217 alpha currency, verbatim raw_payload, composite idempotency key), and (4) logs at INFO leak none of token, header, or amounts.

The stack is fully locked by CLAUDE.md and `.planning/research/`. All Python package versions verified against PyPI on 2026-05-10. The novel work in this phase is not stack selection — it is the **persistent rate-limit gate** (single-row Postgres state + `SELECT ... FOR UPDATE`), the **first Alembic migration** (which must include forward-looking columns Phase 4/5/6 will read but not yet write — see Pitfall 3 / 4 / 10 mitigations), and the **structlog redaction processor** (default-on at INFO+).

Two recurring traps to plan around: (a) the rate-limit gate must persist across container restarts via Postgres, never via in-memory state, and must be in place **before any business logic** (Pitfall 4); (b) every monetary value travels as `int` minor units in DB / JSON / Pydantic — `Decimal` only appears for FX math at the edges, `float` is banned (Pitfall 1).

**Primary recommendation:** Build in this exact order to avoid invariant retrofits: **(1)** compose + Postgres + healthcheck → **(2)** Alembic migration 0001 (full Phase 1 schema including forward-looking columns) → **(3)** `mono_rate_state` table + `RateLimitGate.acquire()` (Postgres `FOR UPDATE` pattern) → **(4)** structlog config with redaction processor → **(5)** `MonobankImporter` (httpx async, all calls go through `acquire()`, numeric→alpha currency mapping at boundary) → **(6)** `POST /api/import` synchronous handler (calls `client-info` once if accounts table empty, picks first card, calls `statement`, upserts with ON CONFLICT DO NOTHING) → **(7)** `GET /api/transactions`, `GET /api/accounts`, `GET /api/health`. Tests in Wave 0 land alongside (3) so the rate-limit gate has a real test gate before any handler depends on it.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Token storage (env-var only, D-01) | Container env / compose | — | No app-level handling. Read once at startup via `pydantic-settings`. |
| Token validation (lazy, D-03) | API / Backend (Application Service) | External — `api.monobank.ua/personal/client-info` | Validation is a side effect of the first import, owned by `MonobankImporter`. |
| Rate-limit budget enforcement (ING-02) | API / Backend (Importer service) | Database / Storage (`mono_rate_state` table) | Importer is the sole writer; persistence lives in Postgres; restart-safe by design. |
| Mono HTTP client (ING-01) | API / Backend (`importers/monobank.py`) | External | httpx `AsyncClient` with retry/timeout. All calls funnel through `RateLimitGate.acquire()`. |
| Account + transaction persistence (ING-03, ING-04) | Database / Storage (Postgres 17, JSONB) | API / Backend (Repository layer) | All SQL behind repos; routes never `import sqlalchemy`. |
| Schema migrations | Database / Storage (Alembic) | API / Backend (entrypoint runs `alembic upgrade head` before serving) | Pre-flight migrations are container-startup work; the API layer never alters schema at runtime. |
| Idempotency (ING-04) | Database / Storage (partial unique index) | API / Backend (`ON CONFLICT DO NOTHING`) | DB enforces uniqueness; code only formats the conflict clause. |
| Log redaction (OPS-04) | API / Backend (structlog processor) | — | Process-wide processor wired in startup; no per-call opt-in. |
| Read API (`GET /api/transactions` etc., D-07/D-09) | API / Backend (FastAPI routes + Pydantic schemas) | Database / Storage | Plain SELECT through repo; serialization via Pydantic models. |
| Healthcheck (`GET /api/health`) | API / Backend | Database / Storage (one trivial `SELECT 1`) | Compose healthcheck depends on this returning 200 with `db: ok`. |
| Single-compose deploy (DEP-01) | Container / Static (compose.yml + Dockerfile) | — | Two services: `app`, `db`. Bind-mount `./data/postgres`. No CDN, no separate frontend service in Phase 1. |
| Network gating (DEP-02) | Container / Network (host firewall + Tailscale) | — | App-level auth is explicitly absent. Documented in README. |

## Standard Stack

### Core

All versions verified against PyPI on 2026-05-10.

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python | 3.13.x | Backend language | `[VERIFIED: CLAUDE.md + research/STACK.md]` Stable; `Decimal`/`zoneinfo`/async first-class. |
| FastAPI | 0.136.1 | Web framework | `[VERIFIED: PyPI 2026-05-10]` Async-native, Pydantic v2, free OpenAPI schema. |
| Uvicorn | 0.46.0 | ASGI server | `[VERIFIED: PyPI 2026-05-10]` `--workers 1` — single-user, single scheduler. |
| Pydantic | 2.13.4 | Validation / serialization | `[VERIFIED: PyPI 2026-05-10]` Used by FastAPI internally. |
| pydantic-settings | 2.14.1 | Typed env config | `[VERIFIED: PyPI 2026-05-10]` Single `Settings` class read from env, no global config dict. |
| SQLAlchemy | 2.0.49 | ORM / Core | `[VERIFIED: PyPI 2026-05-10]` 2.0 typed API + Alembic; SQL Core escape hatch for repos. |
| Alembic | 1.18.4 | Schema migrations | `[VERIFIED: PyPI 2026-05-10]` Same authors as SQLAlchemy; autogenerate works for 80% of changes. |
| psycopg | 3.3.4 | Postgres driver (async) | `[VERIFIED: PyPI 2026-05-10]` Modern v3 is async-native; install `psycopg[binary,pool]`; URL `postgresql+psycopg://`. |
| PostgreSQL | 17 (Docker `postgres:17-bookworm`) | Database | `[VERIFIED: research/STACK.md + research/PITFALLS.md Pitfall 11]` Native NUMERIC; MVCC for poller+API; Alembic-friendly ALTER. |
| httpx | 0.28.1 | HTTP client | `[VERIFIED: PyPI 2026-05-10]` `AsyncClient`; same loop as FastAPI; native timeouts/retries. |
| structlog | 25.5.0 | Structured logs | `[VERIFIED: PyPI 2026-05-10]` Processor pipeline lets us inject the redaction filter declaratively. |
| iso4217 | 1.16.20260101 | Numeric → alpha currency | `[VERIFIED: PyPI 2026-05-10]` Single source of truth for the `980 → UAH` mapping; tiny dep. |
| tenacity | 9.1.4 | Retry decorator | `[VERIFIED: PyPI 2026-05-10]` Wrap Mono fetches with backoff capped at the rate-limit window. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| python-dotenv | 1.x | `.env` loading in dev | `[VERIFIED: pydantic-settings depends on it]` Production reads env from compose. |
| pytest | 9.0.3 | Tests | `[VERIFIED: PyPI 2026-05-10]` Standard. |
| pytest-asyncio | 1.3.0 | async test support | `[VERIFIED: PyPI 2026-05-10]` `asyncio_mode = "auto"` in `pyproject.toml`. |
| testcontainers | 4.14.2 | Real Postgres in tests | `[VERIFIED: PyPI 2026-05-10]` Spins up `postgres:17-bookworm` per test session — never SQLite-as-test-DB (would hide real production constraints like JSONB, partial indexes, `FOR UPDATE`). |
| respx | 0.23.1 | httpx request mocking | `[VERIFIED: PyPI 2026-05-10]` Mocks Mono responses without an HTTP server; pairs natively with httpx. |
| asgi-lifespan | 2.1.0 | FastAPI lifespan in tests | `[VERIFIED: PyPI 2026-05-10]` Required for test-driving the app with httpx `AsyncClient`. |
| freezegun | 1.5.5 | Clock control in tests | `[VERIFIED: PyPI 2026-05-10]` Needed for the rate-limit-gate "advance 65s and try again" test. |
| anyio | 4.13.0 | Async primitives | `[VERIFIED: PyPI 2026-05-10]` Pulled in transitively by FastAPI/httpx; useful for `anyio.sleep` in tests. |

### Dev tools

| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| uv | 0.11.12 | Python package + venv | `[VERIFIED: PyPI 2026-05-10]` `uv sync`, `uv run`, `uv add`. Lockfile = `uv.lock`. (Local dev machine has 0.9.26 — fine; CI/Docker should pin 0.11.x). |
| ruff | 0.15.12 | Linter + formatter | `[VERIFIED: PyPI 2026-05-10]` Replaces black + isort + flake8. |
| basedpyright | 1.39.3 | Type checker | `[VERIFIED: PyPI 2026-05-10]` Strict-mode pyright fork. |
| pre-commit | 4.6.0 | Git hooks | `[VERIFIED: PyPI 2026-05-10]` Runs ruff + basedpyright before commit. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Postgres `FOR UPDATE` for rate gate | Postgres `pg_advisory_lock` | Advisory locks are session-scoped, faster, and avoid table bloat — but transaction semantics are weaker, you have to remember to release, and the read-then-conditionally-update pattern still needs a row anyway. `FOR UPDATE` is the canonical pattern. `[CITED: postgresql.org/docs/17/explicit-locking.html]` |
| Postgres rate gate | File-based lock (`/data/rate_state.json` + flock) | Works on a single node but the file becomes a second source of truth alongside Postgres; flock semantics differ across filesystems; doesn't survive container removal that wipes the volume. Rejected. |
| Postgres rate gate | In-memory `asyncio.Lock` + last-acquired timestamp | Fails SC#4 — restarts violate the rate limit because the timestamp resets. Explicitly forbidden by ING-02. |
| testcontainers Postgres | SQLite-as-test-DB | SQLite can't replicate JSONB, `FOR UPDATE` row-locking, or partial unique indexes with the `WHERE NOT is_deleted` clause. Tests would lie. `[ASSUMED: based on SQLAlchemy 2.0 dialect-feature-matrix]` |
| testcontainers Postgres | Compose-managed `postgres-test` service | Equally valid; testcontainers is more portable across IDEs and CI. Either choice is fine — testcontainers preferred for ergonomics. |
| iso4217 | Hand-rolled `{980: "UAH", 840: "USD", 978: "EUR"}` dict | Hand-rolled is 3 lines and zero deps. iso4217 is more correct (covers every code Mono could plausibly return on a FOP account). Pick the dict if dependency budget is tight; otherwise the package. |
| structlog | stdlib `logging.Filter` for redaction | stdlib filter works but has no native processor pipeline — you'd hand-roll the JSON formatter and lose the lazy-rendering perf. structlog is the right call for this domain. |

**Installation:**

```bash
# Backend bootstrap (one-time, in Phase 1 first task)
uv init --package finance-bro
uv add fastapi==0.136.1 'uvicorn[standard]==0.46.0' \
       pydantic==2.13.4 pydantic-settings==2.14.1 \
       'sqlalchemy[asyncio]==2.0.49' alembic==1.18.4 \
       'psycopg[binary,pool]==3.3.4' \
       httpx==0.28.1 structlog==25.5.0 iso4217==1.16.20260101 tenacity==9.1.4
uv add --dev pytest==9.0.3 pytest-asyncio==1.3.0 \
             testcontainers==4.14.2 respx==0.23.1 asgi-lifespan==2.1.0 \
             freezegun==1.5.5 \
             ruff==0.15.12 basedpyright==1.39.3 pre-commit==4.6.0
```

**Version verification record:** PyPI registry queried 2026-05-10. Each version above represents `info.version` from `https://pypi.org/pypi/<pkg>/json` — i.e., the latest stable release as of that date. Pin exactly to avoid silent drift across the multi-week build.

## Architecture Patterns

### System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                      LAN / Tailscale                              │
│   curl, Browser → /docs (Swagger UI) → POST /api/import           │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼  HTTP (no auth — DEP-02)
┌──────────────────────────────────────────────────────────────────┐
│                   compose service: app                            │
│                                                                   │
│   ┌─────────────────────────────────────────────────────────┐    │
│   │                  FastAPI process (Uvicorn -w 1)         │    │
│   │                                                          │    │
│   │   API Layer — src/finance_bro/api/                      │    │
│   │     /api/health   /api/accounts   /api/transactions     │    │
│   │     /api/import (POST, sync)                            │    │
│   │            │                                             │    │
│   │            ▼                                             │    │
│   │   Application Services — src/finance_bro/services/      │    │
│   │     ImportService.run_one_card()                        │    │
│   │            │                                             │    │
│   │            ▼                                             │    │
│   │   Importer Port — src/finance_bro/importers/base.py     │    │
│   │     ImporterProtocol.discover_accounts()                │    │
│   │     ImporterProtocol.fetch_statement(account, since)    │    │
│   │            │                                             │    │
│   │            ▼                                             │    │
│   │   Mono Adapter — src/finance_bro/importers/monobank.py  │    │
│   │     MonobankImporter (uses httpx.AsyncClient)           │    │
│   │            │                                             │    │
│   │            ▼                                             │    │
│   │   Rate Gate — src/finance_bro/importers/rate_limit.py   │    │
│   │     RateLimitGate.acquire()  ←── owns 1-req/60s budget  │    │
│   │            │                                             │    │
│   │            ▼                                             │    │
│   │   Repository Layer — src/finance_bro/db/                │    │
│   │     AccountRepo  TransactionRepo  RateStateRepo         │    │
│   │            │                                             │    │
│   │   Logging — src/finance_bro/core/logging.py             │    │
│   │     structlog with redaction processor (default ON)    │    │
│   └────────────────────────────┬─────────────────────────────┘    │
│                                │ psycopg async                    │
└────────────────────────────────┼─────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│            compose service: db (postgres:17-bookworm)             │
│                                                                   │
│   Tables:                                                         │
│     accounts                       (D-05 — all kinds persisted)   │
│     transactions                   (forward-looking columns)      │
│     mono_rate_state                (single row per token_hash)    │
│     alembic_version                                               │
│                                                                   │
│   Volume: ./data/postgres → /var/lib/postgresql/data (BIND mount) │
└──────────────────────────────────────────────────────────────────┘
                                 │
External (egress documented in README per OPS-05 hint, full list in Phase 7):
   ├─ api.monobank.ua/personal/   (X-Token header; 1 req/60s)
   └─ (NBU is Phase 3 only — not contacted in Phase 1)
```

### Recommended Project Structure

```
finance-bro/
├── compose.yml                       # 2 services: app, db (DEP-01)
├── Dockerfile                        # Multi-stage: python:3.13-slim-trixie
├── .env.example                      # MONO_TOKEN=, DATABASE_URL=, LOG_LEVEL=
├── .dockerignore
├── pyproject.toml                    # uv-managed
├── uv.lock
├── alembic.ini
├── alembic/
│   ├── env.py                        # configures Postgres URL from settings
│   └── versions/
│       └── 0001_walking_skeleton.py  # accounts + transactions + mono_rate_state
├── src/
│   └── finance_bro/
│       ├── __init__.py
│       ├── main.py                   # FastAPI app factory + lifespan
│       ├── core/
│       │   ├── settings.py           # pydantic-settings Settings()
│       │   ├── logging.py            # structlog config + redaction processor
│       │   └── money.py              # Money(amount_minor: int, currency: str)
│       ├── api/
│       │   ├── routes_health.py      # GET /api/health
│       │   ├── routes_accounts.py    # GET /api/accounts
│       │   ├── routes_transactions.py# GET /api/transactions
│       │   ├── routes_import.py      # POST /api/import
│       │   └── schemas.py            # Pydantic response models
│       ├── db/
│       │   ├── engine.py             # async engine + session factory
│       │   ├── models.py             # SQLAlchemy 2.0 declarative models
│       │   ├── account_repo.py
│       │   ├── transaction_repo.py
│       │   └── rate_state_repo.py
│       ├── importers/
│       │   ├── base.py               # ImporterProtocol, CanonicalAccount, CanonicalTransaction
│       │   ├── monobank.py           # MonobankImporter (httpx adapter)
│       │   ├── rate_limit.py         # RateLimitGate (Postgres-backed)
│       │   └── currency_map.py       # numeric ⇆ alpha (980↔UAH, 840↔USD, 978↔EUR)
│       └── services/
│           └── import_service.py     # ImportService.run_one_card()
├── tests/
│   ├── conftest.py                   # testcontainers Postgres fixture, async client fixture
│   ├── test_health.py
│   ├── test_rate_limit_gate.py       # SC#4 — gate cannot fire twice in 60s, persists across reset
│   ├── test_idempotency.py           # SC#3 — second import is a no-op
│   ├── test_log_redaction.py         # SC#5 — token / X-Token / amount never appear in INFO
│   ├── test_importer_currency_map.py # 980→UAH, 840→USD, 978→EUR; minor-units pass-through
│   ├── test_import_route.py          # full POST /api/import path with respx-mocked Mono
│   └── test_migrations.py            # alembic upgrade head + downgrade base round-trip
└── frontend/                         # reserved-but-empty (Phase 6); .gitkeep only
```

### Pattern 1: Persistent Rate-Limit Gate (Postgres `FOR UPDATE`)

**What:** A single-row state table per token, mutated under a `SELECT ... FOR UPDATE` row lock. Reading and conditionally updating happen atomically inside one transaction. The gate releases when the timestamp + 60s has elapsed; before that it `await asyncio.sleep(remaining)`.

**When to use:** Any time a rate limit must persist across container restarts and be safe under concurrent callers. Owned by `MonobankImporter` per Pitfall 4.

**Why this pattern (vs. alternatives):** `[CITED: postgresql.org/docs/17/explicit-locking.html]` `SELECT ... FOR UPDATE` gives transactional read-then-update with automatic release at commit and no risk of leaving a stuck lock if the process crashes mid-acquire. Advisory locks would work but require explicit release and add a second mechanism for the same problem.

**Example:**

```python
# src/finance_bro/importers/rate_limit.py
# Source: pattern from postgresql.org/docs/17/explicit-locking.html
import asyncio
import hashlib
from datetime import datetime, timedelta, UTC
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

MONO_RATE_LIMIT_SECONDS = 65  # 60s API limit + 5s slack for clock drift

class RateLimitGate:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def acquire(self, token: str) -> None:
        """Block until at least MONO_RATE_LIMIT_SECONDS have elapsed since
        the last acquire for this token. Persists state in Postgres so
        a container restart cannot violate the limit."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(UTC)
        wait_until = None

        async with self._session_factory() as session:
            async with session.begin():
                # FOR UPDATE serializes concurrent acquirers
                row = (await session.execute(
                    text(
                        "SELECT last_acquired_at FROM mono_rate_state "
                        "WHERE token_hash = :h FOR UPDATE"
                    ),
                    {"h": token_hash},
                )).first()
                if row:
                    next_allowed = row[0] + timedelta(seconds=MONO_RATE_LIMIT_SECONDS)
                    if next_allowed > now:
                        wait_until = next_allowed
                # Optimistically claim THIS slot now — even though we may sleep,
                # no other caller can claim until we COMMIT.
                if row:
                    await session.execute(
                        text(
                            "UPDATE mono_rate_state SET last_acquired_at = :ts "
                            "WHERE token_hash = :h"
                        ),
                        {"ts": wait_until or now, "h": token_hash},
                    )
                else:
                    await session.execute(
                        text(
                            "INSERT INTO mono_rate_state (token_hash, last_acquired_at) "
                            "VALUES (:h, :ts)"
                        ),
                        {"h": token_hash, "ts": now},
                    )
            # transaction commits here; lock released

        if wait_until:
            remaining = (wait_until - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
```

> **Subtle point:** the timestamp written to the row is the time at which the *next* allowed acquire began — not "now". This way, two callers who arrive at the same instant both serialize correctly: caller A writes `now`, caller B reads `now+65s` under `FOR UPDATE`, writes `now+65s` (its own claimed slot), then sleeps until `now+65s`. The third caller would write `now+130s`, etc. The DB row always reflects the *latest claimed slot*, not the latest *fired* request.

### Pattern 2: Importer Port + Adapter

**What:** A `Protocol` class defining the contract every bank importer must implement. The Mono adapter is the only concrete implementation in v1. Future PrivatBank/Wise importers slot in without changing the application layer.

**When to use:** Any seam where a v2 swap is plausible. Two seams exist in this project (importer, categorizer); Phase 1 introduces the importer one.

**Example:**

```python
# src/finance_bro/importers/base.py
from typing import Protocol, AsyncIterator
from datetime import datetime
from dataclasses import dataclass

@dataclass(frozen=True)
class CanonicalAccount:
    source_account_id: str        # Mono: account.id; jar.id; etc.
    source_kind: str              # "mono.card" | "mono.jar" | "mono.fop"
    currency: str                 # ISO-4217 alpha
    raw: dict                     # full source payload

@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str             # Mono statementItem.id
    source_account_id: str
    occurred_at: datetime         # UTC
    amount_minor: int             # signed; in account currency
    currency: str                 # ISO-4217 alpha
    raw: dict                     # full source payload (Mono statementItem verbatim)

class ImporterProtocol(Protocol):
    source_kind: str

    async def discover_accounts(self) -> list[CanonicalAccount]: ...
    async def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]: ...
```

### Pattern 3: Synchronous Import Endpoint with `ON CONFLICT DO NOTHING`

**What:** `POST /api/import` is a thin handler that calls `ImportService.run_one_card()`, which (a) fetches accounts if the table is empty, (b) picks the first card, (c) acquires the rate gate, (d) calls `/personal/statement/{account}/{from}/{to}`, (e) bulk-inserts via `INSERT ... ON CONFLICT (account_id, source_tx_id) DO NOTHING`. The `inserted` and `skipped_duplicates` counts come from `cur.rowcount` vs len(payload).

**Example DDL fragment:**

```sql
-- alembic/versions/0001_walking_skeleton.py emits:
CREATE UNIQUE INDEX uq_transactions_account_source_tx
    ON transactions (account_id, source_tx_id)
    WHERE NOT is_deleted;
```

**Example insert:**

```python
# inside TransactionRepo.insert_many()
from sqlalchemy.dialects.postgresql import insert

stmt = insert(Transaction).values(rows)
stmt = stmt.on_conflict_do_nothing(
    index_elements=["account_id", "source_tx_id"],
    index_where=text("NOT is_deleted"),
)
result = await session.execute(stmt)
inserted = result.rowcount
skipped = len(rows) - inserted
```

### Pattern 4: structlog Redaction Processor (default ON)

**What:** A processor in the structlog pipeline that scrubs `token`, `X-Token`, and `amount_minor` (and any field name matching `*amount*` or `*token*`) at INFO+ levels. At DEBUG, raw values pass through (for local dev only — never in production logs).

**Example:**

```python
# src/finance_bro/core/logging.py
import logging, re, structlog

_REDACTED = "***REDACTED***"
_TOKEN_REGEX = re.compile(r"[A-Za-z0-9_-]{30,}")  # Mono tokens are ~40 chars

def _redact(_logger, method_name, event_dict):
    if method_name in {"debug"}:
        return event_dict
    for k in list(event_dict.keys()):
        if re.search(r"token|amount", k, re.IGNORECASE):
            event_dict[k] = _REDACTED
    # Also scrub message body
    if isinstance(event_dict.get("event"), str):
        event_dict["event"] = _TOKEN_REGEX.sub(_REDACTED, event_dict["event"])
    return event_dict

def configure(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,                 # MUST come before the renderer
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
    )
```

### Anti-Patterns to Avoid

- **In-memory rate-limit gate** — Pitfall 4. Fails SC#4 on restart. Always persist to Postgres.
- **`float` anywhere money lives** — Pitfall 1. JSON decoder defaults to float; ban with grep + a Pydantic config (`json_schema_serialization_defaults_required=True`, plus an `int` type annotation on `amount_minor`).
- **Raw `/100` and `*100` in code** — Pitfall 2. Wrap in named helpers (`kopecks_to_decimal`, `decimal_to_kopecks`) only used in display/edges. Mono returns kopecks already; Phase 1 has no display layer, so these helpers may not be needed at all in this phase — but the ban is in effect.
- **Hand-rolled SQL strings everywhere** — concentrate SQL in repos. Routes and services never `import sqlalchemy` directly.
- **Implicit JSON-ization of `Decimal` to float** — if `Decimal` ever reaches the Pydantic boundary in this phase (it shouldn't — `amount_minor` is `int`), serialize as string, never float.
- **Logging the request URL with embedded query parameters that include the token** — never. The token rides in the `X-Token` header, not the URL, but defense-in-depth means the redaction filter scrubs URLs too.
- **Named Docker volumes for the DB** — Pitfall 12. Bind-mount `./data/postgres` so `docker compose down -v` cannot wipe it and the user can `tar` it.
- **`docker compose down -v`** in any docs or scripts — same reason. Document `down` (no `-v`) only.
- **`alembic revision --autogenerate` without manual review** — autogenerate misses partial unique indexes (`WHERE NOT is_deleted`), which is precisely the index we need for ING-04. Hand-write or post-edit migration 0001.
- **Polling `client-info` on every import** — D-06. Burns the rate budget for no information gain.
- **Single-step "fetch then write timestamp" rate gate** — read-then-write without `FOR UPDATE` allows a TOCTOU race between two concurrent callers. Always use the transactional FOR UPDATE pattern.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HTTP client with retries + timeouts | `urllib` + `try/except` loop | `httpx.AsyncClient` + `tenacity` | httpx already implements connection pooling, timeouts, async; tenacity gives exponential backoff with jitter. |
| Schema migrations | Hand-rolled `CREATE TABLE` runner | Alembic | Migrations need versioning, rollback semantics, environment-aware DDL emission. |
| Async DB access | Threadpool over psycopg2 | psycopg 3 async + SQLAlchemy 2.0 async | Avoids threadpool exhaustion under FastAPI. |
| Settings loading | Module-level `os.getenv` calls | `pydantic-settings.BaseSettings` | Type-checked, default-aware, single source of truth, plays well with `.env`. |
| ISO-4217 numeric ↔ alpha mapping | Inline dict scattered across modules | `iso4217` package OR a single `currency_map.py` | One reference table; avoid the bug where two modules disagree on what `978` means (it's EUR, but one module wrote `EUR ` with a trailing space). |
| Rate-limit gate | Sleep-and-retry loop after 429 | Postgres-backed gate that *prevents* 429 in the first place | Pitfall 4. Reactive 429-handling burns the rate budget AND races with other callers. |
| Test database | In-memory SQLite | testcontainers `postgres:17-bookworm` | Production uses Postgres-only features (JSONB, partial indexes, FOR UPDATE). SQLite tests would silently lie. |
| Mono HTTP mocking | Run a real test HTTP server | `respx` | Native httpx adapter; no port conflicts; no flakiness. |
| JSON logging + redaction | hand-rolled `logging.Filter` + `json.dumps` | `structlog` with a redaction processor | Composable, lazy, plays well with FastAPI's existing logger. |
| FastAPI lifespan in tests | Start the server in a subprocess | `asgi-lifespan` + httpx `AsyncClient(transport=ASGITransport(app))` | Same process, real lifespan, fast. |
| Currency arithmetic | `float` operations | `int` minor units in DB/JSON; `Decimal` only at FX edges (Phase 3) | Pitfall 1. |

**Key insight:** every pitfall in `.planning/research/PITFALLS.md` Phase-1 column has a battle-tested library answer. The novel work is wiring them together correctly under the rate-limit constraint and the schema invariants — not re-implementing any of them.

## Common Pitfalls

### Pitfall 1: In-memory rate-limit gate cannot survive `docker compose restart`

**What goes wrong:** Container restarts mid-cycle. The in-memory `last_acquired_at` resets. The next `POST /api/import` immediately fires a Mono call, violating the 60s window, getting a 429 (or a token revocation in pathological cases).
**Why it happens:** "I'll persist it later" is a tempting v1 cut. The gate looks the same in tests until you actually restart the container.
**How to avoid:** Postgres-backed gate from the first commit. State table in migration 0001. No code path uses `asyncio.Lock` + a memory variable. SC#4 directly validates this.
**Warning signs:** A test that `await rate_gate.acquire()` twice without simulating a process boundary in between is not actually testing SC#4. The persistence test must (a) acquire, (b) tear down the in-memory client, (c) construct a new gate instance against the same DB, (d) attempt acquire — and the second acquire MUST sleep.

### Pitfall 2: First Alembic migration missing the `WHERE NOT is_deleted` clause

**What goes wrong:** A bare `UNIQUE (account_id, source_tx_id)` blocks soft-deleted rows from being re-inserted, so the moment Phase 6 implements user-facing soft-delete, soft-deleted-then-re-imported rows return as an `IntegrityError` instead of an UPSERT no-op.
**Why it happens:** Alembic's `--autogenerate` doesn't emit partial-unique-indexes from a SQLAlchemy `UniqueConstraint`. You have to hand-write `op.create_index(..., unique=True, postgresql_where=text("NOT is_deleted"))`.
**How to avoid:** Migration 0001 is hand-edited. Reviewer verifies the index DDL emits `WHERE NOT is_deleted`. Add a test that inserts row R, sets `is_deleted=true`, re-inserts row with the same `(account_id, source_tx_id)` — must succeed. (This is groundwork for ING-07; Phase 1 doesn't exercise the reinsert path but the test locks in the invariant.)
**Warning signs:** `psql -c "\d transactions"` shows the unique constraint without a `WHERE` clause.

### Pitfall 3: Currency code mapping at the wrong layer

**What goes wrong:** Numeric `currencyCode: 980` leaks into the DB (or worse, into the API response) because the importer "forgets" to map it. Now the schema has both `978` and `EUR` rows and rule engines have to handle both.
**Why it happens:** Mono returns numeric in every field; the temptation is to just `JSONB` it and move on.
**How to avoid:** `MonobankImporter._to_canonical(item)` is the ONLY place the numeric→alpha mapping happens. The `CanonicalTransaction` dataclass has `currency: str` typed as alpha. Anything reaching the repository is alpha. Add a `pyright`/`basedpyright` constraint on the column type to enforce.
**Warning signs:** A line in code like `transaction.currency = item["currencyCode"]` (no mapping). Detect with `grep -n 'currencyCode' src/finance_bro/db/`.

### Pitfall 4: Logging the Mono response body at INFO

**What goes wrong:** Bohdan turns on a debug session, copies the logs to a Slack thread for a friend, the friend now has Mono account IDs, balances, and (if the redaction filter missed it) the token. SC#5 fails.
**Why it happens:** httpx debug logging is verbose by default; FastAPI's access logs include the request body when `--log-level debug` is set.
**How to avoid:** (a) Set httpx's logger to WARNING in `core/logging.py`; (b) the redaction processor scrubs `event` strings via the token-shaped regex; (c) test asserts that after a full import cycle, `caplog.text` (or the captured structlog records at INFO) contains zero token-shaped substrings and zero `amount` field values; (d) document in README that `LOG_LEVEL=DEBUG` is for local dev only.
**Warning signs:** Any unit test that captures logs and asserts the request URL is logged in full.

### Pitfall 5: Mono per-account `id` collision assumption

**What goes wrong:** Code uses `source_tx_id` alone as the dedup key, then a jar transfer produces the same `id` on both legs (open question per STATE.md), and one leg silently overwrites the other.
**Why it happens:** Mono's docs are vague on uniqueness scope.
**How to avoid:** Composite `(account_id, source_tx_id)` from migration 0001 — already locked. Phase 1's open-question instrumentation: log a single-line audit at INFO when an import inserts a row whose `source_tx_id` already exists in the DB under a *different* `account_id`. This produces the empirical evidence needed to resolve the open question, without leaking any value.

### Pitfall 6: Bind-mount path drift across re-deploys

**What goes wrong:** README says `./data/postgres`, compose.yml says `./data/db`, user `mv`s the dir to match, Postgres restarts, can't find PG_VERSION, refuses to start.
**Why it happens:** Two-source-of-truth. README and compose.yml drift apart over time.
**How to avoid:** Single `${DATA_DIR}` env var with default `./data` declared in `.env.example`; compose.yml uses `${DATA_DIR}/postgres:/var/lib/postgresql/data`; README references `${DATA_DIR}` not the path. Add a smoke test (Phase 1 manual checklist): `docker compose down && docker compose up -d` — accounts and transactions must persist.

### Pitfall 7: Mono token in the URL (defense-in-depth violation)

**What goes wrong:** Some misguided code constructs `f"https://api.monobank.ua/personal/statement/{token}/..."` and now the token is in every access log line.
**Why it happens:** Confusion between Mono's URL parameters (`/{account}/{from}/{to}`) and the auth header (`X-Token`).
**How to avoid:** The httpx client construction sets `headers={"X-Token": token}` once at instantiation; route URLs are formatted from `account`, `from_ts`, `to_ts` only. Add a test: `respx_mock.calls[0].request.url` must NOT contain the token substring.

### Pitfall 8: Running migrations in a Uvicorn worker race

**What goes wrong:** With `--workers > 1`, both workers race to `alembic upgrade head` on startup; one wins, the other crashes on `relation already exists`.
**Why it happens:** "More workers = more throughput" reflex.
**How to avoid:** Single-user homelab — `--workers 1` is correct. But also: run migrations in a separate compose step (Dockerfile `CMD` is `alembic upgrade head && uvicorn ...`) and the entire `app` service is one container. Document this in the Dockerfile entrypoint.

### Pitfall 9: Mono `1 req/60s` is per token, not per endpoint

**What goes wrong:** Code defines two limiters (one for `client-info`, one for `statement`), each at 1/60s, both fire in the same minute → 429 on the second.
**Why it happens:** Mono's docs phrase the limit per-endpoint in places; community libs (notably python-monobank) have stale assumptions.
**How to avoid:** **One** `RateLimitGate` instance per token. Both `client-info` and `statement` calls use the same gate. Verified `[CITED: github.com/vitalik/python-monobank README]` — "If you use Personal API you may encounter 'Too Many Requests' error" with no per-endpoint distinction; per `[CITED: smaugfm/monobudget README]`, the limit is shared.

### Pitfall 10: `--autogenerate` skipping the partial unique index

**What goes wrong:** See Pitfall 2 — repeated for emphasis because it's the single most common Alembic miss. SQLAlchemy's `Index(..., unique=True, sqlite_where=...)` declarative form generates correctly under `--autogenerate` only on the dialect the constraint targets. For Postgres, use `postgresql_where`.

```python
# src/finance_bro/db/models.py
from sqlalchemy import Index, text
class Transaction(Base):
    __tablename__ = "transactions"
    # ... columns ...
    __table_args__ = (
        Index(
            "uq_transactions_account_source_tx",
            "account_id", "source_tx_id",
            unique=True,
            postgresql_where=text("NOT is_deleted"),
        ),
    )
```

## Code Examples

### `MonobankImporter.discover_accounts` (Mono → Canonical mapping at the boundary)

```python
# src/finance_bro/importers/monobank.py
# Source: github.com/vtopc/go-monobank field reference + python-monobank README
import httpx
from datetime import UTC, datetime
from typing import AsyncIterator
from .base import CanonicalAccount, CanonicalTransaction
from .currency_map import numeric_to_alpha
from .rate_limit import RateLimitGate

MONO_BASE = "https://api.monobank.ua"

class MonobankImporter:
    source_kind = "monobank"

    def __init__(self, token: str, gate: RateLimitGate):
        self._token = token
        self._gate = gate
        self._client = httpx.AsyncClient(
            base_url=MONO_BASE,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"X-Token": token},
        )

    async def discover_accounts(self) -> list[CanonicalAccount]:
        await self._gate.acquire(self._token)
        resp = await self._client.get("/personal/client-info")
        resp.raise_for_status()
        data = resp.json()
        out: list[CanonicalAccount] = []
        # accounts[] — cards (type=black/white/...) and FOPs (type=fop)
        for acc in data.get("accounts", []):
            kind = "mono.fop" if acc.get("type") == "fop" else "mono.card"
            out.append(CanonicalAccount(
                source_account_id=acc["id"],
                source_kind=kind,
                currency=numeric_to_alpha(acc["currencyCode"]),
                raw=acc,
            ))
        # jars[]
        for jar in data.get("jars", []):
            out.append(CanonicalAccount(
                source_account_id=jar["id"],
                source_kind="mono.jar",
                currency=numeric_to_alpha(jar["currencyCode"]),
                raw=jar,
            ))
        return out

    async def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]:
        await self._gate.acquire(self._token)
        from_ts = int(since.timestamp())
        to_ts = int(until.timestamp())
        resp = await self._client.get(
            f"/personal/statement/{source_account_id}/{from_ts}/{to_ts}"
        )
        resp.raise_for_status()
        for item in resp.json():
            yield CanonicalTransaction(
                source_tx_id=item["id"],
                source_account_id=source_account_id,
                occurred_at=datetime.fromtimestamp(item["time"], tz=UTC),
                amount_minor=int(item["amount"]),       # already kopecks
                currency=numeric_to_alpha(item["currencyCode"]),
                raw=item,
            )
```

### `currency_map.numeric_to_alpha` (one source of truth)

```python
# src/finance_bro/importers/currency_map.py
# Mono returns ISO 4217 numeric; downstream uses alpha.
# Source: ISO 4217 standard; verified field meaning via go-monobank docs.
_NUM_TO_ALPHA: dict[int, str] = {
    980: "UAH",
    840: "USD",
    978: "EUR",
    # Extend if FOP accounts surface other codes — not needed for Phase 1.
}

def numeric_to_alpha(code: int) -> str:
    try:
        return _NUM_TO_ALPHA[code]
    except KeyError as e:
        raise ValueError(f"Unknown ISO 4217 numeric code: {code}") from e
```

### `compose.yml` (DEP-01 + DEP-02 shape)

```yaml
# compose.yml
services:
  db:
    image: postgres:17-bookworm
    environment:
      POSTGRES_DB: finance_bro
      POSTGRES_USER: finance_bro
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in .env}
    volumes:
      - ${DATA_DIR:-./data}/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U finance_bro -d finance_bro"]
      interval: 5s
      timeout: 3s
      retries: 12
    restart: unless-stopped

  app:
    build: .
    user: "1000:1000"   # documented PUID/PGID for Synology/Unraid (DEP-01)
    environment:
      MONO_TOKEN: ${MONO_TOKEN:?set in .env}
      DATABASE_URL: postgresql+psycopg://finance_bro:${POSTGRES_PASSWORD}@db:5432/finance_bro
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    depends_on:
      db:
        condition: service_healthy
    ports:
      - "127.0.0.1:8000:8000"   # bind to localhost; Tailscale Funnel exposes if needed
    healthcheck:
      test: ["CMD", "curl", "-fs", "http://localhost:8000/api/health"]
      interval: 10s
      timeout: 5s
      retries: 6
    restart: unless-stopped
```

### `Dockerfile` (multi-stage)

```dockerfile
# Stage 1: builder
FROM python:3.13-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Stage 2: runtime
FROM python:3.13-slim-trixie AS runtime
RUN useradd -u 1000 -m app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=app:app . /app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn finance_bro.main:app --host 0.0.0.0 --port 8000 --workers 1"]
```

### `migrations/0001_walking_skeleton.py` (key fragments)

```python
# alembic/versions/0001_walking_skeleton.py
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None

def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_account_id", sa.Text, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("raw_payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("source_kind", "source_account_id",
                            name="uq_accounts_source"),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger,
                  sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_tx_id", sa.Text, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.false()),
        # Forward-looking columns (Phase 1 doesn't read them; later phases do)
        sa.Column("hold", sa.Boolean, nullable=False, server_default=sa.false()),       # ING-05
        sa.Column("category_id", sa.BigInteger, nullable=True),                         # CAT-04
        sa.Column("category_source", sa.Text, nullable=True),                           # CAT-04
        sa.Column("is_user_locked", sa.Boolean, nullable=False, server_default=sa.false()),  # CAT-04
        sa.Column("mcc", sa.Integer, nullable=True),                                    # Phase 4
        sa.Column("description", sa.Text, nullable=True),                               # Phase 6
        sa.Column("attributed_day", sa.Date, nullable=True),                            # Phase 3
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    # Composite idempotency key — partial unique index (ING-04 + ING-07 groundwork)
    op.create_index(
        "uq_transactions_account_source_tx",
        "transactions",
        ["account_id", "source_tx_id"],
        unique=True,
        postgresql_where=sa.text("NOT is_deleted"),
    )
    # Rate-limit gate state (ING-02)
    op.create_table(
        "mono_rate_state",
        sa.Column("token_hash", sa.Text, primary_key=True),
        sa.Column("last_acquired_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

def downgrade() -> None:
    op.drop_table("mono_rate_state")
    op.drop_index("uq_transactions_account_source_tx", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("accounts")
```

### `tests/conftest.py` (testcontainers Postgres fixture)

```python
# tests/conftest.py
# Source: testcontainers 4.x docs; pytest-asyncio 1.x docs.
import pytest_asyncio
from testcontainers.postgres import PostgresContainer
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from alembic.config import Config
from alembic import command
from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager
from finance_bro.main import app
from finance_bro.db.engine import set_engine

@pytest_asyncio.fixture(scope="session")
async def pg_url():
    with PostgresContainer("postgres:17-bookworm") as pg:
        url = pg.get_connection_url().replace("psycopg2", "psycopg")
        # Run alembic migrations against this DB
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield url

@pytest_asyncio.fixture
async def session_factory(pg_url):
    engine = create_async_engine(pg_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    set_engine(engine, factory)
    yield factory
    await engine.dispose()

@pytest_asyncio.fixture
async def client(session_factory):
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            yield ac
```

## Runtime State Inventory

> **Not applicable.** Phase 1 is a greenfield phase — no rename, refactor, or migration of pre-existing state. There is no prior runtime state to inventory.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.13 | Backend runtime | ✓ | 3.13.3 | — |
| uv | Local dev | ✓ | 0.9.26 | Bump to 0.11.x for Docker build (CI/Dockerfile pins 0.11.12). Local dev works on 0.9.26. |
| Docker | Compose deploy | ✓ | 25.0.3 | — |
| Node.js | Phase 6 only — NOT needed in Phase 1 | ✓ | 23.7.0 | — (deferred) |
| psql client | Optional (debugging only) | ✗ | — | `docker compose exec db psql ...` |
| Postgres 17 | Database | (containerized) | n/a | Pulled by `docker compose up`; image `postgres:17-bookworm` ~150 MB. |
| `api.monobank.ua` reachability | Live import | (network) | n/a | Tests use `respx` to mock; one manual smoke test in Phase 1 verification needs real network. |

**Missing dependencies with no fallback:** None for Phase 1.

**Missing dependencies with fallback:** `psql` not installed locally — fine, since `docker compose exec db psql -U finance_bro` is the documented path.

## Validation Architecture

> Phase enforces `workflow.nyquist_validation: true` (default — config does not disable). VALIDATION.md will be templated from this section.

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.3 + pytest-asyncio 1.3.0 (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/ -x --tb=short` |
| Full suite command | `uv run pytest tests/ -v` |
| Coverage (optional) | `uv run pytest --cov=src/finance_bro --cov-report=term-missing` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ING-01 | `MonobankImporter.discover_accounts()` parses `client-info` shape; numeric → alpha mapping at boundary | unit | `pytest tests/test_importer_currency_map.py -x` | ❌ Wave 0 |
| ING-01 | `MonobankImporter.fetch_statement()` produces CanonicalTransaction with verbatim `raw` | unit | `pytest tests/test_importer_statement.py -x` | ❌ Wave 0 |
| ING-01 | Token rides in `X-Token` header, NEVER in URL | unit | `pytest tests/test_importer_no_token_in_url.py -x` | ❌ Wave 0 |
| ING-02 | `RateLimitGate.acquire()` cannot fire twice within 60s | unit | `pytest tests/test_rate_limit_gate.py::test_within_window -x` | ❌ Wave 0 |
| ING-02 | `RateLimitGate` state persists across instance recreation (simulates restart) | unit | `pytest tests/test_rate_limit_gate.py::test_persists_across_restart -x` | ❌ Wave 0 |
| ING-02 | Two concurrent `acquire()` calls serialize; one waits | unit | `pytest tests/test_rate_limit_gate.py::test_concurrent_serialize -x` | ❌ Wave 0 |
| ING-03 | `POST /api/import` writes `raw_payload` JSONB verbatim | integration | `pytest tests/test_import_route.py::test_raw_payload_verbatim -x` | ❌ Wave 0 |
| ING-03 | `accounts` table populated with all kinds (card + jar + FOP) on first import (D-05) | integration | `pytest tests/test_import_route.py::test_all_accounts_persisted -x` | ❌ Wave 0 |
| ING-03 | `GET /api/transactions` returns rows with `amount_minor: int`, `currency: "XXX"`, full `raw_payload` (D-10) | integration | `pytest tests/test_transactions_route.py -x` | ❌ Wave 0 |
| ING-04 | Two imports of the same Mono `id` produce one row (`inserted=N` then `inserted=0, skipped_duplicates=N`) | integration | `pytest tests/test_idempotency.py -x` | ❌ Wave 0 |
| ING-04 | Soft-deleted row can be re-inserted (partial unique index respects `WHERE NOT is_deleted`) | unit | `pytest tests/test_partial_unique_index.py -x` | ❌ Wave 0 |
| ING-07 | `is_deleted` column defaults to `false`; `raw_payload` is never mutated by application code | unit | `pytest tests/test_schema_invariants.py -x` | ❌ Wave 0 |
| FX-01 | `amount_minor` stored as BIGINT signed minor units; currency stored as CHAR(3) alpha | unit | `pytest tests/test_money_invariants.py -x` | ❌ Wave 0 |
| FX-01 | Importer never produces `float`; round-trip int → DB → JSON → int | unit | `pytest tests/test_money_invariants.py::test_no_float_in_pipeline -x` | ❌ Wave 0 |
| OPS-01 | `MONO_TOKEN` is read once at startup from env; never written to DB or file | unit | `pytest tests/test_settings.py::test_token_env_only -x` | ❌ Wave 0 |
| OPS-04 | Full import cycle produces zero token-shaped substrings in INFO log output | integration | `pytest tests/test_log_redaction.py::test_no_token_in_logs -x` | ❌ Wave 0 |
| OPS-04 | Full import cycle produces zero `amount` values in INFO log output | integration | `pytest tests/test_log_redaction.py::test_no_amounts_in_logs -x` | ❌ Wave 0 |
| OPS-04 | `X-Token` header value never appears in any log record | integration | `pytest tests/test_log_redaction.py::test_no_x_token_header -x` | ❌ Wave 0 |
| DEP-01 | Migration `0001_walking_skeleton` round-trips: `upgrade head` → `downgrade base` → `upgrade head` clean | integration | `pytest tests/test_migrations.py -x` | ❌ Wave 0 |
| DEP-01 | `compose config` validates without errors (smoke test) | manual | `docker compose -f compose.yml config` | n/a — manual phase-gate check |
| DEP-02 | No auth middleware registered; `/docs` reachable without credentials | unit | `pytest tests/test_no_auth.py -x` | ❌ Wave 0 |

**Manual phase-gate validations (cannot be automated under 30s):**

- SC#1 — `docker compose up` starts the app, `/docs` opens, `MONO_TOKEN` paste works (env-var path). Run once before declaring phase done.
- SC#2 — Real Mono call via `POST /api/import` returns real card rows (smoke test against Bohdan's actual token).
- SC#5 — `docker logs $(docker compose ps -q app) | grep -E "(token|X-Token|amount)"` returns zero matches after a real import.

### Sampling Rate

- **Per task commit:** `uv run pytest tests/ -x --tb=short` (quick — fails fast on first error)
- **Per wave merge:** `uv run pytest tests/ -v` (full suite green)
- **Phase gate:** Full suite green + manual SC#1/SC#2/SC#5 + plan-checker review

### Wave 0 Gaps

Every test file listed in the requirements map is currently absent (greenfield). Wave 0 must:

- [ ] Install pytest + pytest-asyncio + testcontainers + respx + asgi-lifespan + freezegun via `uv add --dev`
- [ ] Create `tests/conftest.py` with `pg_url`, `session_factory`, `client` fixtures (per Code Examples)
- [ ] Create `tests/__init__.py` (empty) and `tests/fixtures/` for canned Mono response JSON
- [ ] Add Mono response fixtures: `client_info_minimal.json` (one card, one jar), `statement_two_items.json`
- [ ] Add `[tool.pytest.ini_options]` to `pyproject.toml`: `asyncio_mode = "auto"`, `testpaths = ["tests"]`
- [ ] Verify Docker daemon is running (testcontainers requires it) — document as a precondition

(All test FILES corresponding to the requirements map are gaps; they get created across Phase 1 task waves alongside the production code they exercise. Wave 0 only ensures the harness can run.)

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `psycopg2` synchronous + threadpool | `psycopg` 3 async + SQLAlchemy 2.0 async | psycopg 3.0 GA Oct 2021; SQLAlchemy 2.0 GA Jan 2023 | URL prefix is `postgresql+psycopg://` (NOT `postgresql+psycopg2://`). Common upgrade trap. |
| `pytz` | stdlib `zoneinfo` | Python 3.9+ | `zoneinfo.ZoneInfo("Europe/Kyiv")` only; ban `pytz`. (Phase 3 concern; column lands in Phase 1 schema.) |
| `requests` for HTTP | `httpx` `AsyncClient` | httpx 1.0 expected 2026 — using 0.28.x as latest stable | Same API surface; native async; no thread pool kludge. |
| `pip` + `requirements.txt` + `venv` | `uv` | uv 0.1 GA Feb 2024; mature by 0.5 in Sep 2024 | One binary handles deps + venv + lockfile + Python install. |
| `black` + `isort` + `flake8` | `ruff` | ruff 0.1 GA Aug 2023 | Single tool; faster; replaces 100+ plugins. |
| `mypy` | `basedpyright` (or `mypy 2.0`) | basedpyright fork stabilized 2024–2025 | Strict-mode default; better async-code analysis. |
| `forwardRef`-based React libs | React 19 ref-as-prop | React 19 GA Dec 2024 | Phase 6 concern; not Phase 1. |
| Alembic `--render-as-batch` for SQLite | Native ALTER for Postgres | Postgres switch resolved in research | Phase 1 uses Postgres exclusively; batch mode unused. |

**Deprecated/outdated (don't import):**

- `pytz` — replaced by `zoneinfo`.
- `psycopg2` / `psycopg2-binary` — replaced by `psycopg[binary,pool]` 3.x.
- `requests` — replaced by `httpx`.
- `py-moneyed` — last release 2022, abandoned. Hand-rolled `Money` dataclass instead.
- Celery / RQ / Arq for the importer — overkill for one-job-every-65s; not used until proven necessary.

## Project Constraints (from CLAUDE.md)

These are directives — they have the same authority as locked decisions in CONTEXT.md:

- **Privacy:** No third-party cloud for primary data. Phase 1 implication: no telemetry SDKs, no error-reporting SaaS, no analytics. `pyproject.toml` deps must be auditable; reject `sentry-sdk`, `posthog`, `mixpanel`, `segment`.
- **Tech stack:** Python backend, JS frontend. **No frontend in Phase 1** (deferred to Phase 6) — but reserve `frontend/` directory.
- **Deployment:** Docker on homelab/NAS. Single `docker compose up`-style deploy. Phase 1 must satisfy this on day one.
- **External API:** Monobank personal API, 1 request per 60 seconds per token. Hard rate limit. Drives the entire rate-gate design.
- **Single-user:** No multi-tenancy. Phase 1 implication: no `user_id` column anywhere; no auth; no session table.
- **Network-gated:** No app-level auth in v1. Phase 1 implication: FastAPI runs without auth middleware; `/docs` is open. Bind ports to `127.0.0.1` in compose so only Tailscale/LAN can reach the box.
- **Time horizon:** Solid MVP, ~1–2 months of evening work. Phase 1 = walking skeleton. **Resist any cross-cutting work that doesn't unblock the spine.** No premature interface, no premature optimization, no test gold-plating.
- **Scope discipline:** Visibility, not planning. Phase 1 has no spending math, no UAH rollup, no "this month" — those are later phases.
- **No `float` for money. No `requests`. No `psycopg2`. No `py-moneyed`. No Celery/RQ/Redis. No named Docker volumes for the DB. No `pytz`. No `forwardRef`-only React libs (irrelevant Phase 1).**

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `iso4217` PyPI package handles all currency codes Mono returns on FOP accounts | Standard Stack | LOW — fallback is the 3-entry hand-rolled dict; FOP-specific codes can be added if observed. |
| A2 | testcontainers SQLAlchemy URL substitution `psycopg2 → psycopg` works as written | Code Examples | LOW — easily corrected; alternative is to construct the URL manually from the container's host/port/db. |
| A3 | `respx` mocks the httpx async transport in 0.23.x with the same API surface as 0.22.x | Standard Stack | LOW — respx is stable; if API drift occurs, the test pattern adapts trivially. |
| A4 | Postgres `SELECT ... FOR UPDATE` on `mono_rate_state` provides the strongest atomicity for the read-then-write rate-gate pattern | Architecture Patterns | LOW — `[CITED]` to PostgreSQL 17 docs; standard pattern. |
| A5 | Mono's `client-info` response includes a `type` field on accounts that distinguishes FOP from personal cards | Code Examples (`MonobankImporter.discover_accounts`) | MEDIUM — go-monobank docs list `Type` on Account, but the value space (`black/white/platinum/.../fop`) is undocumented and may differ for FOP. Phase 1 instrumentation should log the observed `type` value on first run to confirm. |
| A6 | A 30-character regex (`[A-Za-z0-9_-]{30,}`) is a safe upper-bound shape for redacting Mono tokens in log messages | Code Examples (`_redact`) | LOW — false positives on long IDs in logs are acceptable (they get redacted unnecessarily). False negatives (real tokens leaking) are the risk; regex captures Mono's known ~40-char shape with margin. |
| A7 | psycopg 3.3.4 + SQLAlchemy 2.0.49 + Alembic 1.18.4 + Postgres 17 form a tested compatibility quad | Standard Stack | LOW — combinatorics verified by published release notes; major version pairs are standard. Pin exact versions to lock the matrix. |
| A8 | The `MONO_TOKEN` env var fits in standard environment limits (≤32 KB on Linux) | OPS-01 | NEGLIGIBLE — Mono tokens are ~40 chars. Mentioned only for completeness. |
| A9 | `respx_mock.calls[0].request.url` is a stringifiable URL whose substring search detects accidental token-in-URL bugs | Code Examples (test pattern) | LOW — respx exposes httpx `Request` objects; `str(req.url)` is a stable httpx API. |

**Note for downstream agents:** No Phase-1-blocking decisions are tagged ASSUMED. Every locked-decision item from CONTEXT.md is HIGH confidence. Most assumptions above are auxiliary (test tooling, regex tuning) and can be repaired without re-planning the phase.

## Open Questions

These are **observation points** for Phase 1 to record evidence on, not blockers:

1. **Mono `statementItem.id` global vs per-account uniqueness**
   - What we know: Composite key `(account_id, source_tx_id)` is defensive regardless of the answer.
   - What's unclear: Empirical evidence pending.
   - Recommendation: Log a single INFO line whenever an import inserts a row whose `source_tx_id` already exists under a *different* `account_id`. Resolves the question after one jar transfer is observed.

2. **FOP token: same personal token, or separate?**
   - What we know: Phase 1 uses one `MONO_TOKEN`. CONTEXT.md D-05 persists FOP rows if Mono returns them under this token.
   - What's unclear: If Bohdan has a FOP account, does it surface under the personal token, or does it require a separate FOP token?
   - Recommendation: After Bohdan's first real import, manually inspect `accounts` table: `SELECT source_kind, COUNT(*) FROM accounts GROUP BY source_kind`. If `mono.fop` is absent and Bohdan has a FOP account, raise to Phase 2 multi-token planning.

3. **Mono 429 response: `Retry-After` header present?**
   - What we know: python-monobank exposes a `TooManyRequests` exception but the README doesn't quote the response shape.
   - What's unclear: Does Mono include `Retry-After`? If yes, Phase 2's reactive backoff can use it; if no, conservative 60s is the answer.
   - Recommendation: Phase 1 doesn't *need* this — the gate prevents 429 entirely. But add a debug log entry on any 429 received that captures all response headers (after redaction) for empirical observation.

4. **Mono per-account `type` enum values (esp. for FOP)**
   - What we know: go-monobank documents `Type CardType` with values `black/white/platinum/...`. CONTEXT.md D-04 picks "first item with `type = card`" — actual mapping needs verification.
   - What's unclear: Is `type == "fop"` the correct discriminator, or is FOP detected via the presence of `IBAN` / `EDRPOU` fields?
   - Recommendation: Phase 1 implementation in `discover_accounts()` should branch on the documented values BUT log the raw `type` value at INFO for any account during the first real import so the team can validate the mapping.

## Security Domain

> `security_enforcement` is not configured in `.planning/config.json`. Treating as enabled (default).

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V1 Architecture | yes | Trust boundary documented (Tailscale/LAN). No app-level auth in v1 (DEP-02). README must document threat model. |
| V2 Authentication | no | Phase 1 has no app-level authentication by design (DEP-02). Reassess in v1.5 only if hosting model changes. |
| V3 Session Management | no | No sessions in Phase 1 (no auth). |
| V4 Access Control | partial | Network-level access control via host firewall + Tailscale. App-level authorization is N/A (single-user). Compose binds to `127.0.0.1`, not `0.0.0.0`, to prevent accidental WAN exposure on multi-NIC hosts. |
| V5 Input Validation | yes | Pydantic schemas validate all `POST /api/import` and `GET /api/*` request shapes. Mono response is parsed via Pydantic models; unknown fields preserved in `raw_payload` only. |
| V6 Cryptography | partial | Phase 1: no app-level crypto (token storage is env-var per D-01). Postgres `pg_hba.conf` requires `md5`/`scram-sha-256` for non-`localhost` connections (default in `postgres:17` image). |
| V7 Error Handling | yes | structlog redaction processor scrubs token and amount data from logs (OPS-04). FastAPI default error handler returns 500 without stack traces in production (`debug=False`). |
| V8 Data Protection | yes | `MONO_TOKEN` redacted in logs. `raw_payload` is JSONB (PII-bearing) but stays inside Postgres which is bind-mounted to user-owned disk per privacy constraint. |
| V9 Communications | partial | Egress to `api.monobank.ua` is HTTPS-only (httpx default). LAN-side traffic is HTTP — acceptable inside the Tailscale tunnel. |
| V10 Malicious Code | yes | All deps pinned via `uv.lock` (`uv sync --frozen`). pre-commit hook prevents accidental introduction of unvetted deps. |
| V11 Business Logic | partial | Idempotency (ING-04) is the only business-logic invariant in Phase 1; enforced by DB constraint, not code. |
| V12 Files / Resources | yes | Bind-mount `./data/postgres` only; no user-uploaded files in Phase 1. |
| V13 API and Web Service | yes | OpenAPI auto-generated by FastAPI; rate limiting at the *outbound* (Mono) side only, no inbound rate limiting (single-user LAN — acceptable). |
| V14 Configuration | yes | Settings via `pydantic-settings`; `.env.example` documents all required env vars; container runs as non-root UID 1000. |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Mono token leaked in logs | Information Disclosure | structlog redaction processor (default ON at INFO+); regex scrub; explicit test (OPS-04) |
| Mono token leaked in URL path | Information Disclosure | Use `X-Token` header only; respx-mocked test asserts URL never contains the token substring |
| Mono token leaked via `.env` committed to git | Information Disclosure | `.env` in `.gitignore`; only `.env.example` (with empty values) committed |
| `docker compose down -v` wipes data | Repudiation / Denial of Service | Bind-mount (not named volume) for Postgres data; README warns against `-v` |
| SQL injection in repos | Tampering | SQLAlchemy parameterized queries everywhere; never f-string SQL |
| Mono response body cached in browser localStorage | Information Disclosure | No frontend in Phase 1; Phase 6 will explicitly forbid localStorage caching of transactions (Pitfall 25) |
| Container runs as root, writes root-owned files into bind mount | Tampering | Dockerfile creates `app` user UID 1000; `compose.yml` sets `user: "1000:1000"` |
| Postgres password leaked into image | Information Disclosure | `POSTGRES_PASSWORD` is a runtime env var only; never `ENV` directive in Dockerfile |
| Multi-port WAN exposure | Spoofing / Information Disclosure | Compose binds `127.0.0.1:8000:8000` (not `0.0.0.0:8000:8000`); Tailscale exposes selectively |
| Migration partial-failure leaves DB wedged | Denial of Service | Migration 0001 is small, all-or-nothing; pre-flight backup hook is a Phase 7 concern (Pitfall 13) |
| Token-shaped substring in error message exposed via API 500 | Information Disclosure | FastAPI exception handlers return 500 with generic messages in production; structlog filter applies to error logs too |

## Sources

### Primary (HIGH confidence)

- [PyPI registry queries 2026-05-10] — fastapi 0.136.1, uvicorn 0.46.0, sqlalchemy 2.0.49, alembic 1.18.4, psycopg 3.3.4, pydantic 2.13.4, pydantic-settings 2.14.1, httpx 0.28.1, structlog 25.5.0, tenacity 9.1.4, pytest 9.0.3, pytest-asyncio 1.3.0, ruff 0.15.12, uv 0.11.12, basedpyright 1.39.3, iso4217 1.16.20260101, pre-commit 4.6.0, testcontainers 4.14.2, respx 0.23.1, asgi-lifespan 2.1.0, freezegun 1.5.5, anyio 4.13.0
- [PostgreSQL 17 — Explicit Locking](https://www.postgresql.org/docs/17/explicit-locking.html) — `SELECT ... FOR UPDATE` row-level locking semantics; advisory locks comparison
- [`go-monobank` package — type definitions](https://pkg.go.dev/github.com/vtopc/go-monobank) — Account/Jar/Transaction (StatementItem) struct fields; canonical Mono API field reference
- [.planning/research/SUMMARY.md] — pre-existing project research (HIGH confidence, dated 2026-05-10); Conflict 1 (DB choice → Postgres) and Conflict 2 (timestamp → only `time`) resolved upstream
- [.planning/research/STACK.md] — pinned versions, version compatibility matrix
- [.planning/research/ARCHITECTURE.md] — modular monolith shape, importer port, repository pattern, schema entity names
- [.planning/research/FEATURES.md] — Mono API quirks shaping feature design (rate limit, 31-day window, hold semantics, currency model)
- [.planning/research/PITFALLS.md] — Pitfalls 1, 2, 3, 4, 11, 12 directly applicable to Phase 1
- [CLAUDE.md] — full project tech-stack table, version compatibility notes, anti-pattern list

### Secondary (MEDIUM confidence)

- [vitalik/python-monobank README](https://github.com/vitalik/python-monobank) — confirms `TooManyRequests` exception, statement field shape (`id`, `amount`, `balance`, `cashbackAmount`, `commissionRate`, `currencyCode`, `description`, `hold`, `mcc`, `operationAmount`, `time`); rate-limit guidance is shared-per-token
- [siomochkin/monobank-open-api-documentation] — community mirror confirming 1-req/60s and 31d+1h max window (URL was 404 at fetch time; fact corroborated by python-monobank and go-monobank)
- [vergilet/monobank Ruby](https://vergilet.github.io/monobank/) — independent confirmation of API field shapes
- [smaugfm/monobudget](https://github.com/smaugfm/monobudget) — Mono-specific transfer detection; rate-limit shared across endpoints

### Tertiary (LOW confidence — flagged for empirical validation)

- Open question 1 (`statementItem.id` per-account vs global uniqueness) — STATE.md flags this for empirical validation in Phase 1
- Open question 3 (Mono 429 `Retry-After` header presence) — STATE.md flags for empirical observation
- A5 in Assumptions Log (Mono `type` enum values) — needs validation against Bohdan's actual `client-info` response

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH — every version verified against PyPI on 2026-05-10
- Architecture: HIGH — pre-existing research is HIGH-confidence and CONTEXT.md locks the deviations
- Pitfalls: HIGH — pulled from `.planning/research/PITFALLS.md` (HIGH confidence) plus Postgres-doc verification of the rate-gate pattern
- Mono API mechanics: HIGH on field shape (cross-validated across go-monobank, python-monobank, vergilet/monobank); MEDIUM on 429 response shape (open question)
- Tests/Validation Architecture: HIGH on framework selection and command shape; the test FILES are gaps to be created in Wave 0
- Security Domain: HIGH on the controls list; the threat model is documented and constrained by single-user + LAN-only

**Research date:** 2026-05-10
**Valid until:** 2026-06-09 (30 days — stack is stable and no library is on a fast-moving release cycle)

## RESEARCH COMPLETE
