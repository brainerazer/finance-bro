# Roadmap: finance-bro

**Project:** finance-bro
**Mode:** mvp
**Granularity:** standard
**Defined:** 2026-05-10
**Core Value:** Automatic visibility into where my money goes — zero manual upkeep, on hardware I own.

## Overview

7 phases. Each phase is a vertical MVP slice that progressively widens and deepens the spending-visibility picture. Phase 1 already produces a demonstrable end-to-end result (token in → real Mono row out via the API). Subsequent phases add reliability (steady-state sync), correctness (FX, categories, reconciliation), the surface the user sees (dashboard + feed), and the operational guarantees that make v1 shippable (backup/restore, exports).

Build-order dependencies follow the architecture DAG: storage spine + Mono importer first, then FX, then categorization, then reconciliation, then the UI that surfaces what was already collected, then the operational closures. No "horizontal layer" phases — every phase moves a real user-observable behavior forward.

Critical invariants land **in the phase that introduces them**, not retrofitted later:
- Composite idempotency key `(account_id, source_tx_id)` (ING-04) — Phase 1
- Single token-bucket rate-limit gate (ING-02) — Phase 1
- `category_source` + `is_user_locked` columns (CAT-04) — Phase 4 (introduced) but schema groundwork in Phase 1 so re-categorization never clobbers manual edits
- Log redaction defaults (OPS-04) — Phase 1
- Backup/restore tested (OPS-02) — Phase 7, before "ship"

## Phases

- [ ] **Phase 1: First Real Transaction** — token entry → poll one Mono account → row visible via API
- [ ] **Phase 2: Reliable Sync** — automatic 60s polling, holds handling, 12-month backfill, sync status visible
- [ ] **Phase 3: UAH Truth** — every foreign-currency transaction has a correct UAH rollup at NBU txn-day rate
- [ ] **Phase 4: Categorized Spending** — rules-driven categorization with default taxonomy; manual edits never overwritten
- [ ] **Phase 5: Honest Totals** — internal transfers and refunds detected and netted; spending math stops lying
- [ ] **Phase 6: This Month UI** — dashboard + transaction feed; mobile-usable; cash + manual edits work
- [ ] **Phase 7: Ship Ready** — daily backups, restore tested, CSV/JSON import + export, no-telemetry promise documented

## Phase Details

### Phase 1: First Real Transaction
**Goal:** Bohdan can paste his Mono token, click import, and see the most recent transactions from one of his Mono cards as JSON rows from the API. The full thin slice exists end-to-end (token → rate-limited Mono call → Postgres → API echo) on the correct schema invariants.
**Mode:** mvp
**Depends on:** Nothing (first phase)
**Why this phase exists:** Visibility-without-manual-upkeep starts with one round trip to Mono. Without it, the project is a stack of plans. This phase proves the spine works and locks the correctness invariants (minor-units BIGINT, composite idempotency key, single rate-limit gate, log redaction) that retrofitting would cost weeks.
**Requirements:** ING-01, ING-02, ING-03, ING-04, ING-07, FX-01, OPS-01, OPS-04, DEP-01, DEP-02
**Success Criteria** (what must be TRUE):
  1. Bohdan starts the app via `docker compose up`, opens it on the LAN with no app-level login, pastes his Mono token, and the app validates it against `/personal/client-info` within one rate-limit slot.
  2. After clicking "import now" for one account, within ~65 seconds Bohdan can call `GET /api/transactions` and see real rows from his Mono card, each row carrying `amount_minor` (BIGINT, signed minor units), `currency` (ISO-4217 alpha), and a verbatim `raw_payload` JSON of the original Mono `statementItem`.
  3. Triggering import twice in quick succession does NOT create duplicate rows: the composite `(account_id, source_tx_id)` unique index makes the second call a no-op insert.
  4. Triggering two manual imports within 60 seconds does NOT cause a Mono 429: the single token-bucket gate serializes both callers to one request per 60s, persisted to disk so a container restart cannot violate the limit.
  5. Inspecting `docker logs` at INFO level after a successful import shows zero hits for the Mono token, the `X-Token` header value, or any transaction `amount` value.
