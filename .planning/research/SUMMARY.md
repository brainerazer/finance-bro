# Research Summary: finance-bro

**Project:** finance-bro
**Domain:** Self-hosted personal-finance importer and spending-visibility dashboard (single user, Monobank UA, multi-currency UAH/USD/EUR, homelab Docker)
**Researched:** 2026-05-10
**Confidence:** HIGH

---

## TL;DR

- **Stack:** FastAPI 0.136 + SQLAlchemy 2.0 + **Postgres 17** in compose, React 19 + Vite 8 + TanStack Query 5 on the frontend
- **DB resolved:** Postgres (not SQLite) because homelab users routinely mount Docker volumes on NFS (Synology/Unraid/TrueNAS), and SQLite WAL on NFS silently corrupts. SQLite is the fallback only for sub-512 MB hardware on a confirmed local block device.
- **Deployment shape:** two compose services (`app`, `db`); bind-mount data dir; LAN/Tailscale trust boundary; no app-level auth in v1
- **Dominant build-order constraint:** the polling rate-limit budget (1 req/60s per token) serializes everything — categorization, reconciliation, and dashboards all depend on transactions existing; get the importer right before building anything else
- **Mono timestamp canonical fact:** Mono exposes only `time` (Unix seconds, UTC posting time). There is no `operationDate`. Month attribution must be computed in `Europe/Kyiv` timezone.
- **Top landmine 1:** SQLite WAL on NFS causes silent DB corruption. Use Postgres.
- **Top landmine 2:** Float for money causes drift in category totals. Use `BIGINT` minor units in DB, `Decimal` in Python.
- **Top landmine 3:** Mono `id` is scoped per account, not globally unique. Dedup key must be `(account_id, source_tx_id)`.
- **Scope cliff:** budgets, LLM categorization, auth, webhooks, and multi-user are explicit anti-features or deferrals; adding any in v1 is the most likely path to never shipping.

---

## Stack Recommendation

| Layer | Choice | Version | Confidence | Why |
|-------|--------|---------|------------|-----|
| Backend language | Python | 3.13.x | HIGH | Stable; `Decimal`/`zoneinfo`/async first-class; LLM ecosystem targets this |
| Backend framework | FastAPI | 0.136.1 | HIGH | Async-native, Pydantic v2, free OpenAPI schema for TS codegen |
| ASGI server | Uvicorn | 0.46.0 | HIGH | `--workers 1` — single-user, single scheduler; no Gunicorn pool needed |
| ORM + migrations | SQLAlchemy 2.0 + Alembic 1.18 | 2.0.49 / 1.18.4 | HIGH | Typed ORM, SQL Core escape hatch for reconciliation, mature migrations |
| DB driver | psycopg 3 | 3.3.4 | HIGH | Async-native; replaces psycopg2; use `psycopg[binary,pool]` |
| **Database** | **PostgreSQL 17** | 17.x | **HIGH** | See "Conflicts Resolved" — NFS/homelab reality makes Postgres the correct default |
| Money type (Python) | `decimal.Decimal` + hand-rolled `Money` dataclass | stdlib | HIGH | `py-moneyed` abandoned (2022); 30-line dataclass beats it |
| Money storage (DB) | `BIGINT` minor units + `CHAR(3)` ISO-4217 alpha column | — | HIGH | Stripe-style minor-units; no float anywhere |
| FX rates (DB) | `NUMERIC(18,8)` in `fx_rates` table | — | HIGH | Rates are not money; higher-precision NUMERIC is correct here |
| Scheduler | APScheduler 3.11 `AsyncIOScheduler` in-process | 3.11.2 | HIGH | One job/65s; avoids Redis + Celery; `max_instances=1, coalesce=True` |
| HTTP client | httpx 0.28 `AsyncClient` | 0.28.1 | HIGH | Same event loop as FastAPI; native retries/timeouts; never `requests` |
| FX source | NBU `exchangenew?json` endpoint | — | HIGH | Authoritative for UAH per PROJECT.md; daily fetch + per-date cache |
| Frontend framework | React 19 + Vite 8 + TypeScript 6 | latest stable | HIGH | Largest component/chart ecosystem; user prefers JS |
| Frontend data cache | TanStack Query 5 | 5.100.x | HIGH | Right-sized for small JSON API + dashboard; no Redux needed |
| Charts | Recharts 3 | 3.8.x | HIGH | Declarative React API; ~50 KB gz; covers all dashboard chart types |
| CSS + components | Tailwind 4 + shadcn/ui | Tailwind 4.3 / shadcn CLI 4.7 | HIGH | shadcn copies components into repo (no vendor runtime dep) |
| API style | REST + FastAPI OpenAPI to `openapi-typescript` | — | HIGH | Types flow to TS for free; no GraphQL or tRPC overhead |
| Python tooling | uv 0.11 + ruff 0.15 + basedpyright 1.39 + pytest 9 | latest stable | HIGH | uv replaces pip+venv+pyenv; ruff replaces black+isort+flake8 |
| Container | `python:3.13-slim-trixie` multi-stage + `postgres:17-bookworm` | pinned | HIGH | Slim base; pin major version |
| Auth (deferred) | Cookie + single passphrase (`itsdangerous`) | v1.5 only | MEDIUM | No auth in v1; network-gated; defer until hosting model changes |

