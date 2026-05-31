# finance-bro

## What This Is

A self-hosted personal-finance tool for one person. It pulls transactions from Monobank, categorizes them automatically, reconciles duplicates and internal transfers, and shows where the money actually goes — all on hardware the user controls. Built for someone who is frustrated with existing budgeting tools that demand manual upkeep and won't host their financial data on third-party clouds.

## Core Value

**Automatic visibility into where my money goes — zero manual upkeep, on hardware I own.**

Every prioritization tradeoff resolves toward: less manual work for the user, and more privacy. Features that demand ongoing tagging, that ship data to third parties, or that move attention away from the spending picture lose.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

- [x] Hybrid categorization framework — user-editable rules engine shipped in Phase 4: MCC-default taxonomy + composable structured-predicate rules (no eval), auto-categorize on import, run-rules-over-history with diff preview, and manual-lock semantics that the engine never clobbers (CAT-04 held transaction-safely). LLM categorizer remains pluggable but deferred. _(Validated in Phase 4: Categorized Spending)_

### Active

<!-- Current scope. Hypotheses until shipped and validated. -->

- [ ] Pull transactions from Monobank via personal-token polling (api.monobank.ua/personal/)
- [ ] Persist accounts, jars, and transactions in a local database with full source payload retained
- [ ] Multi-currency model: UAH, USD, EUR kept distinct; rolled up to UAH at transaction-day FX rate
- [ ] Detect and collapse duplicate transactions on re-import or backfill overlap
- [ ] Detect internal transfers between user's own Mono accounts/jars/cards so they don't show as expense+income
- [ ] Match refunds and reversals to their original transactions so the pair nets to zero in spending views
- [ ] Manually edit, merge, split, or re-categorize transactions when auto-logic is wrong
- [ ] Manually add cash transactions the importer can't see
- [ ] "This month" spending dashboard: total spent, top categories, comparison vs prior month
- [ ] Transaction feed with quick re-categorize
- [ ] Responsive web UI usable in a mobile browser
- [ ] Docker deployment runnable on a homelab/NAS (single compose file or image)
- [ ] Network-gated access only — no app-level auth; assume Tailscale/LAN trust boundary

### Out of Scope

<!-- Explicit boundaries. Reasoning included to prevent re-adding. -->

- Other data sources (PrivatBank, Wise, Revolut, IBKR, crypto, on-chain) — design importer interface to be extensible, but build only Monobank in v1; revisit once Mono path is rock solid
- Investment / brokerage / net-worth tracking — Core Value is spending visibility, not wealth tracking; defer until v1 is validated
- Budgets, alerts, cashflow forecasting, savings goals — Core Value is visibility, not planning; users will ask for this once visibility is solved
- LLM-driven categorization (in v1) — build rules first, observe the long tail, then choose between local (Ollama) and API LLM with full information
- Mobile push notifications — would require a public endpoint and scope creep; defer with PWA
- PWA / installable / offline mode — responsive browser is enough for the daily use described; defer
- Multi-user, sharing, household accounts — single-user by design; do not build multi-tenancy code paths
- App-level authentication (login, passwords, passkeys) — homelab + Tailscale/LAN is the trust boundary; revisit only if hosting model changes
- Public internet exposure — homelab + VPN model; if this changes, auth + threat model must be re-evaluated first
- Webhook ingestion (in v1) — needs public endpoint; polling at the rate limit is sufficient given 1 req/60s constraint

## Context

- **Single user**: Bohdan. Located in Ukraine, lives primarily in UAH with USD/EUR holdings — multi-currency is reality, not an edge case.
- **Motivation**: Frustration with existing budgeting tools. Two pain points named explicitly: (1) too much manual work — categorizing every transaction, importing CSVs, fixing duplicates by hand; (2) privacy concerns about sending raw bank-feed data to third-party SaaS (Mint, YNAB, Lunch Money, etc.).
- **Source ecosystem (v1)**: Monobank UA exposes a public personal API at `api.monobank.ua/personal/` using a per-user token. Hard rate limit of 1 request per 60 seconds per token. Webhooks exist but require a publicly reachable endpoint, which conflicts with the homelab+VPN model — not used in v1.
- **Currencies in scope**: UAH, USD, EUR — kept distinct in storage, with a UAH rollup view computed at transaction-day rate. NBU publishes daily official rates for UAH that should be the source of truth for FX conversion.
- **Deployment target**: A homelab box (NAS / mini-PC / Pi) running Docker. LAN access is primary; remote access via Tailscale or equivalent overlay VPN. No managed cloud, no third-party DBaaS.
- **Trust boundary**: Network. The app does not assume hostile traffic on its HTTP surface in v1 — Tailscale/LAN gates access. Auth is therefore not in v1.
- **No prior code**: Greenfield repo. No legacy constraints. No existing user data to migrate.

## Constraints

- **Privacy**: No third-party cloud services for primary data storage or processing. All transaction data lives on user-controlled hardware. — Privacy is a stated motivation; violating this defeats the purpose of building it.
- **Tech stack**: Python backend, JS frontend. — User preference; favors Python's LLM tooling ecosystem for the eventual hybrid-categorizer work.
- **Deployment**: Docker on homelab/NAS. Single `docker compose up` style deploy. — Target environment is a home server, not a managed cloud.
- **External API**: Monobank personal API, 1 request per 60 seconds per token. — Hard rate limit; informs poll cadence and backfill strategy.
- **Single-user**: No multi-tenancy. — Stated requirement; building it in is wasted complexity.
- **Network-gated**: No app-level auth in v1. — Tailscale/LAN handles access control; revisit only if exposure model changes.
- **Time horizon**: Solid MVP, ~1–2 months of evening work. — User's stated capacity; rules out long-tail polish for v1.
- **Scope discipline**: Visibility, not planning. — Core Value rules out budgets/forecasts/alerts in v1 even when "while we're at it" tempting.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Self-hosted on homelab via Docker | Privacy is the primary motivation; user controls the hardware and network | — Pending |
| Monobank-only for v1 with extensible importer interface | Don't over-design; ship one source well, prove the model, then add others | — Pending |
| Hybrid categorization, LLM deferred to v1.5+ | Build rules first; observe the actual long tail before choosing local vs API LLM | — Pending |
| Multi-currency: UAH/USD/EUR distinct + UAH rollup at txn-day rate | Reflects reality of a UA user with foreign-currency cards; single-base would lose information | — Pending |
| No app-level auth — network-gated only | Homelab + Tailscale/LAN provides trust boundary; auth is added complexity for no gain in this model | — Pending |
| Personal-token polling, not webhooks | Works behind any network; trade real-time for simplicity given 1 req/60s rate limit | — Pending |
| Python backend, JS frontend | User preference; ecosystem favors future LLM categorizer work | — Pending |
| Responsive web only — no PWA/push in v1 | Mobile browser is sufficient for stated use; avoid scope creep | — Pending |
| Visibility-first scope (no budgets/forecasts) | Core Value is "see where money goes"; planning features are downstream of solving visibility | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-05-31 after Phase 4 (Categorized Spending) completion*
