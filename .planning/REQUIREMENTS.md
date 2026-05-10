# Requirements: finance-bro

**Defined:** 2026-05-10
**Core Value:** Automatic visibility into where my money goes — zero manual upkeep, on hardware I own.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Ingestion

- [ ] **ING-01**: Pull transactions from Monobank personal API (`api.monobank.ua/personal/`) across cards, jars, and FOP accounts via `/personal/client-info` and `/personal/statement`
- [ ] **ING-02**: Single token-bucket gate enforces 1 req/60s across all Mono callers; persisted to disk so restarts cannot violate the rate limit
- [ ] **ING-03**: Persist accounts and transactions in Postgres with the full Mono `statementItem` retained as `raw_payload` JSON per row
- [ ] **ING-04**: Composite idempotency key `(account_id, source_tx_id)` prevents duplicate inserts on re-import
- [ ] **ING-05**: Hold/pending transactions ingested with `hold` flag; excluded from totals; updated in-place when same `id` arrives with `hold=false`
- [ ] **ING-06**: Chunked, resumable backfill in ≤30-day windows; `last_cursor` persisted so a crashed backfill resumes exactly where it stopped
- [ ] **ING-07**: Soft-delete model for transactions; `raw_payload` is immutable
- [ ] **ING-08**: Polling status surfaced in UI: last poll timestamp, last error, 401/429 distinguished

### Multi-Currency

- [ ] **FX-01**: Store transaction amount in original currency (UAH/USD/EUR distinct, signed minor units `BIGINT` + ISO-4217 alpha currency)
- [ ] **FX-02**: NBU FX rates fetched daily (16:00 Kyiv); 12-month historical backfill on first run; weekend/holiday fallback uses most-recent prior business-day rate
- [ ] **FX-03**: UAH rollup computed on read via NBU rate at transaction-day; never denormalized into a stored column
- [ ] **FX-04**: For FX-on-card transactions, use Mono's account-currency `amount` directly; do not double-convert via NBU

### Categorization

- [ ] **CAT-01**: Rules engine with composable predicates: merchant substring/regex, `mcc` and `originalMcc`, amount sign/range, account, currency, counterparty `IBAN`/`EDRPOU`, comment, hold flag
- [ ] **CAT-02**: User-controlled rule priority list; first-match-wins on category
- [ ] **CAT-03**: Default ~15-category taxonomy seeded from MCC groups; user-editable categories
- [ ] **CAT-04**: `category_source` and `is_user_locked` columns from day one; locked rows skipped by every categorizer re-run
- [ ] **CAT-05**: Run-rules-on-history with diff preview before commit

### Reconciliation

- [ ] **REC-01**: Detect internal transfers between user-owned Mono accounts/jars/cards (opposite sign + same amount in common currency + within ±2 days); auto-pair at confidence ≥0.8; surface lower-confidence pairs for user confirmation; never auto-hide unilaterally
- [ ] **REC-02**: Match refunds and reversals (same account, opposite sign, matching amount, overlapping counterparty/MCC, ±60 day window); paired transactions net to zero in spending views
- [ ] **REC-03**: Duplicate detection on re-import or backfill overlap is idempotency-key based, not heuristic — two legitimate identical-amount transactions on the same day are not collapsed

### Manual Entry & Edits

- [ ] **MAN-01**: Manually edit, merge, split, or re-categorize transactions; `raw_payload` remains immutable
- [ ] **MAN-02**: Manually add cash transactions (`source = manual_cash`); editable like any other transaction
- [ ] **MAN-03**: User edits flagged so re-running rules cannot overwrite them

### Dashboard & Feed

- [ ] **UI-01**: "This month" dashboard — total spent, top categories, comparison vs prior month (calendar month, Europe/Kyiv); periods clipped to same day-of-month for fair month-over-month comparison
- [ ] **UI-02**: Transaction feed with filter, sort, search, cursor pagination
- [ ] **UI-03**: Quick re-categorize directly from the transaction feed
- [ ] **UI-04**: Transaction detail drawer showing matched rule and raw payload; receipt link-out when Mono's `receiptId` is present
- [ ] **UI-05**: Responsive web UI working on a real 375px-wide phone browser

### Operations & Privacy

- [ ] **OPS-01**: Token entry, validation, rotation; token encrypted at rest
- [ ] **OPS-02**: Daily `pg_dump` to bind-mounted backup directory; restore procedure documented and tested before v1 ships
- [ ] **OPS-03**: CSV import as fallback ingestion path; CSV and JSON full data export
- [ ] **OPS-04**: Log redaction on by default (Mono token, `X-Token` header, transaction amounts at `INFO+`)
- [ ] **OPS-05**: No analytics/telemetry SDKs; documented network egress (Mono and NBU only)