**What to avoid:** `float` for money, `requests` (sync), `psycopg2`, `py-moneyed`, Celery + Redis, Redux/Zustand, `moment.js`, `pytz` (use `zoneinfo`), named Docker volumes for DB, `docker compose down -v` on live data.

---

## Feature Scope

### v1 Table Stakes (must-build; partial v1 is not usable)

- Polling-based Mono ingestion with rate-limit-aware queue (65s interval, `max_instances=1`)
- Per-account ingestion across cards, jars, FOP accounts (separate endpoints; `counterEdrpou` preserved)
- Persist full source payload (`raw_payload` JSON column) alongside normalized rows for replay safety
- Idempotent import via `(account_id, source_tx_id)` unique index; soft-delete model (no hard deletes)
- Hold/pending handling: ingest with `hold` flag, exclude from totals, update-in-place when `hold: false` arrives with same `id`
- Chunked, resumable backfill (30-day windows, newest-first, `last_cursor` persisted for crash recovery)
- Multi-currency model: `amount_minor` + `currency` + `operation_amount_minor` + `operation_currency` all stored; UAH rollup computed on read via NBU txn-day rate
- NBU FX fetch (daily cron at 16:00 Kyiv, 12-month historical backfill on first run, fallback to most-recent prior rate when date has no NBU publication)
- Rules engine: composable predicates (merchant substring/regex, MCC, amount sign/range, account, currency, counterparty IBAN/EDRPOU, comment); ordered priority list; first-match-wins; `is_user_locked` prevents re-run overwrite
- Default category taxonomy (~15 categories) seeded from MCC groups; user-editable
- Internal-transfer detection: opposite-sign + same-amount-in-common-currency + within ±2 days + both accounts user-owned; auto-pair at confidence >= 0.8, surface for user confirmation below
- Refund/reversal pairing: same account, opposite sign, same amount, overlapping counterparty/MCC, within ±60 days; same confidence model
- Manual edit / merge / split / re-categorize (raw_payload never mutated)
- Manual cash transaction entry (`source = 'manual_cash'`)
- "This month" dashboard: total spent, top categories, vs prior month (calendar month, Europe/Kyiv, periods clipped to day-of-month for fair comparison)
- Transaction feed with filter, search, sort, cursor pagination, quick re-categorize, detail drawer with matched rule + raw payload
- Polling status visibility (last poll, last error, 401/429 surfaced explicitly)
- Token entry, validation, rotation; encrypted at rest (fernet/libsodium key from env)
- CSV import as fallback; CSV + JSON export
- DB backup/restore (daily `pg_dump` to bind-mounted backup dir; restore procedure documented and tested before v1 ships)
- Log redaction on by default (Mono token, `X-Token` header, transaction amounts at INFO+)
- Responsive web UI working on a real 375px-wide phone browser
- Docker single-compose deploy (`app` + `db`), bind-mount data dir, documented `PUID`/`PGID`
- Network egress documented (Mono + NBU only); no analytics/telemetry SDKs

### v1 Differentiators (build if cheap; cluster with nearby phases)

