# Phase 1: First Real Transaction - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in `01-CONTEXT.md` — this log preserves the alternatives considered.

**Date:** 2026-05-10
**Phase:** 01-first-real-transaction
**Areas discussed:** Token entry surface, Account pick in Phase 1, API surface scope
**Areas presented but skipped by user:** Schema groundwork scope (deferred to Claude's discretion)

---

## Token entry surface

### Q1 — Token entry mechanism

| Option | Description | Selected |
|--------|-------------|----------|
| HTML form on /setup (Recommended) | Walking-skeleton-faithful: matches SC#1 'paste his Mono token, click import' literally. Adds minimum frontend (one form + status text). Token validated against /personal/client-info on submit. | |
| Env var only (MONO_TOKEN) | Zero frontend in Phase 1. Token comes from compose env. Rotate = edit .env + docker compose up -d. Faster to ship; defers all UI to Phase 6. | ✓ |
| Both — env-var bootstrap + form override | Env var seeds the first run if present; UI lets you rotate without restart. More moving parts but covers both NAS-restart and rotate-without-restart cases. | |

**User's choice:** Env var only (MONO_TOKEN)
**Effect:** Defers all frontend work to Phase 6 (UI-01..05). No `/setup` page, no `POST /api/token`, no client-side React stub in Phase 1.

### Q2 — Import trigger

| Option | Description | Selected |
|--------|-------------|----------|
| POST /api/import (Recommended) | Curl-able. Keeps the explicit 'click' from SC#1, satisfies SC#3 (two rapid POSTs = no duplicates), no scheduler required yet. Phase 2 layers APScheduler on top. | ✓ |
| Auto-import on startup only | App boots → validates token → polls one account → done. Zero API surface for triggering. Re-testing SC#3 requires a container restart. | |
| Both — startup auto + POST /api/import | Auto-import on first boot for instant gratification, POST endpoint for re-trigger. Two moving parts; cognitive overhead about what fired when. | |

**User's choice:** POST /api/import (Recommended)

### Q3 — Token at-rest storage

| Option | Description | Selected |
|--------|-------------|----------|
| Env-only, no DB row (Recommended) | Token lives only in MONO_TOKEN env var. Never written to DB. OPS-01 satisfied by filesystem control + LAN/Tailscale boundary. Rotate = edit .env + restart. Zero crypto code in Phase 1. | ✓ |
| Env on first boot, encrypted in DB after | Env seeds on first run, app encrypts (Fernet, key from APP_SECRET_KEY env) and persists to DB; reads from DB afterward. Adds crypto dependency + migration row + key-management story. | |
| Defer OPS-01 entirely — env var, no encryption | Mark OPS-01 partially satisfied; encryption-at-rest moves to Phase 7. Bare env. Drops a phase requirement explicitly. | |

**User's choice:** Env-only, no DB row (Recommended)
**Notes:** The chosen option satisfies OPS-01 because the threat model (LAN + Tailscale + filesystem control) makes DB-row encryption with an env-resident key effectively theatre. Documented in `01-CONTEXT.md` D-01.

### Q4 — Token validation cadence

| Option | Description | Selected |
|--------|-------------|----------|
| Lazily on first POST /api/import (Recommended) | App starts silent. First import does client-info → persist accounts → statement call (after the 60s gate). One round-trip per slot. SC#1 'validate within one rate-limit slot' satisfied because the slot is the import slot. | ✓ |
| At startup, fail-fast if invalid | Container boots → client-info → logs OK or errors out. Discovers and caches accounts before user touches anything. Costs one rate slot on every container restart. | |
| Both — startup ping + before every import | Belt-and-suspenders. Burns rate budget aggressively (every poll cycle costs 2 slots). | |

**User's choice:** Lazily on first POST /api/import (Recommended)

---

## Account pick in Phase 1

### Q1 — How is the polled account selected?

| Option | Description | Selected |
|--------|-------------|----------|
| Auto-pick first card (Recommended) | Zero config. App takes the first item with type=card from client-info and polls it. Other accounts still discovered. | ✓ |
| POST /api/import?account_id=... (explicit) | Bohdan calls GET /api/accounts to see IDs, then POSTs the one he wants. More clicks; matches a future multi-account UX. | |
| Env var MONO_PRIMARY_ACCOUNT_ID | Hard-code the account ID at deploy time. Restart to switch. Brittle (account IDs are opaque Mono strings). | |
| Poll all accounts (drop 'one account') | Forget SC#2's wording — round-robin across all discovered accounts. Pulls Phase 2's scheduler logic forward. | |

**User's choice:** Auto-pick first card (Recommended)

### Q2 — Accounts table population

| Option | Description | Selected |
|--------|-------------|----------|
| All accounts (cards + jars + FOP) (Recommended) | Schema honesty. Every account Mono returned lands as a row — source_kind = mono.card / mono.jar / mono.fop. Only the first card is polled in Phase 1. Phase 2's round-robin doesn't need a re-discovery migration. | ✓ |
| Only the picked card | One row, one polled account. Smaller blast radius for Phase 1, but Phase 2 has to re-discover and backfill accounts. | |
| No accounts table yet — store account_id on transactions only | Skip the accounts table for Phase 1. Violates ING-03's account modeling and breaks the (account_id, source_tx_id) FK shape from day one. | |

**User's choice:** All accounts (cards + jars + FOP) (Recommended)

### Q3 — Account discovery cadence

| Option | Description | Selected |
|--------|-------------|----------|
| Once on first import only (Recommended) | Discovery happens during the first POST /api/import. After that, accounts are read from DB. Saves rate-limit budget. New accounts (e.g., a new card) won't appear until manual refresh — acceptable for Phase 1. | ✓ |
| Every POST /api/import | Every import burns 2 rate slots: one for client-info, one for statement. Halves your effective polling rate forever. | |
| Manual GET /api/accounts/refresh endpoint | Discovery runs once on first import, plus on demand when Bohdan curls a refresh endpoint. Needs one extra route in Phase 1. | |

**User's choice:** Once on first import only (Recommended)

---

## API surface scope

### Q1 — Pagination/filter shape on GET /api/transactions

| Option | Description | Selected |
|--------|-------------|----------|
| None — plain list, ordered by time desc (Recommended) | Return all rows for the polled account, no params. Volumes are tiny in Phase 1. Phase 6 (UI-02) is when cursor pagination + search land. | ✓ |
| ?limit=N, default 100 | One simple knob — cap response size without cursor machinery. Reasonable middle ground. | |
| Full cursor pagination from day one | Build the Phase 6 shape (?cursor=, ?limit=, X-Next-Cursor header) right now. | |
| ?account_id filter (no pagination) | Plain list with an account filter. Useful when Phase 2 widens to multiple accounts. | |

**User's choice:** None — plain list, ordered by time desc (Recommended)

### Q2 — POST /api/import response shape

| Option | Description | Selected |
|--------|-------------|----------|
| Sync, 200 + JSON summary (Recommended) | Blocks until the rate gate releases and the statement call returns. Returns {polled_account_id, statement_count, inserted, skipped_duplicates}. Up to ~60s wait. Single-user homelab — the wait is fine. | ✓ |
| Async, 202 + run_id, poll GET /api/imports/{run_id} | Kicks a background task, returns immediately with a job ID. Pulls Phase 2's import_runs table forward. | |
| Sync 204 No Content (no JSON body) | Minimal — just blocks until done, returns 204. Caller has to GET /api/transactions to see what happened. | |
| Hybrid: sync if rate-slot free, async + 202 if blocked | Best of both. Adds branching logic and an import_runs table to Phase 1. | |

**User's choice:** Sync, 200 + JSON summary (Recommended)

### Q3 — Supporting endpoints (multi-select)

| Option | Description | Selected |
|--------|-------------|----------|
| GET /api/health (Recommended) | Plain liveness check returning {status: 'ok', db: 'ok'}. Used by Docker compose healthcheck. | ✓ |
| GET /api/accounts (Recommended) | Lists discovered accounts (cards/jars/FOPs) from the local DB. Confirms client-info worked. | ✓ |
| GET /api/import/status | Last successful poll, last error, 401/429 distinction. ING-08 explicitly belongs to Phase 2. | |
| Auto-generated /docs (OpenAPI Swagger UI) | FastAPI default. Free Swagger UI at /docs lets you click around the API surface in a browser. | ✓ |

**User's choice:** GET /api/health, GET /api/accounts, /docs (Swagger UI). Skipped: /api/import/status (left to Phase 2 per ING-08).

### Q4 — Transaction row shape

| Option | Description | Selected |
|--------|-------------|----------|
| SC#2 fields + traceability (Recommended) | id, account_id, source_tx_id, amount_minor, currency, time (UTC), raw_payload. Matches SC#2 literally and adds just enough to debug duplicates. | ✓ |
| Above + description, mcc, hold, attributed_day | Adds the most useful debugging columns plus an attributed_day computed on read. Closer to Phase 6 needs; pulls Phase 2/3 columns forward. | |
| Full row — every column on transactions | Echo the whole table. Fastest to write but exposes raw schema; Phase 6 will tighten it later. | |

**User's choice:** SC#2 fields + traceability (Recommended)

---

## Claude's Discretion

The user did not select **Schema groundwork scope** in the gray-area selection step. The shape of the first Alembic migration is therefore Claude's call within the framing already established by PROJECT.md, ROADMAP.md, and `research/`. The chosen approach (documented in `01-CONTEXT.md` Claude's Discretion):