**Plans:** 4 plans
- [x] 01-01-PLAN.md — Project scaffold + test harness + first migration with partial unique index + structlog redaction
- [x] 01-02-PLAN.md — RateLimitGate (Postgres FOR UPDATE) + MonobankImporter port and httpx adapter
- [ ] 01-03-PLAN.md — Repos + ImportService + FastAPI routes (health, accounts, transactions, import) + idempotency / log-redaction integration tests
- [ ] 01-04-PLAN.md — compose.yml + Dockerfile + README + manual phase-gate verification (real Mono, real docker logs)
**UI hint:** no
**Notes / Risks:**
  - **Pitfall 1 (floats for money):** schema must use `BIGINT` minor units + ISO-4217 alpha currency column from day one. No `Float`/`Real`/`Numeric(_,2)` columns for transactional amounts. `Decimal` only at edges.
  - **Pitfall 11 (SQLite WAL on NFS):** stack is Postgres 17 in compose with bind-mounted `./data/postgres`. Document that the data directory must be a local block device, not an NFS share.
  - **Pitfall 3 (composite idempotency key):** unique index on `(account_id, source_tx_id)` lands in the first migration, not "later when we have re-imports". Mono `id` is per-account scope.
  - **Pitfall 4 (rate-limit gate):** the token bucket lives in one place, owned by `MonobankImporter`, and persists last-acquired-at across restarts. Implement before writing any business logic.

### Phase 2: Reliable Sync
**Goal:** Bohdan stops clicking import. The app polls Mono on its own at the rate-limit budget, ingests holds correctly (and updates them in place when they clear), can backfill 12 months on first connect, and surfaces "last poll N min ago" plus 401/429 distinctly so silent failures are impossible.
**Mode:** mvp
**Depends on:** Phase 1
**Why this phase exists:** "Zero manual upkeep" demands the import is automatic and self-healing. A user who has to click import is doing manual work; a user who has no signal that polling is broken is worse off than one with a manual button. This phase makes the importer trustworthy.
**Requirements:** ING-05, ING-06, ING-08
**Success Criteria** (what must be TRUE):
  1. Bohdan leaves the app running for an hour without touching it; new transactions on his Mono card appear in `GET /api/transactions` within ~3 minutes of posting (round-robin across his accounts at one poll per 60s).
  2. Bohdan triggers a 12-month backfill on a fresh install; it walks ≤30-day windows newest-first, persists `last_cursor` per chunk, and resumes exactly where it stopped if the container is killed mid-run.
  3. A pending Mono transaction (`hold: true`) appears in the feed flagged as held and is excluded from any "spent" totals; when it later returns with `hold: false` and possibly a different amount, the same row updates in place — no duplicate row, no double-count.
  4. `GET /api/import/status` (or equivalent surface) shows last successful poll timestamp, last error if any, and distinguishes 401 (token revoked → scheduler stopped, banner asks for re-paste) from 429 (rate-limit hit → backed off, will retry next slot).
**Plans:** TBD
**UI hint:** no
**Notes / Risks:**
  - **Pitfall 5 (31-day backfill window):** chunk to ≤30 days, walk newest-first, treat any 4xx as error not absence. Constant-named `MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000`.
  - **Pitfall 23 (token revocation):** 401 and 429 must follow distinct paths. 401 stops the scheduler and surfaces a UI/banner state. 429 backs off ≥60s.
  - **Hold→cleared idempotency** (Pitfall 3 continued): same `(account_id, source_tx_id)` returning with `hold: false` upserts the existing row; never inserts a second.

### Phase 3: UAH Truth
**Goal:** Bohdan looks at any USD or EUR transaction in the feed and sees an honest UAH equivalent computed at the NBU rate of the transaction's day, with weekend/holiday fallback to the most recent prior business-day rate. FX-on-card transactions use Mono's already-converted account-currency amount — never double-converted via NBU.
**Mode:** mvp
**Depends on:** Phase 1, Phase 2
**Why this phase exists:** Bohdan lives in UAH but holds USD/EUR. Without honest UAH rollups every dashboard number downstream is a lie. The Core Value is "where my money goes" measured in the unit he thinks in.
**Requirements:** FX-02, FX-03, FX-04
**Success Criteria** (what must be TRUE):
  1. On first run, the app fetches 12 months of NBU rates (USD/UAH, EUR/UAH) and persists them in `fx_rates` as `NUMERIC(18,8)` keyed by `(rate_date, from_currency, to_currency)`.
  2. A daily cron job at 16:00 Europe/Kyiv pulls today's NBU rate; failure does not block transaction import — rollups gracefully fall back to most recent prior business-day rate, and the API marks affected responses with `fx_stale: true`.
  3. `GET /api/transactions` for a USD-currency transaction returns a UAH-equivalent computed on read by joining `transactions × fx_rates` on `(currency, attributed_day)`; no `uah_amount_minor` is denormalized in the schema.
  4. For a Mono FX-on-card transaction (e.g., EUR card paying at a USD merchant), the UAH rollup uses Mono's `amount` field (account currency, already settled) × NBU rate — not a re-conversion via the operation-currency leg.
  5. A Sunday-dated transaction's UAH rollup uses Friday's NBU rate; the API response makes the rate date and source visible.