| Differentiator | Cost | Cluster with |
|----------------|------|--------------|
| MCC + composable rules (both `mcc` and `originalMcc` matchable) | Already in table stakes | Phase 6 |
| `is_user_locked` + `category_source` audit trail | Low — one column | Phase 6 |
| Run-rules-on-history with diff preview before commit | Low | Phase 6 |
| Multi-token / multi-card support (FOP token separate from personal) | Medium | Phase 2-4 |
| Transaction detail drawer with matched rule + raw payload | Low | Phase 10 |
| Receipt link-out via Mono `receiptId` | Very low | Phase 10 |
| Token redaction in logs by default | Very low | Phase 1 |
| Token encrypted at rest | Low | Phase 2 |
| Network egress documented in-app (About page) | Very low | Phase 13 |
| Top-merchants view (pivot on normalized payee) | Low | Phase 10 / v1.x |
| Calendar heatmap (cheap once dashboard backend exists) | Low | v1.x |
| Tags as orthogonal axis (join table, chip UI) | Low | v1.x |
| Auto-rule suggestion from manual recategorizations (needs 50+ overrides first) | Medium | v1.x |
| Smart "looks like a transfer" prompt for low-confidence pairs | Medium | v1.x |

### Anti-Features (explicitly NOT building)

| Feature | Why Not |
|---------|---------|
| Budgets / envelopes / category limits | Core Value is visibility, not planning; budgets demand ongoing maintenance — the exact thing this user is fleeing |
| Cashflow forecasting | Requires recurring-transaction model, future entries, scheduled bills — different product surface |
| Savings-goal progress tied to jars | Planning UX; show jar balance + goal as two numbers, no progress bar |
| Alerts / push notifications | No push channel in homelab + Tailscale model; SMTP/Telegram bot is out-of-scope moving part |
| App-level authentication | Network-gated model; auth doubles security surface for zero gain in this trust model |
| Multi-user / household accounts | Single-user by design; multi-tenancy is a v3 conversation |
| Investment / brokerage / net-worth tracking | Different product domain; Mono does not expose investment data |
| LLM categorization in v1 | Build rules first, observe actual long tail, then choose local Ollama vs API with full information |
| Webhook ingestion in v1 | Requires public HTTPS endpoint; breaks homelab + Tailscale trust model |
| PWA / installable / offline mode | Responsive browser is sufficient; service worker adds scope for no stated need |
| Cloud sync | Defeats trust model — user-controlled hardware is the point |
| Other importers (PrivatBank, Wise, Revolut) in v1 | After Mono path is rock solid only; importer interface is designed for extensibility |
| Plaid / Salt Edge / bank aggregators | These ARE the third-party data exposure the user is avoiding |
| Real-time updates | Mono rate limit is 1 req/60s; real-time is structurally impossible; show "last polled N min ago" |
| AI-generated insights | Dashboard being good enough IS the insight; avoid LLM dependency in v1 |

---

## Architecture

### Modular Monolith — Single Process, Ports and Adapters at Two Seams

One FastAPI process hosts the HTTP API, APScheduler, Mono poller, categorizer, reconciler, and FX fetcher. No worker queue, no Redis, no Celery. Frontend is a separate SPA served via FastAPI `StaticFiles` (or optional Caddy sidecar). Two compose services: `app` and `db`.

Ports/adapters are enforced at exactly two seams:
1. **Importer port** — so PrivatBank/Wise can be added as adapter implementations without touching the model layer
2. **Categorizer port** — so an LLM categorizer plugs in alongside the rules engine without code changes elsewhere

Everywhere else: plain functions, no premature interfaces.

### Component Map

```
Browser (LAN/Tailscale)
  └─ Web UI (React SPA)
       └─ REST + SSE → FastAPI (single process)
                         ├─ API Layer (routes, Pydantic schemas — no business logic)
                         ├─ Application Services (use cases: import, reconcile, categorize)
                         │    ├─ Poller (rate-budget owner, round-robin accounts)
                         │    │    └─ Importer Port → MonobankImporter (httpx adapter)
                         │    ├─ Categorizer Engine (plugin pipeline, priority-ordered)
                         │    │    └─ RulesCategorizer (default; LlmCategorizer slot in v1.5)
                         │    └─ Reconciler Engine (dedup → transfer detection → refund pairing)
                         ├─ FX Service (NBU adapter + fx_rates cache)
                         ├─ Scheduler (APScheduler AsyncIOScheduler in lifespan)
                         └─ Repository Layer (SQLAlchemy 2.0; all SQL lives here)
                              └─ PostgreSQL 17 (compose service, bind-mount ./data/postgres)

External:
  api.monobank.ua/personal/  (1 req / 60s per token)
  bank.gov.ua/NBU             (daily FX rates, no auth required)
```