- First migration ships `accounts` and `transactions` only. Tables `categories`, `rules`, `fx_rates`, `transaction_links`, `import_runs` are deferred to their owning phases.
- Forward-looking columns on `transactions` that are painful to retrofit are added now: `hold`, `category_id`, `category_source`, `is_user_locked`, `mcc`, `description`, `attributed_day`. Phase 1 doesn't read or write them; later phases simply backfill.
- This is asymmetric on purpose: cheap-to-add tables get added later, expensive-to-add columns on the hot table get added now.

Other discretionary choices documented in CONTEXT.md:

- **Rate-limit bucket persistence** — single-row Postgres table with `SELECT ... FOR UPDATE`.
- **Project layout** — `src/finance_bro/{api,core,db,importers,services}/`.
- **Log redaction** — `structlog` + custom processor masking token, `X-Token`, and `amount_minor` at INFO+.
- **Mono numeric → ISO alpha currency** mapping at the importer boundary.
- **Timezone** — `time` stored as UTC `TIMESTAMPTZ`; `attributed_day` column nullable in Phase 1.

## Deferred Ideas

Captured in CONTEXT.md `<deferred>` section. Briefly:

- HTML form for token entry / rotation UI — not in v1 roadmap
- Token encryption-at-rest in DB (Fernet/NaCl) — moot under env-only storage
- Multi-account polling round-robin — Phase 2 (ING-05/06)
- Manual `POST /api/accounts/refresh` — Phase 2+
- Cursor pagination + filter + search on `/api/transactions` — Phase 6 (UI-02)
- Async `import_runs` job tracking — Phase 2
- `GET /api/import/status` — Phase 2 (ING-08)
- Frontend (React + Vite + Tailwind + shadcn/ui) — Phase 6
- Daily `pg_dump` backup + restore drill — Phase 7 (OPS-02)
