# Phase 1: First Real Transaction - Context

**Gathered:** 2026-05-10
**Status:** Ready for planning

<domain>
## Phase Boundary

The walking skeleton for finance-bro: a working spine that takes a Mono token from the environment, polls one Mono card via the rate-limited importer, and exposes the resulting transaction rows over a JSON API — end-to-end, on the canonical schema invariants (BIGINT minor units, composite idempotency key, single token-bucket gate, log redaction).

This phase ships the project skeleton (compose file + Postgres + FastAPI + Alembic), the first migration with the correctness invariants baked in, the Mono importer port + adapter, the rate-limit gate, the import endpoint, and the read endpoint that proves the round-trip worked. Every line of code in Phase 1 must serve that spine. No categorization, no FX, no scheduler, no UI — all of those have their own phase.

</domain>

<decisions>
## Implementation Decisions

### Token entry surface

- **D-01:** Token enters the running app via the `MONO_TOKEN` environment variable only. No HTML form, no `POST /api/token`, no DB row for the token, no encryption code in Phase 1. The token never touches disk inside the app — the `.env` file (or compose env) is the at-rest substrate. This satisfies **OPS-01** (token at rest) via the filesystem + LAN/Tailscale trust boundary that the project already accepts; encryption-in-DB would be theatre when the master key would have to live in the same env. Rotation = edit `.env` + `docker compose up -d`.
- **D-02:** Manual import is triggered by `POST /api/import` (curl-able from the LAN). No scheduler in Phase 1 — APScheduler lands in Phase 2. The endpoint takes no body in Phase 1 (the polled account is fixed by D-04).
- **D-03:** Token validation is **lazy** — on the first `POST /api/import` the importer calls `/personal/client-info` (using one rate-limit slot), persists the discovered accounts, then proceeds to call `/personal/statement/...` after the gate releases. App startup is silent; no rate-budget burned just to verify the token is valid before the user has asked for anything.

### Account pick in Phase 1

- **D-04:** The polled account in Phase 1 is **the first item with `type = card`** in the `/personal/client-info` response. Zero config — Bohdan does not need to know account IDs. Subsequent imports re-poll the same card. Multi-card / round-robin is Phase 2's problem.
- **D-05:** When `client-info` is fetched, **all** accounts Mono returned are persisted to the `accounts` table — cards, jars, and any FOP accounts. `source_kind` distinguishes them (`mono.card` / `mono.jar` / `mono.fop`). Only the picked card is polled in Phase 1, but the rest of the schema is honest from day one and Phase 2's round-robin doesn't need a re-discovery migration.
- **D-06:** Account discovery is a **one-shot on first import**. After the initial `client-info` call, accounts are read from the DB on every subsequent import; `client-info` is not re-called automatically. This protects the rate-limit budget — every additional `client-info` call is a slot that could have been a statement call. A manual refresh endpoint is deferred to Phase 2+.

### API surface scope

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

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents (researcher, planner, executor) MUST read these before planning or implementing.**

### Project framing
- `.planning/PROJECT.md` — Core Value, constraints, key decisions, in-scope vs out-of-scope
- `.planning/REQUIREMENTS.md` — v1 requirement IDs and per-phase mapping (Phase 1 owns: ING-01, ING-02, ING-03, ING-04, ING-07, FX-01, OPS-01, OPS-04, DEP-01, DEP-02)
- `.planning/ROADMAP.md` — Phase 1 section: goal, success criteria, requirements, notes/risks (especially Pitfalls 1, 3, 4, 11)
- `.planning/STATE.md` — accumulated decisions, open questions explicitly tagged for Phase 1 resolution
- `CLAUDE.md` — full stack table, version compatibility notes, "what NOT to use" list