### Core Schema Entities (canonical names for downstream phases)

- **`accounts`**: `(source_kind, source_account_id)` unique; `source_kind` = `mono.card` / `mono.jar` / `mono.fop` / `cash`
- **`transactions`**: `(account_id, source_tx_id)` partial unique index (idempotency key); `amount_minor BIGINT`, `currency CHAR(3)`, `operation_amount_minor`, `mcc INTEGER`, `hints JSON`, `raw_payload JSON`, `category_id`, `category_source`, `is_user_locked`, `is_deleted`, `import_run_id`
- **`transaction_links`**: one table for transfer pairs and refund pairs; `link_kind` = `internal_transfer` or `refund`; `confidence REAL`; unique on `(source_tx_id, target_tx_id, link_kind)`
- **`categories`**: flat list with optional `parent_id`; `is_internal` flag for pseudo-categories
- **`rules`**: `priority INTEGER`, `predicate JSON` (structured, no eval), `category_id`, `is_enabled`
- **`fx_rates`**: `(rate_date, from_currency, to_currency)` unique; `rate NUMERIC(18,8)`; fallback = `MAX(rate_date) WHERE rate_date <= transaction_date`
- **`import_runs`**: audit trail + backfill resumability (`last_cursor`, `status`, `requests_used`, `txs_inserted`)

UAH rollup is **computed on read** (join `transactions` x `fx_rates` on `currency + date`). Never stored as a column — it becomes stale when FX rates backfill or correct.

### Build-Order DAG

Each phase is shippable before the next starts:

```
Phase 1: Skeleton + storage spine
   └─ Phase 2: Mono importer (manual trigger, rate-budget gate)
         ├─ Phase 3: Backfill orchestration (chunked, resumable)
         └─ Phase 4: Scheduler + steady-state polling (APScheduler lifespan)
   └─ Phase 5: FX service (NBU, daily cron, historical backfill)
         └─ (requires 2 + 5) Phase 6: Categorization / rules engine
               └─ Phase 7: Reconciler (transfer + refund detection)
                     └─ (requires 5 + 6 + 7) Phase 8: Read API + cursor pagination
                           └─ Phase 9: SSE event stream
                                 └─ (requires 8 + 9) Phase 10: Frontend MVP
                                       ├─ Phase 11: Manual edits (cash, exclude, split/merge)
                                       ├─ Phase 12: Backup/restore UX + data export
                                       └─ Phase 13: Polish (mobile, retries, empty states)
```

**Suggested 8-week / 13-phase breakdown:**

| Weeks | Phases | Milestone |
|-------|--------|-----------|
| 1-2 | 1-4 | "I can poll Mono and see rows in Postgres" |
| 3-4 | 5-7 | "I can see UAH rollups and detected transfers" |
| 5-6 | 8-10 | "I have a usable web UI" |
| 7-8 | 11-13 | "Manual edits work, I can back up, polish" |

---

## Critical Mono API Facts

| Fact | Value | Feature implication |
|------|-------|---------------------|
| Rate limit | 1 request / 60s per token, shared across all endpoints for that token | Single token-bucket gate; all callers route through it; poll interval = 65s |
| Statement window | Max 31 days + 1 hour (2,682,000s) per call | Backfill chunks to <=30-day windows; 12-month backfill = ~12 calls = ~13 minutes |
| Pagination | Max 500 items per statement call | If result == 500, split window in half and retry |
| Timestamp | Only `time` (Unix seconds, UTC posting time) — no `operationDate` field exists | Derive `attributed_day` as `Europe/Kyiv` calendar date; use `zoneinfo`, not `pytz` |
| Pending state | `hold: bool` only | Ingest holds with flag; exclude from totals; update-in-place when same `id` returns `hold: false` |
| Amounts | `amount` = account-currency minor units (kopecks); `operationAmount` = transaction-currency minor units; both signed | Convert at import boundary only; never re-divide downstream |
| Currency codes | ISO 4217 numeric: `currencyCode: 980` = UAH, `840` = USD, `978` = EUR | Map to ISO alpha at importer boundary; everything downstream uses alpha |
| Transaction ID | `statementItem.id` — stable opaque string; uniqueness scope is per-account (not documented as globally unique) | Dedup key must be `(account_id, source_tx_id)`, never `source_tx_id` alone |
| Accounts | Cards, jars, FOP accounts returned together from `/personal/client-info` | Each is a separate ingestion target with its own statement endpoint |
| FOP extras | `counterEdrpou` (sole-proprietor tax ID) + `counterIban` | Store in `hints` JSON; expose as rule-condition fields |
| `mcc` vs `originalMcc` | Both present; `mcc` = Mono-normalized, `originalMcc` = merchant-declared | Rules engine supports matching on either; default to `mcc` |
| `receiptId` | Present on many (not all) withdrawals | Link out to Mono receipt page when present; no OCR |
| Webhooks | Require public HTTPS endpoint | Out of scope in v1 |
| 429 response body | Body shape not officially documented | Treat any 429 as "back off >= 60s"; respect `Retry-After` header if present |