### Deploy

- [ ] **DEP-01**: Single-compose deploy (`app` + `db` services); bind-mount data directory; documented `PUID`/`PGID`
- [ ] **DEP-02**: Network-gated access only — no app-level authentication; Tailscale/LAN is the trust boundary

## v2 Requirements

Deferred to a post-v1 release. Tracked but not in current roadmap.

### Visualization

- **V2-VIS-01**: Top-merchants view (pivot on normalized payee)
- **V2-VIS-02**: Calendar heatmap of spending intensity
- **V2-VIS-03**: Tags as orthogonal axis to categories (join table, chip UI)

### Categorization

- **V2-CAT-01**: Auto-rule suggestion from manual recategorizations (after ≥50 overrides)
- **V2-CAT-02**: LLM categorizer (local Ollama or API) plugged in via `Categorizer` port

### Reconciliation

- **V2-REC-01**: Smart "looks like a transfer" prompt for low-confidence transfer pairs

### Importers

- **V2-IMP-01**: PrivatBank importer
- **V2-IMP-02**: Wise importer
- **V2-IMP-03**: Revolut importer

### Operations

- **V2-OPS-01**: Webhook ingestion for near-real-time updates (requires public HTTPS endpoint — only viable if hosting model changes)
- **V2-OPS-02**: App-level authentication (passphrase + signed cookie) for non-LAN exposure

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Budgets / envelopes / category limits | Core Value is visibility, not planning; budgets demand ongoing maintenance the user is fleeing |
| Cashflow forecasting | Requires recurring-transaction model and future entries — different product surface |
| Savings-goal progress tied to jars | Planning UX; jar balance + goal as two numbers is enough, no progress bar |
| Alerts / email / push notifications | No push channel in homelab+Tailscale model; SMTP/Telegram bot is out-of-scope moving part |
| Multi-user / household accounts | Single-user by design; multi-tenancy is a v3 conversation |
| Investment / brokerage / net-worth tracking | Different product domain; Mono does not expose investment data |
| LLM categorization in v1 | Build rules first, observe the long tail, then choose local vs API LLM with full info |
| Real-time UI updates | Mono rate limit is 1 req/60s; real-time is structurally impossible — show "last polled N min ago" |
| AI-generated insights / summaries | Dashboard being good IS the insight; avoid LLM dependency in v1 |
| Cloud sync | Defeats trust model — user-controlled hardware is the point |
| Plaid / Salt Edge / bank aggregators | These ARE the third-party data exposure the user is avoiding |
| PWA / installable / offline mode | Responsive browser is sufficient; service worker adds scope for no stated need |
| Public internet exposure | Homelab + VPN model; if this changes, auth + threat model must be re-evaluated first |

## Traceability

Empty initially. Populated by the roadmap agent — each requirement maps to exactly one phase.

| Requirement | Phase | Status |
|-------------|-------|--------|
| ING-01 | TBD | Pending |
| ING-02 | TBD | Pending |
| ING-03 | TBD | Pending |
| ING-04 | TBD | Pending |
| ING-05 | TBD | Pending |
| ING-06 | TBD | Pending |
| ING-07 | TBD | Pending |
| ING-08 | TBD | Pending |
| FX-01 | TBD | Pending |
| FX-02 | TBD | Pending |
| FX-03 | TBD | Pending |
| FX-04 | TBD | Pending |
| CAT-01 | TBD | Pending |
| CAT-02 | TBD | Pending |
| CAT-03 | TBD | Pending |
| CAT-04 | TBD | Pending |
| CAT-05 | TBD | Pending |
| REC-01 | TBD | Pending |
| REC-02 | TBD | Pending |
| REC-03 | TBD | Pending |
| MAN-01 | TBD | Pending |
| MAN-02 | TBD | Pending |
| MAN-03 | TBD | Pending |
| UI-01 | TBD | Pending |
| UI-02 | TBD | Pending |
| UI-03 | TBD | Pending |
| UI-04 | TBD | Pending |
| UI-05 | TBD | Pending |
| OPS-01 | TBD | Pending |
| OPS-02 | TBD | Pending |
| OPS-03 | TBD | Pending |
| OPS-04 | TBD | Pending |
| OPS-05 | TBD | Pending |
| DEP-01 | TBD | Pending |
| DEP-02 | TBD | Pending |

**Coverage:**
- v1 requirements: 35 total
- Mapped to phases: 0 (will be filled by the roadmapper)
- Unmapped: 35 ⚠️ (expected at this stage)

---
*Requirements defined: 2026-05-10*
*Last updated: 2026-05-10 after initial definition*