### Research (HIGH confidence, dated 2026-05-10)
- `.planning/research/SUMMARY.md` — TL;DR of stack/architecture/pitfalls; conflicts resolved (Postgres>SQLite, `time` only no `operationDate`)
- `.planning/research/STACK.md` — pinned versions and library choices (FastAPI 0.136, SQLAlchemy 2.0.49, psycopg 3.3.4, Postgres 17, APScheduler 3.11.2, httpx 0.28.1)
- `.planning/research/ARCHITECTURE.md` — modular monolith shape, importer port, canonical schema entity names (`accounts`, `transactions`, `transaction_links`, `categories`, `rules`, `fx_rates`, `import_runs`)
- `.planning/research/FEATURES.md` — Mono API field shapes, ISO numeric→alpha currency map, rate-limit semantics
- `.planning/research/PITFALLS.md` — Phase 1 landmines: float for money (#1), composite idempotency key (#3), shared-token rate gate (#4), SQLite-on-NFS (#11), named volume vs bind mount (#12)

### External (no auth required, fetch on demand)
- Monobank Open API: https://api.monobank.ua/docs/index.html — `/personal/client-info` and `/personal/statement/{account}/{from}/{to}` shapes
- siomochkin/monobank-open-api-documentation: rate-limit confirmation (1 req / 60s shared across endpoints per token)
- Storing currency values best practices: https://cardinalby.github.io/blog/post/best-practices/storing-currency-values-data-types/

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- None — greenfield repo. Phase 1 establishes every convention used by later phases.

### Established Patterns
- None in code yet. The patterns Phase 1 *introduces* (and that Phases 2–7 must follow):
  - Importer port (`ImporterProtocol.pull(since: datetime) -> list[RawTxn]`) at `src/finance_bro/importers/base.py`; `MonobankImporter` is the only adapter in v1.
  - Repository pattern in `src/finance_bro/db/` — all SQL lives behind repos; routes/services never import `sqlalchemy` directly.
  - `Money(Decimal, currency)` value object at the application edge; raw `BIGINT` + `CHAR(3)` in DB; `Decimal` only when arithmetic is needed; never `float`.
  - Single owner of rate-limit budget (`MonobankImporter`) — every Mono caller routes through it.

### Integration Points
- Phase 1 *is* the integration point for everything that follows. Schema extensions in later phases are migrations on the tables created here. The importer port is the seam where Phase 2's scheduler attaches and where future PrivatBank/Wise importers slot in.

</code_context>

<specifics>
## Specific Ideas

- **Walking-skeleton discipline.** Every line in Phase 1 should make the spine real, not the long tail. If a feature can be added by a one-line migration in a later phase without rewrites, it does not belong in Phase 1. If it requires a hot-table column added later (e.g., `is_user_locked`), it lands in the first migration as a nullable/default column even though Phase 1 never reads it.
- **Composite key in the FIRST migration.** Per ROADMAP.md Phase 1 Pitfall 3: the unique index on `(account_id, source_tx_id)` lands in migration 0001, not retrofitted. Use a partial unique index (`WHERE NOT is_deleted`) so soft-deleted rows don't block re-inserts.
- **Single rate-limit gate in code on day one.** Per Pitfall 4: token bucket lives in one place, owned by `MonobankImporter`, persists last-acquired-at to Postgres across restarts. Implement before any business logic — no temporary "skip rate-limit for tests" path.
- **Postgres bind-mount, never named volume.** `./data/postgres:/var/lib/postgresql/data` per Pitfall 12 — named volumes get wiped by `docker compose down -v` and are invisible in the NAS file browser.
- **Log redaction is opt-out, not opt-in.** Token, `X-Token` header, and `amount_minor` are masked at INFO+ by default. SC#5 explicitly requires zero hits for the token, header, or amount values in `docker logs` after a successful import.
- **Mono `time` is the only timestamp.** Stored as UTC `TIMESTAMPTZ`. There is no `operationDate` in the API; the `attributed_day` (Europe/Kyiv calendar date) is computed on read in Phase 3 and beyond.
- **Numeric → alpha currency at the boundary.** Mono returns `currencyCode: 980` etc. The importer maps to `UAH`/`USD`/`EUR` before any row is constructed; downstream code never sees the numeric code.
- **Open Questions to resolve empirically in Phase 1** (per STATE.md):
  1. Is Mono `statementItem.id` globally unique or per-account scope? — composite key is defensive; observe whether a jar transfer produces the same `id` on both sides.
  2. FOP token: same personal token or separate? — observe whether FOP accounts appear in `client-info` response.
  3. Mono 429 response shape: includes `Retry-After`? — observe in a deliberate over-poll test.

</specifics>

<deferred>
## Deferred Ideas

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

</deferred>

---

*Phase: 01-first-real-transaction*
*Context gathered: 2026-05-10*