**NBU FX specifics:**
- Publishes daily at ~15:30 Kyiv; fetch cron at 16:00 Kyiv for safety margin
- Weekends and Ukrainian public holidays have no published rate — API returns empty array, not yesterday's rate
- Fallback: `MAX(rate_date) WHERE rate_date <= transaction_date` — last known business-day rate
- NBU returns major-unit decimal rates; store as `NUMERIC(18,8)` in `fx_rates`
- For FX-on-card transactions: use Mono's `amount` (already in account currency) — do not re-convert via NBU; that double-converts and drifts from the user's real balance

---

## Top Landmines (by phase)

### Phases 1-2: Skeleton + Importer

1. **SQLite WAL on NFS corrupts silently** — homelab users put Docker data dirs on Synology/NFS shares; WAL relies on POSIX locks unavailable over NFS; silent corruption discovered days later. Use Postgres. (Pitfall 11)
2. **Float for money** — JSON decoder returns `float` by default; `0.1 + 0.2 != 0.3`; totals drift across thousands of transactions. Use `BIGINT` minor units in DB, `Decimal` in Python. (Pitfall 1)
3. **Mono `id` dedup key must include `account_id`** — uniqueness is per-account, not global. (Pitfall 3)

### Phases 3-4: Backfill + Scheduler

1. **Rate limit is per token, not per endpoint** — all calls (statement, client-info) share one 60s budget; the token-bucket gate must cover every caller. (Pitfall 4)
2. **31-day window hard limit** — passing a longer range returns empty or 400; chunk to <=30 days; treat any 4xx as error, not "no data". (Pitfall 5)
3. **Off-by-100 on Mono amounts** — Mono returns kopecks; NBU returns major-unit decimals; wrap in named conversion functions, ban raw `/100`. (Pitfall 2)

### Phases 5-7: FX + Categorization + Reconciler

1. **NBU gaps on weekends/holidays** — empty array means "no rate today", not "rate is 0"; fall back to most-recent prior rate; never block import on FX availability. (Pitfall 7)
2. **`time` field / timezone** — `time` is UTC posting time; `attributed_day` must use `Europe/Kyiv`; use `zoneinfo`, not `pytz`; late-night transactions cross month boundaries and that is acceptable. (Pitfall 6, 14)
3. **Manual category edits overwritten by re-run rules** — add `category_source` + `is_user_locked` columns before writing the rules engine; the engine skips locked rows unconditionally. (Pitfall 10)

### Phases 8-10: API + Frontend MVP

1. **"This month" boundary ambiguity** — default to calendar month (Europe/Kyiv); label the period explicitly; clip both months to same day-of-month for M-o-M comparison. (Pitfall 16)
2. **Internal transfer false positives** — same-amount same-day outflow/inflow pairs are ambiguous; require >= 3 signals for auto-pair; surface low-confidence matches for user confirmation, never auto-hide. (Pitfall 9)
3. **No transaction data in localStorage** — plaintext, readable to any JS on the origin; fetch fresh on every load. (Pitfall 25)

### Phases 11-13: Polish + Backup + Deploy

1. **No backup = eventual total data loss** — `docker compose down -v` or NAS disk failure with no backup means all history is gone (Mono only serves 31 days from token re-issue); daily `pg_dump` to bind-mounted dir is a day-one feature, tested before v1 ships. (Pitfall 18)
2. **Named volume vs bind mount** — use bind mount (`./data/postgres:/var/lib/postgresql/data`), not named volumes; named volumes get wiped by `down -v` and are invisible in the NAS file browser. (Pitfall 12)
3. **Schema migrations without pre-flight backup** — before `alembic upgrade head` on container start, copy current DB to `backups/pre-migration-${sha}/`; never drop columns in the same release that removes the code that uses them. (Pitfall 13)