**Plans:** TBD
**UI hint:** no
**Notes / Risks:**
  - **Pitfall 7 (NBU weekend gaps):** empty array on a non-business day means "no rate today", not "rate is 0". Fallback query: `MAX(rate_date) WHERE rate_date <= transaction_date`.
  - **Pitfall 8 (multi-hop FX):** never double-convert via NBU when Mono already gave you the account-currency amount. Write a property test: rollup must equal `account_currency_amount × NBU_rate(account_currency, attributed_day)`.

### Phase 4: Categorized Spending
**Goal:** Bohdan opens the app and most of his transactions already have a sensible category attached, courtesy of MCC defaults and his own rules. When auto-logic is wrong, he fixes one row, and re-running rules over history never clobbers his manual fix again.
**Mode:** mvp
**Depends on:** Phase 1, Phase 2 (rules need transactions to run on; lock columns must already exist on the schema from Phase 1's groundwork)
**Why this phase exists:** "Where my money goes" requires transactions to be grouped meaningfully. Without categorization, the dashboard is a flat list — visibility without insight. Without manual-lock semantics, every re-run undoes Bohdan's corrections, defeating the "zero upkeep" promise.
**Requirements:** CAT-01, CAT-02, CAT-03, CAT-04, CAT-05
**Success Criteria** (what must be TRUE):
  1. On first connect, every imported transaction is auto-categorized by the rules engine using a default ~15-category taxonomy seeded from MCC groups (Groceries, Cafe/Restaurants, Transport, Utilities, Entertainment, etc.); uncategorized rows are visibly marked, never silently bucketed.
  2. Bohdan creates a rule via `POST /api/rules` with composable predicates (e.g., `mcc IN [5411, 5499] AND amount_minor < 0 AND description ICONTAINS "ATB"`), priority-ordered; first-match-wins on category. The structured predicate JSON contains no eval/regex-of-doom — only a fixed op vocabulary.
  3. Bohdan calls "run rules over history" and gets a diff preview ("47 transactions will change category, 3 will be overwritten") before commit; after confirm, only the targeted rows update and `is_user_locked = 1` rows are skipped unconditionally.
  4. After Bohdan manually re-categorizes a transaction (sets `category_source = manual`, `is_user_locked = 1`), every subsequent rule run, including on next import, leaves that row alone.
  5. The category list is fully user-editable: Bohdan can rename, recolor, or add categories via `PATCH /api/categories` and `POST /api/categories`; rules referencing a deleted category surface a clear error rather than silently corrupting state.
**Plans:** TBD
**UI hint:** no
**Notes / Risks:**
  - **Pitfall 10 (manual edits clobbered):** `is_user_locked` and `category_source` columns must already exist from Phase 1 schema groundwork; the engine skips locked rows unconditionally on every pass.
  - **Pitfall 21 (MCC long tail):** MCC is one signal among many. Rules engine must allow user override at per-merchant-pattern level. Avoid hardcoded "MCC → category" only.
  - **Pitfall 8 / Anti-pattern 8 (eval-based predicates):** rules are structured JSON with a fixed op vocabulary, never `eval()` of a Python expression string.

### Phase 5: Honest Totals
**Goal:** When Bohdan transfers UAH from his card to his jar, or when a merchant refunds him, the dashboard math doesn't lie. Internal transfers between his own Mono accounts/jars/cards are detected and excluded from spending. Refunds are paired with their original charges and the pair nets to zero in spending views. Two legitimate identical-amount coffees on the same day are NOT collapsed.
**Mode:** mvp
**Depends on:** Phase 4 (reconciliation runs after categorization in the pipeline)
**Why this phase exists:** Without this, "this month spending" includes jar top-ups as expenses and shows refunds as income — every number is wrong by sometimes-large amounts. The Core Value of visibility requires totals that match what Bohdan's bank app would show.
**Requirements:** REC-01, REC-02, REC-03
**Success Criteria** (what must be TRUE):
  1. When Bohdan moves 5,000 UAH from his card to a jar, both legs are detected as an `internal_transfer` (opposite sign, same amount in common currency, ±2 days, both accounts user-owned), automatically paired at confidence ≥ 0.8, and excluded from "this month spent". Lower-confidence candidate pairs are surfaced via `GET /api/links/pending` for user confirmation — never auto-hidden silently.
  2. When a merchant refunds Bohdan, the original charge and the refund (same account, opposite sign, matching amount, overlapping counterparty/MCC, within ±60 days) are paired as `refund` and net to zero in spending views; the original charge still appears in transaction-detail with the linked refund.
  3. Backfill of an overlapping window (re-importing the last 7 days when 6 days are already present) does NOT collapse two legitimately identical 50-UAH coffees on the same day from different cards into one — dedup is keyed on `(account_id, source_tx_id)`, not on heuristic content match.
  4. Auto-paired transfers and refunds are reversible: Bohdan can call `DELETE /api/transactions/{id}/link/{link_id}` to unlink a false positive, and re-running reconciliation does not re-create the link without a fresh signal.
**Plans:** TBD
**UI hint:** no
**Notes / Risks:**
  - **Pitfall 9 (transfer false positives):** require ≥3 signals for auto-pair; 2-signal candidates surface for confirmation; never silently auto-hide. A salary deposit and an unrelated same-day same-amount expense must NOT auto-pair.
  - **Pitfall 28 (refund matching unrelated charges):** two coffees three weeks apart matching as "purchase + refund" is the failure case; require overlapping counterparty/MCC, not just amount + window.
  - **Pitfall 22 (jars look different):** card↔jar transfers fire on both sides with mirrored signs; the internal-transfer logic catches them via the same-user-owned signal.

### Phase 6: This Month UI
**Goal:** Bohdan opens the app on his phone (375px wide, real device) and within five seconds sees: total spent this month in UAH, top categories, comparison vs prior month clipped to the same day-of-month. He can scroll the transaction feed, filter by date/account/category, search by merchant, click a row to see the matched rule and raw Mono payload, quick-recategorize from the row, manually edit/merge/split a transaction, and add a cash transaction the importer can't see.
**Mode:** mvp
**Depends on:** Phase 3 (UAH rollup), Phase 4 (categories + lock semantics), Phase 5 (transfer/refund netting)
**Why this phase exists:** This is where the spine becomes a product. Everything before this phase happens server-side; this is what Bohdan actually sees and uses daily. It is also the phase where mobile-responsive design and the manual-edit surface land — both cross-cutting concerns scoped to where the user encounters them.
**Requirements:** UI-01, UI-02, UI-03, UI-04, UI-05, MAN-01, MAN-02, MAN-03
**Success Criteria** (what must be TRUE):
  1. Bohdan opens the dashboard at any time of day in any month and sees three numbers without scrolling: total spent this month (calendar month, Europe/Kyiv, UAH-rolled), top 3 categories, and a labeled comparison vs prior month with both periods clipped to the same day-of-month for fair comparison ("01–10 May 2026 vs 01–10 April 2026").
  2. Bohdan opens the transaction feed on a real 375px-wide phone browser; columns reflow, touch targets are sized for thumbs, and cursor pagination over `(occurred_at DESC, id DESC)` returns 50 rows per page with no duplicates or skips even when new rows arrive between page loads.
  3. Bohdan filters the feed by date range, account, category, and merchant substring search; results update without a page reload; each row exposes a quick-recategorize control (single dropdown or keyboard shortcut) that sets `category_source = manual`, `is_user_locked = 1`.
  4. Clicking a transaction opens a detail drawer showing the matched rule (or "no match"), the full raw Mono payload, the FX rate used for UAH rollup, and a link out to Mono's receipt page when `receiptId` is present.
  5. Bohdan adds a cash transaction (`POST /api/transactions/cash`) with amount, currency, date, description, and category; it appears in the feed and dashboard like any other transaction, with `source = manual_cash` and `raw_payload` left null. Existing transactions can be edited, merged, or split, and `raw_payload` of the underlying source rows is never mutated.
**Plans:** TBD
**UI hint:** yes
**Notes / Risks:**
  - **Pitfall 16 ("this month" boundary):** label the period explicitly. Default to calendar month, Europe/Kyiv. Comparison clips both periods to today's day-of-month; never show "120% vs prior month" on day 1.
  - **Pitfall 14 (timezone):** `attributed_day` derived via `zoneinfo.ZoneInfo("Europe/Kyiv")`, not `pytz`. A 23:30 Kyiv-time transaction is attributed to that Kyiv calendar day, not UTC's.
  - **Pitfall 25 (localStorage PII):** do NOT cache transaction data in localStorage. Fetch fresh on every load. Mono token never reaches the browser.
  - **Pitfall 27 (mobile viewport):** test on Bohdan's actual target phone, not just desktop devtools "iPhone preview".

### Phase 7: Ship Ready
**Goal:** Before declaring v1, the operational guarantees are real, not aspirational. A daily `pg_dump` lands in a bind-mounted backup directory, the restore procedure is documented and tested manually on a fresh container, CSV import works as a fallback ingestion path, full data export to CSV and JSON works, and the no-telemetry promise is documented in-app with the exact list of network egress destinations.
**Mode:** mvp
**Depends on:** Phase 6 (the UI surfaces export/import controls and the about page)
**Why this phase exists:** Self-hosted means Bohdan is his own DBA. Without backup/restore tested before v1 ships, the first NAS disk failure or `docker compose down -v` typo erases everything. Without an export, the privacy promise of "leave anytime" rings hollow. These are release-blockers, not nice-to-haves.
**Requirements:** OPS-02, OPS-03, OPS-05
**Success Criteria** (what must be TRUE):
  1. A daily cron job runs `pg_dump` to a bind-mounted `${DATA_DIR}/backups/` directory; the last 30 daily, 12 monthly snapshots are retained; the README documents the restore procedure ("stop container, restore file, restart") and Bohdan has performed it manually once before declaring v1 done.
  2. Bohdan can upload a CSV of historical transactions Mono can't see (legacy bank exports, etc.) via `POST /api/import/csv` and the rows land in the same normalized schema with `source = csv_import`; idempotency is content-hash based for CSV-sourced rows.
  3. Bohdan can call `GET /api/export?format=csv` and `GET /api/export?format=json` and receive a complete dump of his data — including raw payloads for the JSON variant — that round-trips back via re-import without data loss.
  4. The app's "About" page lists exactly the outbound network destinations the app contacts (`api.monobank.ua`, `bank.gov.ua`) and the README documents that no analytics/telemetry SDKs are present; `grep`-able evidence: zero `sentry-sdk`/`posthog`/`mixpanel`/`segment` deps in `pyproject.toml` or `package.json`.
**Plans:** TBD
**UI hint:** yes
**Notes / Risks:**
  - **Pitfall 18 (no backup = total loss):** `${DATA_DIR}/backups/` must populate on day one of this phase, not "soon". Test restore manually before declaring done.
  - **Pitfall 12 (named volume vs bind mount):** backup directory and Postgres data directory are bind mounts so `docker compose down -v` can't wipe them. Document `PUID`/`PGID` for Synology/Unraid users.
  - **Pitfall 13 (migrations after gap):** pre-flight backup hook on container start (copy current DB to `backups/pre-migration-${sha}/`) before running `alembic upgrade head`.

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. First Real Transaction | 0/4 | Planned | - |
| 2. Reliable Sync | 0/? | Not started | - |
| 3. UAH Truth | 0/? | Not started | - |
| 4. Categorized Spending | 0/? | Not started | - |
| 5. Honest Totals | 0/? | Not started | - |
| 6. This Month UI | 0/? | Not started | - |
| 7. Ship Ready | 0/? | Not started | - |

## Coverage

- **v1 requirements:** 35 total
- **Mapped to phases:** 35
- **Unmapped:** 0
- **Duplicates:** 0

All v1 REQ-IDs in REQUIREMENTS.md are mapped to exactly one phase. See REQUIREMENTS.md Traceability section for the full mapping.

---
*Roadmap defined: 2026-05-10*
*Last updated: 2026-05-10 after initial creation*
