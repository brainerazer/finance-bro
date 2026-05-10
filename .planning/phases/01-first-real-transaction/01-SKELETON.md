# Walking Skeleton — finance-bro

**Phase:** 1 — First Real Transaction
**Generated:** 2026-05-10

## Capability Proven End-to-End

A user on the LAN runs `docker compose up`, opens `http://localhost:8000/docs`, calls `POST /api/import` once with `MONO_TOKEN` set in `.env`, and within ~65 seconds calls `GET /api/transactions` and sees real Monobank `statementItem` rows from their first card — each row carrying `amount_minor` (BIGINT signed minor units), `currency` (ISO-4217 alpha), and verbatim `raw_payload` JSON. A second `POST /api/import` returns `inserted=0, skipped_duplicates=N` and produces zero duplicate rows. `docker logs` at INFO level shows zero hits for the Mono token, the `X-Token` header, or any transaction `amount` value.

## Architectural Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Backend framework | FastAPI 0.136.1 + Uvicorn 0.46.0 (`--workers 1`) | Locked by CLAUDE.md / RESEARCH.md. Async-native, free OpenAPI, single-user keeps `--workers 1` correct (Pitfall 8). |
| Database | PostgreSQL 17 (`postgres:17-bookworm`) in compose; bind-mount `./data/postgres` | Locked by D-05/STATE.md. Required for JSONB, partial unique index, `SELECT ... FOR UPDATE`. Bind-mount guards against `docker compose down -v` (Pitfall 12). |
| ORM / migrations | SQLAlchemy 2.0.49 + Alembic 1.18.4 + psycopg 3.3.4 (`postgresql+psycopg://`) | Locked. NOT `psycopg2` — common upgrade trap. |
| Money model | `amount_minor` BIGINT signed minor units + `currency` CHAR(3) ISO-4217 alpha. `Decimal` only at FX edges (Phase 3). Never `float`. | FX-01 + Pitfall 1. JSON exposes `amount_minor` as plain `int` (D-10). |
| Idempotency key | Composite partial unique index `(account_id, source_tx_id) WHERE NOT is_deleted` in migration `0001_walking_skeleton` | ING-04 + Pitfall 2 + Pitfall 10. Hand-written DDL via `op.create_index(..., postgresql_where=text("NOT is_deleted"))`. |
| Rate-limit gate | Single-row Postgres table `mono_rate_state(token_hash, last_acquired_at)` mutated under `SELECT ... FOR UPDATE`; owned by `MonobankImporter`; persisted across restarts | ING-02 + Pitfall 1/4/9. NOT in-memory. Optimistic claim on the *next* slot so concurrent acquirers serialize. Single gate per token (NOT per endpoint). |
| Token storage | `MONO_TOKEN` env var only; read once at startup via `pydantic-settings`; never persisted to DB or filesystem | D-01 + OPS-01. Rotation = edit `.env` + `docker compose up -d`. No HTML form, no `POST /api/token`. |
| Mono auth transport | `X-Token` header on every request; never in URL | OPS-04 + Pitfall 7. Defense in depth via redaction processor scrubbing token-shaped substrings from log messages. |
| Currency mapping | Numeric → alpha at importer boundary in `importers/currency_map.py`; downstream code never sees `currencyCode: 980` | Pitfall 3. Single source of truth. |
| Account discovery | Lazy: first `POST /api/import` calls `/personal/client-info` once, persists ALL accounts (cards/jars/FOPs per D-05) with `source_kind`. Subsequent imports read accounts from DB. | D-03 + D-05 + D-06. Protects rate budget. |
| Polled account in Phase 1 | First account with Mono `type == "card"` (and not `"fop"`) — fixed by D-04 | Phase 2 owns multi-account round-robin. |
| Import endpoint | `POST /api/import` — synchronous, no body. Blocks up to ~60s on the gate. Responds `{polled_account_id, statement_count, inserted, skipped_duplicates}`. | D-08. SC#3 testable from response numbers. |
| Read endpoint | `GET /api/transactions` — flat list ordered by `time DESC`, no pagination/filtering. Each row exposes `id, account_id, source_tx_id, amount_minor, currency, time (ISO-8601), raw_payload`. | D-07 + D-10. |
| Supporting endpoints | `GET /api/health` (`{"status":"ok","db":"ok"}` for compose healthcheck), `GET /api/accounts`, `/docs` (Swagger UI, auth-free) | D-09. |
| Auth | None at app layer (DEP-02). Compose binds `127.0.0.1:8000:8000`; LAN/Tailscale is the trust boundary. | Locked. No middleware, no cookies, no sessions, `/docs` open. |
| Logging | structlog 25.5.0 with redaction processor as default. At INFO+, scrubs `token`/`X-Token`/`amount*` keys + token-shaped substrings in `event` strings. DEBUG bypasses redaction. | OPS-04 + Pitfall 4. Default-on; opt-out via `LOG_LEVEL=DEBUG`. |
| Test infrastructure | pytest 9.0.3 + pytest-asyncio 1.3.0 (`asyncio_mode = "auto"`) + testcontainers 4.14.2 (real `postgres:17-bookworm`) + respx 0.23.1 (httpx mocking) + asgi-lifespan 2.1.0 + freezegun 1.5.5 | RESEARCH.md Validation Architecture. NEVER SQLite-as-test-DB — would silently pass while production breaks (JSONB, partial indexes, `FOR UPDATE`). |
| Container shape | Multi-stage `python:3.13-slim-trixie` + `uv 0.11.12`. Dockerfile entrypoint: `alembic upgrade head && uvicorn ...`. Runs as UID 1000 (documented PUID/PGID). | DEP-01 + Pitfall 8. |
| Directory layout | `src/finance_bro/{api,core,db,importers,services}/` Python package. `alembic/` at repo root. `tests/` at repo root. `frontend/` reserved-but-empty (Phase 6). | Per RESEARCH.md Project Structure. |
| External egress | `api.monobank.ua` (HTTPS) only in Phase 1. No NBU, no telemetry, no analytics. | Privacy constraint + OPS-05 (full list documented in Phase 7). |