---

## Conflicts Resolved

### Conflict 1: Database — SQLite vs Postgres

**What the researchers said:**
- STACK.md: recommended Postgres 17 (MVCC for concurrent poller+UI writes, exact NUMERIC arithmetic, pgvector door, Alembic ergonomics)
- ARCHITECTURE.md: recommended SQLite + WAL (single writer, simpler ops, smaller footprint, natural single-user default)
- PITFALLS.md: flagged "SQLite WAL on NFS = silent corruption" as the single biggest homelab landmine; explicitly recommended Postgres in compose given Synology/Unraid/TrueNAS reality

**Resolution: Postgres 17**

The deciding factor is the homelab deployment target, not the concurrency argument. Privacy-first homelab users routinely run Docker with data directories on NFS shares (Synology volumes, Unraid user shares, TrueNAS datasets). SQLite WAL mode relies on POSIX `fcntl()` advisory locks and shared-memory mappings — both unreliable over NFS. The failure mode is silent corruption discovered days later, not a loud startup error.

Postgres in compose costs approximately 80 MB of RAM at idle and one additional compose service. On a box already running Docker and homelab services, those costs are negligible. Additional upside: exact `NUMERIC` arithmetic for SQL rollups, full `ALTER TABLE` for schema evolution, pgvector path for the v1.5 LLM categorizer, and `pg_dump` backups that are transactionally safe.

**SQLite remains the documented escape hatch** for hardware under 512 MB RAM AND only when the data directory is pinned to a confirmed local block device (not any NFS/SMB/CIFS path). In that case: WAL mode, `busy_timeout = 5000`, Alembic `render_as_batch = True`, and a prominent warning that the data directory cannot be on a network share.

### Conflict 2: Mono timestamp field — `time` vs `operationDate`

**What the researchers said:**
- PITFALLS.md was framed around a potential `time` vs `operationDate` footgun (cross-month attribution risk)
- FEATURES.md (HIGH confidence, cross-validated against Mono official docs, go-monobank type definitions, python-monobank, and vergilet/monobank Ruby client) confirms there is **only `time`** — there is no `operationDate` field

**Resolution: Mono provides only `time` (Unix seconds, UTC posting time). The `operationDate` framing is dropped entirely.**

The mental model of `operationDate` is imported from APIs (Stripe, Plaid, EU open-banking) that distinguish "transaction date" from "settlement/posting date." Mono does not make this distinction in the API response.

Canonical model:
- Store `occurred_at` = `time` field value, in UTC
- Derive `attributed_day` = `occurred_at` converted to `Europe/Kyiv` calendar date using `zoneinfo.ZoneInfo("Europe/Kyiv")`
- "This month" dashboard filters on `attributed_day`
- Accept that ~1% of late-night transactions straddle a month boundary from the user's perspective; allow manual date override on the transaction if needed
- For refund matching: do not use date proximity as a primary signal; use amount + merchant + description with arbitrary date gap

---

## Open Questions

| Question | Risk if wrong | Phase to resolve |
|----------|---------------|-----------------|
| Is Mono `statementItem.id` globally unique or strictly per-account? | If global: dedup key could be simplified. Currently assuming per-account (safest). | Phase 2 — test empirically by checking whether a jar transfer gives the same `id` on both the source account and the jar, or different `id`s |
| What does NBU return on a weekend — empty array, 404, or error JSON? | If not empty array: fallback logic needs adjustment. Currently assuming empty array. | Phase 5 — hit the NBU endpoint on a Saturday with a recorded fixture test |
| How far back does Mono actually retain statement history accessible via the API? | If shorter than 12 months: backfill range assumptions break. | Phase 3 — run backfill with a 24-month window; observe where data ends |
| FOP token: same personal token or separate token issued for FOP accounts? | If separate: multi-token architecture required from day one. Currently assuming FOP appears under one personal token. | Phase 2 — verify against a real Mono FOP account `client-info` response |
| Mono 429 response: does it include a `Retry-After` header? | If present: use it precisely. If absent: 60s conservative backoff. | Phase 2 — observe a deliberate 429 in a controlled test environment |

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All versions verified against PyPI/npm/Docker Hub on 2026-05-10; version compatibility matrix explicitly checked |
| Features | HIGH | Mono API field shapes cross-validated against official docs, go-monobank types, three client READMEs, and multiple open-source importer issue trackers |
| Architecture | HIGH | Prescriptive, well-sourced; component names and interface shapes are canonical for downstream phases |
| Pitfalls | HIGH for money/SQLite/Docker; MEDIUM for NBU weekend behavior and reconciliation specifics | SQLite NFS corruption confirmed across Sonarr, GoToSocial, NixOS, Mozilla issue trackers independently |