## Stack Touched in Phase 1

- [x] Project scaffold — `pyproject.toml` (uv), `compose.yml`, `Dockerfile`, `.env.example`, `alembic.ini`, `tests/conftest.py`, ruff + basedpyright + pre-commit configured
- [x] Routing — FastAPI app with `/api/health`, `/api/accounts`, `/api/transactions`, `POST /api/import`, `/docs`
- [x] Database — Real read AND write via Alembic migration `0001_walking_skeleton` (`accounts`, `transactions`, `mono_rate_state`); `INSERT ... ON CONFLICT DO NOTHING`; `GET /api/transactions` SELECT
- [x] UI — Phase 1 has no React frontend (deferred to Phase 6). Swagger UI at `/docs` is the human entry point — `POST /api/import` button + `GET /api/transactions` button drive the spine
- [x] Deployment — `docker compose up` brings up `app` + `db`; healthchecks gate `app` on `db`; bind-mounted Postgres data; documented in README

## Out of Scope (Deferred to Later Slices)

This list prevents future phases from re-litigating Phase 1's minimalism. Anything below is **forbidden** in Phase 1 work.

- React + Vite + Tailwind + shadcn/ui frontend → Phase 6 (UI-01..05)
- HTML token-paste form / `POST /api/token` endpoint / token rotation UI / token encryption-at-rest in DB → not on roadmap (D-01, env-var only)
- APScheduler / automatic 60s polling / 12-month backfill / hold semantics → Phase 2 (ING-05/06/08)
- `GET /api/import/status` (last poll, last error, 401/429 distinction) → Phase 2 (ING-08)
- `import_runs` table / async import / cursor resumability → Phase 2
- `fx_rates` table / NBU fetcher / UAH rollup join / `attributed_day` derivation → Phase 3 (FX-02/03/04)
- `categories` + `rules` tables / categorizer engine / MCC taxonomy / diff-preview-on-history → Phase 4 (CAT-01..05)
- `transaction_links` table / transfer + refund pairing → Phase 5 (REC-01/02)
- Cursor pagination + filter + search + quick-recategorize on `/api/transactions` → Phase 6 (UI-02/03)
- Manual edit / merge / split / cash transactions (`source = manual_cash`) → Phase 6 (MAN-01..03)
- Daily `pg_dump` cron / restore drill / CSV import / CSV+JSON export / `/about` egress page → Phase 7 (OPS-02/03/05)
- Multi-account round-robin polling / multi-token (FOP separate token) handling → Phase 2+
- Manual `POST /api/accounts/refresh` to re-fetch `client-info` → Phase 2+
- `category_id`, `category_source`, `is_user_locked`, `mcc`, `description`, `attributed_day`, `hold` are present as **forward-looking columns** in migration 0001 but **not read or written** by any Phase 1 code path

## Subsequent Slice Plan

Each later phase adds a vertical user-visible capability without altering Phase 1's architectural decisions:

- **Phase 2 — Reliable Sync:** APScheduler in-process job per token at 65s; `import_runs` for resumable backfill; `hold` semantics; `GET /api/import/status` (401 vs 429 distinguished).
- **Phase 3 — UAH Truth:** `fx_rates` table; NBU daily fetcher (16:00 Kyiv); UAH rollup computed on read via JOIN on `(currency, attributed_day)`; weekend/holiday fallback to most-recent-prior business-day rate.
- **Phase 4 — Categorized Spending:** `categories` + `rules` tables; rules engine with structured-JSON predicates (no eval); MCC-seeded taxonomy; `category_source` / `is_user_locked` writes (column already present from Phase 1); diff-preview before commit.
- **Phase 5 — Honest Totals:** `transaction_links` table; internal-transfer detection (≥3 signals → auto-pair); refund matching (counterparty/MCC overlap, ±60d); reversible via `DELETE /api/transactions/{id}/link/{link_id}`.
- **Phase 6 — This Month UI:** React 19 + Vite 8 + Tailwind 4 + shadcn/ui frontend in `frontend/`; "this month" dashboard (Europe/Kyiv calendar month, day-of-month-clipped comparison); transaction feed (cursor pagination on `(occurred_at DESC, id DESC)`); manual edit/merge/split/cash; mobile-responsive (375px).
- **Phase 7 — Ship Ready:** Daily `pg_dump` to bind-mounted `${DATA_DIR}/backups/`; restore tested manually; CSV import (`source = csv_import`); full CSV/JSON export; `/about` page documenting `api.monobank.ua` + `bank.gov.ua` as the only network egress; grep-able evidence of zero analytics SDKs.