**Overall confidence: HIGH**

### Gaps to Address During Implementation

- **Mono `id` global vs per-account uniqueness:** use composite key defensively; validate empirically in Phase 2
- **NBU weekend/holiday API response shape:** build fallback logic first; validate against a real Sunday fixture in Phase 5
- **Mono historical retention horizon:** run a 24-month backfill attempt in Phase 3
- **FOP token vs personal token:** confirm account structure before committing to single-token polling in Phase 2
- **Mono 429 body/header shape:** observe deliberately in a controlled test before relying on `Retry-After`

---

## What This Unlocks Next

With this synthesis, requirements definition can name table-stakes feature IDs (ingestion, dedup, transfer detection, multi-currency, rules engine, dashboard, transaction feed, backup) and set v1 boundaries against the explicit anti-feature list. The roadmapper can lay out the 13-phase build order on the resolved Postgres stack, using the dependency DAG above as the phase-ordering constraint. Per-phase research is warranted for: Phase 2 (Mono importer — empirical validation of `id` scope, FOP token structure, and 429 response shape), Phase 5 (NBU FX — weekend response shape and holiday gap handling), and Phase 7 (Reconciler — transfer/refund heuristic tuning against real transaction data). Phases 1, 6, 8, and 10 follow well-documented patterns (FastAPI + SQLAlchemy + React + TanStack Query) and can proceed directly to planning without additional research.

---

## Sources (aggregated)

**Mono API (HIGH confidence):**
- Monobank Open API docs v250818: https://api.monobank.ua/docs/index.html
- go-monobank struct definitions: https://pkg.go.dev/github.com/vtopc/go-monobank
- python-monobank README: https://github.com/vitalik/python-monobank
- siomochkin/monobank-open-api-documentation: https://github.com/siomochkin/monobank-open-api-documentation
- vergilet/monobank Ruby client: https://vergilet.github.io/monobank/

**NBU FX (HIGH confidence):**
- NBU Developer API: https://bank.gov.ua/en/open-data/api-dev
- floatrates.com NBU mirror: https://www.floatrates.com/source/nbu/ — weekday-only publication confirmed
- kastaneda/nbu_rates archive: https://github.com/kastaneda/nbu_rates

**Stack libraries (HIGH confidence, live registry 2026-05-10):**
- PyPI, npm registry, Docker Hub library tags — all versions in stack table above

**Architecture and pitfall evidence (HIGH confidence):**
- FastAPI lifespan events: https://fastapi.tiangolo.com/advanced/events/
- SQLAlchemy 2.0 docs: https://docs.sqlalchemy.org/en/20/
- Alembic batch mode docs: https://alembic.sqlalchemy.org/en/latest/batch.html
- Storing currency values best practices: https://cardinalby.github.io/blog/post/best-practices/storing-currency-values-data-types/
- SQLite WAL docs (confirms NFS incompatibility): https://sqlite.org/wal.html
- Sonarr #1886 — SQLite on NFS: https://github.com/Sonarr/Sonarr/issues/1886
- GoToSocial SQLite networked storage warning: https://docs.gotosocial.org/en/latest/advanced/sqlite-networked-storage/

**Feature prior art (MEDIUM confidence):**
- Actual Budget docs and issue tracker — rules (#3702), backups, duplicate detection (#2519, #6239): https://actualbudget.org/docs/
- Lunch Money support docs — rules, retroactive apply, split/unsplit: https://support.lunchmoney.app/
- Firefly III docs and issues — duplicate detection, transfer matching (#11329), export: https://docs.firefly-iii.org/
- Oleksios/Merchant-Category-Codes (UA-localized MCC dataset): https://github.com/Oleksios/Merchant-Category-Codes
- smaugfm/monobudget (Mono-specific transfer detection): https://github.com/smaugfm/monobudget

---

*Research completed: 2026-05-10*
*Ready for roadmap: yes*
