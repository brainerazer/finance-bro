# Feature Research

**Domain:** Self-hosted personal-finance / spending-visibility tool (Monobank-only v1, single-user)
**Researched:** 2026-05-10
**Confidence:** HIGH on competitive feature gravity (well-documented public products); HIGH on Mono API field shape (cross-checked against multiple client libraries and the official docs); MEDIUM on "what other Mono importers got wrong" (relies on README claims and a small number of GitHub issue threads — limited dataset).

---

## Framing — The Two Forces

Every feature in this section was scored against two anchors:

1. **Core Value** (PROJECT.md): "Automatic visibility into where my money goes — zero manual upkeep, on hardware I own." Visibility, not planning. Anything that biases toward planning (budgets, forecasts, alerts, savings goals) loses, even if competitors have it.
2. **The Mono API shape** (`api.monobank.ua/personal/`): personal-token auth, **31-day + 1h** statement window per request, **1 request / 60s / token** hard rate limit, `time` is the only timestamp (no separate `operationDate`), `hold: bool` is the only pending/cleared signal, transactions carry `mcc`/`originalMcc`/`comment`/`receiptId`/`invoiceId`/`counterEdrpou`/`counterIban`, and **jars and FOP accounts are first-class** alongside personal cards in `/personal/client-info`. ([Monobank docs](https://api.monobank.ua/docs/index.html), [go-monobank types](https://pkg.go.dev/github.com/vtopc/go-monobank))

Where these two forces conflict (e.g., users *want* budgeting, the product *won't* build it), the Core Value wins and the feature lands as an anti-feature.

---

## Feature Landscape

### Table Stakes (Users Expect These)

These are the features whose absence makes the tool feel broken. They are boring; users won't praise them but will reject the product without them. All must ship in v1.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **Polling-based ingestion from Monobank with backfill** | Without it, there is no product. Mono's `/personal/statement` is the only data path that works behind a homelab firewall — webhooks need a public endpoint and are explicitly out of scope. | M | Cadence: poll on the 1-req/60s budget per token. For backfill, walk the 31-day window backward in 60s-spaced calls. Must never burn the rate-limit on the user-facing UI thread. ([Mono rate limit](https://github.com/vitalik/python-monobank), [monobudget](https://github.com/smaugfm/monobudget)) |
| **Per-account ingestion (cards, jars, FOP)** | `/personal/client-info` returns all of these together; ignoring jars or FOP would silently drop transactions. Mono FOP accounts have additional fields (`counterEdrpou`) and have historically been mis-routed to personal accounts by other tools. | M | Treat `Account` and `Jar` as separate ingestion targets — they hit different statement endpoints (`/statement/{accountId}` vs `/statement/{jarId}`). FOP accounts can carry `counterEdrpou`/`counterIban` that personal accounts don't. ([go-monobank](https://pkg.go.dev/github.com/vtopc/go-monobank), [FOP integration issue](https://opencartforum.com/en/files/file/9243-vipiska-z-rahunku-fop-v-privatbanku-ta-monobanku-dlya-opencart/)) |
| **Persist full source payload alongside normalized rows** | Re-derivable. If categorization logic changes or a field's meaning is misinterpreted, raw `statementItem` JSON is the only safe replay source. Already in PROJECT.md. | S | Store the original Mono `statementItem` JSON verbatim in a `raw_payload` column. Cost is small (Mono items are < 1 KB). Pays for itself the first time a rule misfires. |
| **Idempotent re-import / duplicate collapse** | Backfill windows overlap; Mono will return the same item again. Without idempotency the DB doubles every poll. This is the #1 bug reported across every importer ecosystem (Actual Budget, Firefly III, ynab-bank-importer, beancount-import). | S–M | Use Mono `statementItem.id` as the natural unique key (it's a stable opaque string per-account). Add a content-hash fallback for manual/CSV imports that lack a stable ID. ([Firefly III duplicate detection](https://docs.firefly-iii.org/references/data-importer/duplicate-detection/), [Actual Budget #2519](https://github.com/actualbudget/actual/issues/2519), [ynab-bank-importer #36](https://github.com/gitviola/ynab-bank-importer/issues/36)) |
| **Internal-transfer detection (no double-count)** | If a UAH→USD jar transfer shows as a 5000 UAH expense + a 120 USD income, "this month spending" is wrong. Every prior-art tool considers this a critical correctness feature. PROJECT.md lists it explicitly. | M | Match by: same user (always true), opposite-sign amounts, time-window correlation (≤ 5 min), and signal heuristics from `description` (Mono uses recognizable transfer/jar verbiage). Monobudget uses similar logic. ([monobudget transfer recognition](https://github.com/smaugfm/monobudget), [Firefly transfer matching](https://docs.firefly-iii.org/how-to/data-importer/import/transfers/)) |
| **Multi-currency: UAH / USD / EUR distinct, with UAH rollup at txn-day rate** | A UA user with foreign-currency cards has multi-currency as the daily case, not the edge. Actual Budget's lack of native multi-currency is a known frustration. | M | Store `amount` (account ccy, minor units) AND `operationAmount` (txn ccy) AND `currencyCode` (ISO 4217 numeric, as Mono returns it) AND `mccDate`/`time` separately. Pull NBU daily rate at txn-day for rollup. Always show *which* rate was used. ([Actual multi-currency #2147](https://github.com/actualbudget/actual/issues/2147), [Mono fields](https://pkg.go.dev/github.com/vtopc/go-monobank)) |
| **Refund / reversal pairing** | Returns must net to zero in spending views, otherwise the dashboard lies. Tax/accounting tools have done this for decades; users have learned to expect it. | M | Match on: same `mcc` (or original mcc), same merchant string fuzzy-match, opposite-sign amounts within a fuzz window, and within a 90-day pairing window. Show the pairing in UI, allow user to confirm/reject. |
| **Manual edit / merge / split / re-categorize** | Auto-logic will be wrong. Without manual override the whole tool fails the moment a rule misfires. Universal across Lunch Money, Actual, Firefly, Tiller, Monarch. | M | Edits must NOT mutate `raw_payload`. Splits should preserve total = sum-of-parts invariant. Merges should be reversible (Lunch Money's "unsplit" is the right model). ([Lunch Money split/unsplit](https://support.lunchmoney.app/finances/transactions/other-features)) |
| **Manual cash transaction entry** | Mono can't see cash. If cash isn't enterable, "where did my money go" has a permanent blind spot. PROJECT.md lists it. | S | Same shape as a Mono transaction but with a `source = 'manual_cash'` flag and no `raw_payload`. Treat the user as the source of truth for the amount. |
| **Rules engine (auto-categorization)** | Universal in this category. AutoCat (Tiller), Filters (PocketSmith), Rules (Actual, Firefly, Lunch Money) — all ship one. PROJECT.md commits to a rules-first hybrid. | M | See "Rules engine shape" below — composable conditions on merchant string + MCC + amount sign + counterparty fields, ordered list with first-match-wins semantics. Critical: rules must run on backfilled data too, not just on new ingest. ([Actual rules](https://actualbudget.org/docs/budgeting/rules/), [Lunch Money rules](https://support.lunchmoney.app/setup/rules), [Tiller AutoCat](https://tiller.com/courses/getting-started-with-tiller/lessons/getting-started-with-tiller-part-3-of-8-customize-your-categories-and-keep-organized-with-autocat/)) |
| **Default category taxonomy** | Forcing a user to invent 30 categories on first run is a quit-trigger. Every tool ships a starter set. | S | Ship ~12–18 categories aligned to common UA spending (Groceries, Cafe/Restaurants, Transport, Utilities, Subscriptions, Entertainment, Health, Clothes, Travel, ATM/Cash, Transfers, Income, Other). User can edit. Map to Mono MCCs as defaults. ([MCC dataset for UA](https://github.com/Oleksios/Merchant-Category-Codes)) |
| **"This month" spending dashboard** | The named primary view in PROJECT.md. Total + top categories + month-over-month delta. | M | Per the multi-currency model, dashboard is in UAH-rollup by default with a per-currency toggle. Three numbers visible without scrolling: total spent, top 3 categories, vs prior month. |
| **Transaction feed with filter + search + quick re-categorize** | Universal. Lunch Money, PocketSmith, Actual all have it as the central UI. | M | Filter by date, account, category, merchant substring, amount range. Quick re-categorize = single keystroke or dropdown without leaving the row (Actual's `C` shortcut is the gold standard). ([Actual shortcuts](https://actualbudget.org/docs/getting-started/tips-tricks/), [PocketSmith bulk actions](https://www.pocketsmith.com/blog/transaction-filters-bulk-actions-and-personal-summary-averages/)) |
| **Hold/pending transaction handling** | Mono's `hold: true` items have no final amount and may change before clearing. Showing them as final spending creates ghost numbers in dashboards. | S | Either (a) ingest holds and flag them, exclude from totals; (b) skip ingestion until `hold: false`. Recommended (a) — visibility into pending matters for the daily-feed use case, but exclude from rolled-up dashboard numbers. |
| **Responsive web UI usable on a phone browser** | PROJECT.md commits to this. The "while standing in the grocery store check what category that just went into" flow needs mobile. | M | Tailwind/responsive grid; transaction feed must work on a 375px-wide viewport. No PWA, no install banner — that's out of scope. |
| **Docker deployment (single compose file)** | Self-hosted users expect `docker compose up`. Anything more is friction. PROJECT.md committed. | S | One image (or one app + one db image). All config via env. Healthcheck endpoint. Volume mount for the database. |
| **Full data export (CSV + JSON)** | "Leave anytime" is part of the privacy promise. Without an export the self-hosted angle rings hollow. Actual exports zip; Firefly exports CSV. | S | CSV (one row per normalized transaction) + JSON (with raw_payload). Single-button export covering all data. ([Actual backup/restore](https://actualbudget.org/docs/backup-restore/backup/), [Firefly export](https://docs.firefly-iii.org/tutorials/firefly-iii/exporting-data/)) |
| **Database backup / restore** | Self-hosted = user is their own DBA. Without a documented backup story they will lose everything once. | S | Daily SQLite (or Postgres) dump to a known volume path; documented restore. Actual Budget keeps 10 daily + 15min current — that's the bar. ([Actual backups](https://actualbudget.org/docs/backup-restore/backup/)) |
| **Polling status visibility** | "Is the importer working?" must be answerable from the UI in < 5s. Without this, the tool feels haunted when something silently breaks. | S | Show last-poll-time, last-success-time, last-error per token/account. Surface 401 (token revoked) and 429 (rate limited) explicitly. |
| **Token entry & rotation** | The personal token is the entire auth surface to Mono. It expires and gets revoked; replacing it must not require a redeploy. | S | UI to paste/replace the token. Validate by hitting `/personal/client-info` once. Store encrypted-at-rest (libsodium / fernet) — even on a homelab, plaintext-token-on-disk is bad hygiene. |
| **Show FX rate used for each rolled-up transaction** | Without it, users can't tell if "this 100 EUR coffee = 4500 UAH" rollup is right. Builds trust. PocketSmith does this for its multi-currency users. | S | A small `(@ rate)` annotation on rolled-up amounts + ability to drill into "what rate was used and where did it come from" (NBU daily). ([PocketSmith multi-currency](https://learn.pocketsmith.com/article/122-multi-currency-beta-features-in-pocketsmith)) |
| **CSV import as fallback** | If the importer breaks for any reason — token issue, Mono API outage, FOP weirdness — the user needs a manual escape hatch to keep their data complete. Every personal-finance tool supports it. Mono itself can export CSV from the personal cabinet. | S | CSV ingestion path that maps to the same normalized schema. Idempotency via content hash. |

### Differentiators (Competitive Advantage)

These are where this product can credibly beat Mint/YNAB/Lunch Money for *its* user. They are tied to the privacy/automation framing or to the fact that a single-user, single-source tool can be opinionated where SaaS tools can't.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **MCC + composable rules engine** | Most importers (monobudget excepted) match on merchant string only and miss the long tail. Mono ships an `mcc` *and* `originalMcc` on every transaction; using both is a 30-second feature with a multi-month payoff. | M | Rule conditions: `merchant_substring`, `merchant_regex`, `mcc IN (...)`, `mcc_group`, `amount_sign`, `amount_range`, `account_id`, `counterparty_iban`, `counterparty_edrpou`, `comment_contains`. ALL conditions AND'd within a rule; rules in user-ordered list, first match wins. Borrow Actual's "rules can write notes/payee/category in one pass" model. ([Actual rules](https://actualbudget.org/docs/budgeting/rules/), [monobudget MCC mapping](https://github.com/smaugfm/monobudget), [MCC dataset](https://github.com/Oleksios/Merchant-Category-Codes)) |
| **Auto-rule suggestion from manual recategorization** | Lunch Money and Copilot both do this — the system watches your overrides and offers to make a rule. Reduces upkeep dramatically over time, which is the #1 stated goal. | M | When user re-categorizes the second matching merchant, prompt: "Always categorize 'KAVA NA POSHTI' as Cafe/Restaurants?" — one-click rule creation from observed pattern. ([Lunch Money learns from behavior](https://support.lunchmoney.app/setup/categories/auto-categorization), [Copilot ML](https://help.copilot.money/en/articles/8182433-copilot-intelligence-for-spending)) |
| **Multi-currency that doesn't lie** | Actual Budget *still* (as of 2025) doesn't natively support this; users complain about it. A UA-targeted tool that handles UAH+USD+EUR cleanly out of the box is genuinely differentiated. | M | Already in table stakes; the differentiator is *honesty* about it: dashboards show ccy explicitly, rollup is opt-in, FX rate source is visible, never silent conversion. ([Actual #2147](https://github.com/actualbudget/actual/issues/2147)) |
| **Calendar heatmap / day-by-day spending** | Lunch Money added this in 2024; Monarch has it. Surprisingly absent from Actual and Firefly. Maps perfectly to the "where did my money go" question. | S–M | One screen: month grid, each day color-coded by total spend. Click a day → that day's transactions. Cheap once the dashboard backend exists. ([Lunch Money calendar](https://lunchmoney.app/features), [Monarch heatmaps](https://www.monarch.com/compare/ynab-alternative)) |
| **Top-merchants view (not just top-categories)** | Categories abstract away the actual question users have: "wait, how much did I spend at Silpo this month?" Monarch reports support this; most don't. Trivial once data is normalized. | S | A pivot on `description` (or normalized `payee` after rules clean it up). Top 10 by sum or count. |
| **Transaction-detail drawer with full source payload** | A self-hosted tool can show what an SaaS hides. "Why was this categorized as Cafe?" → show: rule matched, MCC, original Mono description, full statementItem JSON. | S | Detail panel with: rule that matched (or "no match"), MCC + group, both currency amounts, the Mono `comment`, `receiptId` (link to Mono's receipt page), counterparty IBAN/EDRPOU if present. |
| **Token + URL + log redaction by default** | Privacy is a stated motivation. Most importers leak tokens into stdout/logs at some point. Defaulting to redacted logs is cheap and on-brand. | S | Logger middleware that scrubs `X-Token` headers and substrings matching the token pattern. Document the redaction guarantee in README. |
| **Encryption at rest for tokens** | Even on a homelab, raw token in a SQLite file is bad hygiene if backups leave the box. Most self-hosted tools punt on this. | S | Encrypt the Mono token column with a key from env (`FINANCE_BRO_SECRET_KEY`) at rest using fernet/libsodium. The user accepts that losing the env key = re-paste the token. |
| **Telemetry-free by default + visible network egress list** | "We send nothing to third parties except `api.monobank.ua` and `bank.gov.ua` (NBU rates)" should be a documented promise, surfaced in-app. Differentiator vs Mint-style products that the user is fleeing. | S | No analytics SDKs, no telemetry endpoints, no error-reporting SaaS. About page lists every outbound host. |
| **Multi-token / multi-card per user** | A real Mono user has personal + jars + FOP, sometimes via different tokens (FOP token is separate). Most importers assume one token. PROJECT.md hints at this with "multiple Mono tokens / multiple cards." | M | Token is a first-class entity; `Account`/`Jar` belong to a token; multiple tokens per user. Each token has its own polling cadence (since 1-req/60s is per-token). Internal-transfer detection must work across tokens (two jars on different tokens are still both "yours"). |
| **Rate-limit-aware backfill** | Naive backfill burns the rate budget; smart backfill stays under it and shows progress. Differentiator over scrappy importers that fail silently after the third 429. | M | Job queue with token-scoped 60s minimum gap. Backfill walks 31-day windows backward; UI shows "backfill: 4 of 12 windows done." Treat 429 as a signal to back off, not retry-immediately. ([Mono rate-limit retry guidance](https://github.com/vitalik/python-monobank)) |
| **Receipt link-out for transactions with `receiptId`** | Mono provides a `receiptId` for many transactions; it links to the Mono cheque page. Differentiator: showing the receipt link inline saves a context switch. | S | Detail-drawer link: `https://check.gov.ua/...` or whatever the canonical Mono receipt URL is. Cheap pure-UI feature. |
| **Smart "this looks like a transfer" prompt for ambiguous cases** | When the heuristic isn't confident enough to auto-pair, surface candidates ("this 5000 UAH from Card A on Mar 10 matches a 5000 UAH on Jar B — pair?"). Better than silently miscategorizing or silently dropping. | M | UI dialog over a list of unpaired-but-suspicious candidates. Same UX as duplicate-detection in Lunch Money. ([Lunch Money dedup](https://support.lunchmoney.app/finances/transactions/other-features)) |
| **Run-rules-on-history (retroactive apply)** | When user adds a new rule, "would you like to apply it to existing 2,400 transactions?" — Lunch Money does this and it's the difference between "I'll get to it" and "fixed in 5 seconds." | S | Async job, idempotent. Show diff (N transactions affected) before commit. ([Lunch Money retroactive rules](https://support.lunchmoney.app/setup/rules)) |
| **Tags as orthogonal axis (not just category)** | Categories are mutually exclusive; tags are not. "Vacation Tbilisi 2026" cuts across Cafe/Transport/Hotels. Lunch Money and PocketSmith both ship tags. Cheap to add. | S | A `transaction_tags` join table. UI: comma-separated chip input. Filter+sum by tag. ([Lunch Money tags](https://lunchmoney.app/features/transactions)) |

### Anti-Features (Commonly Requested, Often Problematic)

These will be requested by the user-of-self at some point. They are *all* tempting because every neighbor product has them. They lose because they fight the Core Value or scope discipline.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| **Budgets / envelopes / category limits** | YNAB/Actual Budget *are* this; the gravity is huge. | Core Value is **visibility, not planning**. Budgets demand ongoing maintenance (the exact thing this user is fleeing) and shift attention from "where did my money go" to "did I stay under." Adding budgets v1 is the most likely way the project scope-creeps to death. | Build excellent visibility first. If the user *still* wants planning after 6 months of v1 use, they will know exactly which 4 categories matter. PROJECT.md says: defer until visibility is solved. |
| **Cashflow forecasts / "what will I have on the 30th"** | Calendar-budget tools (CalendarBudget, PocketSmith) lean on this. | Forecasting requires modeling recurring transactions, future-dated entries, scheduled bills — a whole new feature surface. Out of scope by Core Value. | Show last-3-months trend and let the user pattern-match it. Trends ≠ forecasts. |
| **Savings goals tied to jars** | Mono jars literally have a `goal` field. Tempting to surface as "you're 73% to your USD trip jar!" | This is a planning UX, not a visibility UX. It will demand goal-progress UI, milestone celebrations, etc. Out of scope. | Show jar balance + goal as two numbers in the account list. No progress bar, no nudge. |
| **Alerts / notifications ("you spent $500 on cafes this week")** | Every SaaS tool has them. Easy to imagine wanting them. | Alerts require a notification channel. The homelab + Tailscale model has no push channel. Email needs SMTP config. Telegram bot drags in another moving part. None of this is core. PROJECT.md lists push and PWA as out-of-scope. | Status surfaces in the dashboard the user already opens. Pull, not push. |
| **App-level auth (passwords, 2FA, passkeys)** | Reflexive ask for any web app that handles money. | PROJECT.md commits to network-gated only — Tailscale/LAN is the trust boundary. Adding auth doubles the security surface (sessions, password resets, account recovery) for zero gain in this trust model. | Document the threat model loudly: "this assumes you're on your LAN or VPN. If you expose this to the public internet, you broke the threat model." |
| **Multi-user / household / shared budgets** | Most consumer tools support this. | PROJECT.md is explicit: single-user. Building multi-user later is much easier than backing it out. Building it now means tenancy code paths everywhere. | Single-user code paths only. If "household" ever lands, run a second instance. |
| **Investment / brokerage / net-worth tracking** | Maybe Finance, Ghostfolio, Monarch make this central. | Out of scope per PROJECT.md. Mono doesn't expose investment data. Adding any of this drags in IBKR/crypto/on-chain APIs and a wholly different domain (positions, prices, FX-on-positions, lots). | "Spending visibility" is the entire surface. Net-worth view is a different product. |
| **LLM categorization in v1** | Maybe and others ship it; obvious appeal. | PROJECT.md says: "build rules first, observe the long tail, choose between local Ollama and API LLM with full information." Building LLM in v1 means the user can't measure its actual lift over rules, and it leaks data unless local-LLM is plumbed correctly. | Pluggable categorizer interface (rules engine implements it; LLM implementation can land later). Rules in v1; observation in v1; choice in v1.5. |
| **Webhook ingestion in v1** | Real-time updates. | Requires public endpoint, which breaks the homelab + Tailscale trust model. PROJECT.md out-of-scope. | Polling is fine — the user-facing loss is a few-minute delay on transactions. Acceptable for visibility. |
| **PWA / installable mobile app** | Closer to a "real app." | PROJECT.md out-of-scope. Adds service worker, offline, push channel surface — none of which fit the trust model. | Responsive web. Add-to-homescreen via OS — the user already has that. |
| **Cloud sync between devices** | Actual Budget supports it; users expect it. | The threat model is "data on hardware I own." A sync server is by definition another data location, and either lives on the same homelab (no value) or in the cloud (defeats the point). | Single-instance, single-DB. Backup/restore is the durability story. |
| **Real-time everything** | "Why isn't this showing instantly?" | Mono rate-limit is 1 req/60s. Real-time is fundamentally not possible from this API. Pretending otherwise creates UX lies. | Show "last polled N min ago" and let users mash a manual refresh. |
| **AI-generated insights ("you spent 30% more on cafes this month than last")** | Copilot, Monarch, modern apps. | (a) Long-tail noise, (b) drags in LLM dependency, (c) easy for the user to do themselves with a working dashboard. The dashboard *is* the insight. | Make the dashboard so good the insight is obvious. M-o-M delta on top categories does 80% of the job. |
| **Subscription/recurring tracking ("Netflix charged you")** | Lunch Money, Copilot, Monarch all have this. | Genuinely useful but: (a) requires recurrence detection logic, (b) crosses into "alert me when one stops" planning territory, (c) the user can spot subscriptions in their own merchants list once. | Defer to v1.5; meanwhile, the Top-Merchants view + tags solves the "what subscriptions do I have" question manually. |
| **Reports library (heat maps, sankey, treemap, time-of-day)** | Monarch markets a "comprehensive visualization toolkit." | Each chart is a maintenance load and most don't earn their place. Pick the 3 that actually answer questions and skip the rest. | Ship: month dashboard, calendar heatmap, top-merchants. That's enough. The full Monarch chart library is a rabbit hole. |
| **Import from "any bank" via Plaid/Salt Edge/aggregator** | Universal in SaaS. | (a) Defeats the whole self-hosted privacy promise — these aggregators *are* the third-party data exposure the user is avoiding. (b) PROJECT.md scopes v1 to Mono. | Importer interface is extensible; non-Mono sources land in v2 only after Mono path is rock solid, and only via direct bank APIs / CSV — never aggregators. |
| **Categorization from receipts / OCR** | Some apps do receipt photo capture. | Mono provides `receiptId` for free; surfacing the existing receipt is sufficient. Building OCR is a different product. | Surface Mono's `receiptId` as a link. Done. |
| **In-app currency conversion at "current" rate** | Tempting "live FX" feature. | Conflates txn-day rate (correct for historical reconciliation) with today's rate (irrelevant for a spending view). Creates ambiguity in dashboards. | Use NBU txn-day rate only. Surface the rate. Resist any "live" framing. |

---

## Mono API Quirks That Shape Feature Design

These are concrete API behaviors that drive specific feature decisions. Confirmed against [api.monobank.ua/docs](https://api.monobank.ua/docs/index.html) and the [go-monobank type definitions](https://pkg.go.dev/github.com/vtopc/go-monobank).

| Quirk | Source | Feature implication |
|-------|--------|---------------------|
| **Statement window: max 31 days + 1 hour** (2,682,000s) | Mono docs; Python client | Backfill must paginate by ≤ 31-day windows, walking backward. UI: "backfilling Feb 8–Mar 10... 3 of 12 windows." |
| **Rate limit: 1 request / 60s / token** | Mono docs; widely confirmed | Every API call competes for the same token-scoped slot. Polling, backfill, and on-demand refresh must share one queue. UI must never trigger an API call inline. |
| **Statement returns up to 500 items per call**; if more, you must narrow the window | Mono docs | If a 31-day window has >500 items (high-volume FOP, busy month), backfill must detect "results == 500 → split window in half and retry." |
| **`statementItem.id` is stable per-account, opaque string** | Mono docs; cross-language client confirmation | Use directly as the natural unique key for idempotency. Don't compute hashes; the Mono ID is enough for Mono-sourced rows. |
| **Only `time` (Unix seconds) — no separate `operationDate`** | go-monobank types; Mono docs | The "transaction time" and "settlement time" are not separately exposed. The `hold` flag is the only pending/cleared signal. Treat `time` as the canonical transaction timestamp; for FX rollup, use `time`'s calendar date in Kyiv timezone (Europe/Kiev). |
| **`hold: bool` is the only pending state** | go-monobank types | A pending hold can change amount before clearing. Either (a) ingest with flag, exclude from totals; (b) skip until cleared. Recommended (a). When a held item later clears with a different `amount`, the same `id` arrives again — must update in place. |
| **`amount` is in account-currency minor units; `operationAmount` is in transaction-currency minor units; `currencyCode` is ISO 4217 numeric** | go-monobank types | Multi-currency model must store all three. Never store as float; always int64 minor units. UAH=980, USD=840, EUR=978 (the values you'll see in `currencyCode`). |
| **`mcc` and `originalMcc` are both present**; `originalMcc` is the merchant's intended code, `mcc` is what Mono normalized to | go-monobank types | Rules engine should let users match on either. Default to `mcc` for categorization; expose `originalMcc` for power users. |
| **Jars are first-class accounts with their own `/personal/statement/{jarId}`** | Mono docs; client-info structure | Treat jars as accounts in storage; transactions in/out of a jar are real transactions and double up with the source-account transactions when transferring (this is a core internal-transfer signal). |
| **FOP accounts add `counterEdrpou` (sole-proprietor tax ID) and `counterIban`** | go-monobank types | Rules engine should support matching on `counterEdrpou` and `counterIban`. Useful for "always categorize incoming counterparty 12345678 as 'Salary'." Also: FOP-personal transfers are internal transfers and must be detected. |
| **`comment` is user-supplied text on the transaction** | go-monobank types | Surface in UI. Make it searchable. Include in rule-condition matching. |
| **`receiptId` exists for many (not all) withdrawals** | go-monobank types; webhook docs | Link out to Mono receipt page when present. Don't build OCR. |
| **Webhooks need a public HTTPS endpoint** | Mono docs | Out of scope per PROJECT.md. Document this in setup so future-self doesn't accidentally enable it. |
| **MCC catalogue is ISO 18245**; community UA-localized dataset exists | [Oleksios/MCC dataset](https://github.com/Oleksios/Merchant-Category-Codes) | Ship default category mappings against MCC groups (4xxx Transport, 5411 Grocery, 5812/5814 Food, etc.). Use the UA-language MCC dataset for friendly labels. |

---

## Prior-Art Gotchas

What earlier Monobank/personal-finance importers got wrong, with sources. These map directly into "do this differently."

| Gotcha (project) | Symptom | What this project should do |
|------------------|---------|------------------------------|
| **Backfill creates duplicates** ([ynab-bank-importer #36](https://github.com/gitviola/ynab-bank-importer/issues/36), [Actual #2519](https://github.com/actualbudget/actual/issues/2519)) | Re-running an importer over an overlapping window creates two rows. Re-importing after a deletion creates the row again. | Use Mono `statementItem.id` as the dedup key; for manual deletions, mark as `deleted` not actually delete, so re-imports don't resurrect them. ([Actual "Reimport deleted transactions" toggle](https://actualbudget.org/docs/transactions/importing/) is the precedent.) |
| **Internal transfers double-counted** (every importer that doesn't try) | A 1000 UAH jar deposit = -1000 outflow on card + +1000 inflow on jar. "This month spending" = 0. | Pair both legs at ingest. PROJECT.md acknowledges this. Use opposite-amount + close-time + Mono description heuristics. Allow manual confirm/reject for low-confidence pairs. ([monobudget transfer detection](https://github.com/smaugfm/monobudget)) |
| **Multi-currency transfers create "ghost" gain/loss** ([Firefly III #11329](https://github.com/firefly-iii/firefly-iii/issues/11329)) | When transferring 100 USD to UAH, the two legs have different `amount` values (100 USD vs 4200 UAH). Naive matchers reject the pair. | Match on `operationAmount + operationCurrency` for the cross-leg, not just `amount`. Mono's `operationAmount` is what makes this tractable. |
| **Held transactions show as final** | A pending hold of 1500 UAH that clears at 1450 UAH leaves the dashboard with 1500 forever (or with a duplicate). | Treat `hold: true` items as ingestible-with-flag. When the same `id` returns with `hold: false`, update in place. Never insert two rows for the same `id`. |
| **Token leaks into logs** | Importers that just `print(response.text)` on errors will leak the token-bearing request URL or header. | Log middleware that scrubs `X-Token: ...` and any 64-char-hex-looking substrings. Default-on. |
| **Rules don't run on backfilled data** ([Actual #3702](https://github.com/actualbudget/actual/issues/3702)) | User adds a rule expecting it to clean up history; only future txns get categorized. | "Apply to existing transactions?" prompt on rule creation. Idempotent. Show diff before commit. ([Lunch Money does this](https://support.lunchmoney.app/setup/rules)) |
| **Merged transfers come back unmerged on next sync** ([Actual #6239](https://github.com/actualbudget/actual/issues/6239)) | User pairs two transactions; importer sees them as new again next poll. | Pairing must be persisted on the *source* row by Mono ID, not on a derived "merged transaction" object the importer can re-create. |
| **Rules with regex don't apply to imported transactions** ([Actual #3235](https://github.com/actualbudget/actual/issues/3235)) | Regex rules silently skip the import path. | Treat rules engine as an integral part of ingest, not a post-hoc cleanup. Same engine, same execution path, for new + backfill + manual. |
| **FOP transactions routed to personal account** ([OpenCart Mono integration](https://opencartforum.com/en/files/file/9243-vipiska-z-rahunku-fop-v-privatbanku-ta-monobanku-dlya-opencart/)) | Some integrations couldn't separate FOP from personal — items landed on the wrong account. | Storage keys account-id and token-id together. Never collapse two Mono `Account` objects into one based on user identity. |
| **monobudget: webhook-only, requires reverse proxy** ([smaugfm/monobudget README caveats](https://github.com/smaugfm/monobudget)) | Hard to run on a homelab without exposing the box. | Polling-first design, no public endpoint required. Webhook is a deliberate non-feature in v1. |
| **Maybe Finance: project unmaintained** ([maybe-finance/maybe](https://github.com/maybe-finance/maybe)) | A self-hosted tool needs to be maintainable by one person. Maybe got too ambitious (investments, AI chat) and stalled. | Stay narrow. Visibility-only. Mono-only. The scope ceiling *is* the maintainability story. |

---

## Rules Engine Shape (Concrete Recommendation)

Rules are central to the categorization promise; this section gets specific so REQUIREMENTS.md can lift it directly.

**Rule structure:**

```
Rule {
  id: string
  enabled: bool
  priority: int  (lower = earlier, ties broken by id)
  conditions: [Condition]   # ALL must match (AND)
  actions: [Action]
}

Condition (one of):
  - merchant_substring (case-insensitive, on description)
  - merchant_regex
  - mcc_in (list of int)
  - mcc_group (e.g. "5400-5499" Grocery, "5800-5899" Food)
  - amount_sign (negative=expense, positive=income)
  - amount_range (min, max in minor units, account ccy)
  - account_id (specific account or jar)
  - currency_code (840=USD, 978=EUR, 980=UAH)
  - counterparty_iban_substring
  - counterparty_edrpou (exact)
  - comment_contains (case-insensitive on user comment)
  - hold_status (true / false / either)

Action (any combination):
  - set_category
  - set_payee (clean up "MERCHANT*1234567 KYIV UA" → "Merchant Name")
  - add_tag
  - mark_as_transfer  # for cases the auto-detector misses
  - mark_as_refund_of (paired transaction id)
  - exclude_from_dashboard  (e.g. internal transfer artifacts)
```

**Execution order:** rules sorted by `priority` ascending, first-match-wins on category (subsequent rules can still add tags). Borrowed from Tiller's "more specific rules at top" model and Actual's "least to most specific" auto-ranking — but explicit user control over priority, not auto-rank, because automatic rule ranking is the source of half the [Actual #3702](https://github.com/actualbudget/actual/issues/3702)-class bugs.

**Apply scope:** by default applies to (a) new ingest, (b) backfill, (c) re-imports. On rule create/edit, prompt "apply to existing transactions?" with a count-affected diff before commit.

---

## Feature Dependencies

```
[Mono ingestion (poll)]
    └──requires──> [Token entry & rotation]
    └──requires──> [Rate-limit-aware queue]
    └──feeds────> [Persist source payload]
                       └──feeds──> [Idempotent dedup (by Mono id)]
                                       └──feeds──> [Internal-transfer detection]
                                       └──feeds──> [Refund pairing]
                                       └──feeds──> [Rules engine]

[Rules engine]
    └──requires──> [Default categories]
    └──requires──> [MCC catalogue]
    └──enhances──> [Auto-rule suggestion]
    └──enhances──> [Run-rules-on-history]

[Multi-currency model]
    └──requires──> [NBU rate fetch]
    └──feeds────> [UAH rollup dashboard]

[Manual entry / edit / split / merge]
    └──requires──> [Persist source payload]  (so manual ops never mutate raw)

[Spending dashboard]
    └──requires──> [Internal-transfer detection]  (else numbers are wrong)
    └──requires──> [Refund pairing]              (else numbers are wrong)
    └──requires──> [Multi-currency model]
    └──enhances──> [Calendar heatmap]
    └──enhances──> [Top-merchants view]

[Transaction feed]
    └──requires──> [Search + filter index]
    └──enhances──> [Quick re-categorize]
    └──enhances──> [Detail drawer with raw payload]

[Backup/restore]  ──orthogonal to──>  [Full data export]
[Logs redaction] ──orthogonal to──>  everything
```

### Dependency Notes

- **Internal-transfer detection requires source payload + dedup**: detection runs over normalized rows; without dedup, the same transfer leg appears twice and breaks pairing.
- **Refund pairing depends on having the original transaction in the DB**: if the original was on a date before earliest backfill, pairing will silently fail. UX must handle "no original found" gracefully (don't crash, don't auto-create a phantom).
- **Auto-rule suggestion depends on observed user overrides**: ship rules engine first; layer suggestion on top once there are 50+ manual recategorizations to learn from.
- **Multi-currency rollup depends on NBU rate availability**: NBU API can be down. Cache rates locally; fall back to last-known rate with a "(rate from N days ago)" annotation. Never block ingest waiting on rates.
- **Calendar heatmap depends on internal-transfer detection**: a day with 5000 UAH "spending" that's actually a jar transfer should be a cool-colored day, not a spike. Heatmap on noisy data is worse than no heatmap.

---

## MVP Definition

### Launch With (v1) — Table Stakes Only

The minimum that makes this a usable spending-visibility tool. Everything below has to ship together; a partial v1 is not usable.

- [ ] Mono polling ingestion with rate-limit-aware queue, backfill in 31-day windows
- [ ] Per-account ingestion across cards, jars, FOP (with `counterEdrpou` preserved)
- [ ] Persist normalized rows + `raw_payload` JSON
- [ ] Idempotency by `statementItem.id`; soft-delete model (don't resurrect on re-import)
- [ ] Internal-transfer detection (account ↔ jar, account ↔ account, FOP ↔ personal)
- [ ] Refund/reversal pairing with manual confirm for low-confidence
- [ ] Multi-currency: UAH/USD/EUR distinct + UAH rollup at NBU txn-day rate, with rate visible
- [ ] Rules engine with the concrete shape above (merchant + MCC + amount + counterparty)
- [ ] Default category taxonomy (~15 categories) mapped to MCC groups
- [ ] Manual edit / merge / split / re-categorize
- [ ] Manual cash-transaction entry
- [ ] "This month" dashboard: total + top categories + vs prior month
- [ ] Transaction feed: filter, search, sort, quick re-categorize, detail drawer
- [ ] Hold/pending handling (ingest with flag, exclude from totals, update-in-place on clear)
- [ ] CSV import as fallback
- [ ] CSV + JSON export
- [ ] DB backup/restore documented
- [ ] Polling status visibility (last poll, last error, 401/429 surfaced)
- [ ] Token entry, validation, rotation, encrypted at rest
- [ ] Network egress documented (mono + nbu only)
- [ ] Log redaction on by default
- [ ] Responsive web UI working on mobile browser
- [ ] Docker single-compose deploy

### Add After Validation (v1.x)

Layer on once v1 has lived for a couple months and the long tail of categorization is observable.

- [ ] Auto-rule suggestion from manual recategorizations (~50+ overrides observed)
- [ ] Run-rules-on-history with diff preview (cheap; can land in v1 if time permits)
- [ ] Calendar heatmap view
- [ ] Top-merchants view
- [ ] Tags as orthogonal axis
- [ ] Receipt link-out via `receiptId`
- [ ] Multi-token UI polish (add second token, e.g. FOP, after personal is working)
- [ ] Smart "this looks like a transfer" prompt for unpaired-but-suspicious candidates

### Future Consideration (v2+)

Defer until v1 is validated. Each of these is a defensible product decision *not* to build right now.

- [ ] LLM categorizer (local Ollama or API) — wait until rule long tail is observed
- [ ] Subscription/recurring detection — defer; tags + top-merchants cover the manual case
- [ ] Additional importers (PrivatBank, Wise, Revolut, IBKR) — only after Mono path is rock solid
- [ ] Webhook ingestion — only if hosting model changes to allow public endpoints
- [ ] PWA — only if mobile-browser is proven insufficient
- [ ] Alerts/notifications — only if a clear value-vs-complexity case emerges
- [ ] Investment/net-worth tracking — different product
- [ ] Budgets/forecasts/goals — explicitly out per Core Value; revisit only if user asks repeatedly
- [ ] Multi-user — explicitly out per PROJECT.md
- [ ] Cloud sync — defeats trust model

---

## Feature Prioritization Matrix

Top features ranked by user-value × cost. P1 = must-have for v1; P2 = should land in v1 if cheap, otherwise v1.x; P3 = future.

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Polling ingestion + backfill (rate-limit-aware) | HIGH | MEDIUM | P1 |
| Idempotent dedup by Mono id | HIGH | LOW | P1 |
| Internal-transfer detection | HIGH | MEDIUM | P1 |
| Refund/reversal pairing | HIGH | MEDIUM | P1 |
| Multi-currency model + UAH rollup | HIGH | MEDIUM | P1 |
| Rules engine (merchant + MCC + amount + counterparty) | HIGH | MEDIUM | P1 |
| Manual edit/merge/split/recategorize | HIGH | MEDIUM | P1 |
| "This month" dashboard | HIGH | MEDIUM | P1 |
| Transaction feed with filter/search/quick recat | HIGH | MEDIUM | P1 |
| Hold/pending handling | MEDIUM | LOW | P1 |
| CSV import fallback | MEDIUM | LOW | P1 |
| CSV + JSON export | MEDIUM | LOW | P1 |
| Polling status visibility | MEDIUM | LOW | P1 |
| Token rotation + encrypted at rest | MEDIUM | LOW | P1 |
| Log redaction default | MEDIUM | LOW | P1 |
| Manual cash entry | MEDIUM | LOW | P1 |
| Default category taxonomy | MEDIUM | LOW | P1 |
| Multi-token / multi-card | MEDIUM | MEDIUM | P1 (Mono FOP needs it) |
| Run-rules-on-history (retroactive) | MEDIUM | LOW | P1 if time, else P2 |
| Receipt link-out via receiptId | LOW | LOW | P2 |
| Calendar heatmap | MEDIUM | LOW | P2 |
| Top-merchants view | MEDIUM | LOW | P2 |
| Tags as orthogonal axis | MEDIUM | LOW | P2 |
| Auto-rule suggestion from edits | HIGH | MEDIUM | P2 (needs data first) |
| LLM categorizer | MEDIUM | HIGH | P3 |
| Subscription/recurring detection | LOW | MEDIUM | P3 |
| Other importers (Privat, Wise, etc.) | MEDIUM | HIGH | P3 |
| Investments/net-worth | LOW | HIGH | P3 (different product) |
| Budgets/forecasts/goals | LOW | HIGH | NEVER (anti-feature) |

**Priority key:**
- P1: Must have for v1
- P2: Should have, add when possible (v1 if cheap, else v1.x)
- P3: Future consideration
- NEVER: Anti-feature; documented as a non-goal

---

## Competitor Feature Analysis

How the major comparables handle the highest-value features. "Our approach" column = the design choice for finance-bro.

| Feature | Actual Budget | Firefly III | Lunch Money | Tiller / Monarch | Our Approach |
|---------|---------------|-------------|-------------|------------------|--------------|
| **Auto-categorization** | Auto-creates rules from user behavior; rules ranked least→most specific | Rule engine with triggers/actions; no native MCC | ML-learned + explicit rules; learns from manual edits | Tiller AutoCat = manual rules top-down; Monarch = ML | Manual rules engine in v1; suggest-rule-from-edits in v1.x; LLM in v2. Rules USE MCC explicitly (the differentiator). |
| **Rules apply retroactively** | "Run rules" button; some bugs ([#3702](https://github.com/actualbudget/actual/issues/3702)) | Yes, on demand | Yes, with diff preview before apply | AutoCat re-runs over sheet | Yes, with diff preview. Lunch Money's UX is the model. |
| **Internal transfer detection** | Manual or "create transfer" UI; merge bugs ([#6239](https://github.com/actualbudget/actual/issues/6239)) | Importer auto-merges A→B/B←A pairs; depends on data quality | Manual marking; no strong auto-detection | Tiller manual; Monarch ML | Auto-detect at ingest using time + opposite amount + Mono description signals; cross-token; manual confirm for low-confidence |
| **Multi-currency** | NOT supported natively (POC PR open since 2024) | Supported | Supported | PocketSmith yes; others limited | First-class. UAH/USD/EUR distinct + NBU txn-day rollup. Rate always visible. |
| **Duplicate detection** | Hash + import id; reimport-deleted toggle | Content hash + identifier | Dedup tool with 2+ criteria | Tiller content-based; Monarch via aggregator | Mono `statementItem.id` (primary); content hash for manual/CSV (secondary). Soft-delete to prevent resurrection. |
| **Hold/pending state** | Cleared/uncleared flag (manual) | Cleared/reconciled | Cleared/uncleared | Same | Single `hold: bool` from Mono; flag in UI; exclude from totals; update-in-place on clear. |
| **Split transactions** | Yes | Yes (must keep src+dst on splits) | Yes; "unsplit" reversible | Yes | Yes; preserve sum-of-parts invariant; reversible. Lunch Money's unsplit is the model. |
| **Calendar / heatmap** | No | No | Calendar view added 2024 | Monarch heatmap | Yes (P2 — cheap once dashboard exists). |
| **Tags vs categories** | Categories only; no tags | Tags via journal-meta | Tags + categories | Tiller labels; Monarch tags | Categories (1:1) + tags (n:m). Both. |
| **Data export** | Zip backup; CSV via API | CSV (limited round-trip) | API + CSV | CSV; Sheets-native | CSV + JSON (with raw_payload). Zip restore. |
| **Self-hosted Docker** | Yes | Yes | No (SaaS) | No (SaaS) | Yes; single compose file. |
| **Auth** | Optional password | App auth | SaaS auth | SaaS auth | None. Network-gated. |
| **AI/LLM categorization** | Community projects (e.g., actual-ai) | Community projects (e.g., firefly-iii-ai-categorize) | Built in (assistive) | Monarch yes; Copilot is ML-first | Deferred to v1.5+. Pluggable interface in v1. |
| **Budgets** | Zero-sum envelopes (THE feature) | Available | Yes | Yes | NO (anti-feature). |

---

## Sources

### Self-hosted comparables
- [Actual Budget — Rules engine](https://actualbudget.org/docs/budgeting/rules/)
- [Actual Budget — Tips & Tricks (keyboard shortcuts)](https://actualbudget.org/docs/getting-started/tips-tricks/)
- [Actual Budget — Importing transactions](https://actualbudget.org/docs/transactions/importing/)
- [Actual Budget — Backups](https://actualbudget.org/docs/backup-restore/backup/)
- [Actual Budget — Multi-currency feature request #2147](https://github.com/actualbudget/actual/issues/2147)
- [Actual Budget — Multi-currency PR #3658](https://github.com/actualbudget/actual/pull/3658)
- [Actual Budget — Duplicate transactions #2519](https://github.com/actualbudget/actual/issues/2519)
- [Actual Budget — Rule "apply actions" not working #3702](https://github.com/actualbudget/actual/issues/3702)
- [Actual Budget — Regex rules not applying #3235](https://github.com/actualbudget/actual/issues/3235)
- [Actual Budget — Merged transfers not staying merged #6239](https://github.com/actualbudget/actual/issues/6239)
- [Firefly III — Rule actions](https://docs.firefly-iii.org/references/firefly-iii/rule-actions/)
- [Firefly III — Importing transfers](https://docs.firefly-iii.org/how-to/data-importer/import/transfers/)
- [Firefly III — Duplicate detection](https://docs.firefly-iii.org/references/data-importer/duplicate-detection/)
- [Firefly III — Reconciliation](https://docs.firefly-iii.org/how-to/firefly-iii/finances/reconcile/)
- [Firefly III — Cross-currency transfer issue #11329](https://github.com/firefly-iii/firefly-iii/issues/11329)
- [Firefly III — Export tutorial](https://docs.firefly-iii.org/tutorials/firefly-iii/exporting-data/)
- [Maybe Finance (unmaintained)](https://github.com/maybe-finance/maybe)

### SaaS comparables (feature gravity)
- [Lunch Money — Rules engine](https://lunchmoney.app/features/rules)
- [Lunch Money — Auto-categorization](https://support.lunchmoney.app/setup/categories/auto-categorization)
- [Lunch Money — Other features (split/dedup)](https://support.lunchmoney.app/finances/transactions/other-features)
- [Lunch Money — Stats & Trends](https://lunchmoney.app/features/stats-trends/)
- [Lunch Money — Transaction utilities](https://lunchmoney.app/features/transactions)
- [Tiller — AutoCat](https://tiller.com/courses/getting-started-with-tiller/lessons/getting-started-with-tiller-part-3-of-8-customize-your-categories-and-keep-organized-with-autocat/)
- [Tiller — Splitting transactions](https://help.tiller.com/en/articles/581912-splitting-transactions-between-multiple-categories)
- [PocketSmith — Filters & bulk actions](https://www.pocketsmith.com/blog/transaction-filters-bulk-actions-and-personal-summary-averages/)
- [PocketSmith — Multi-currency](https://learn.pocketsmith.com/article/122-multi-currency-beta-features-in-pocketsmith)
- [Copilot — Intelligence for spending (ML)](https://help.copilot.money/en/articles/8182433-copilot-intelligence-for-spending)
- [Monarch vs YNAB comparison](https://www.monarch.com/compare/ynab-alternative)

### Monobank API
- [Monobank Open API docs](https://api.monobank.ua/docs/index.html)
- [go-monobank package (struct definitions)](https://pkg.go.dev/github.com/vtopc/go-monobank)
- [python-monobank (rate-limit handling)](https://github.com/vitalik/python-monobank)
- [siomochkin/monobank-open-api-documentation](https://github.com/siomochkin/monobank-open-api-documentation)
- [vergilet/monobank Ruby (API field reference)](https://vergilet.github.io/monobank/)

### Mono-specific tooling and prior art
- [smaugfm/monobudget](https://github.com/smaugfm/monobudget)
- [GitHub topics: monobank](https://github.com/topics/monobank)
- [uabean — beancount importers for UA banks](https://github.com/topics/monobank)
- [Oleksios/Merchant-Category-Codes (UA-localized MCC)](https://github.com/Oleksios/Merchant-Category-Codes)
- [FOP integration challenges (OpenCart forum)](https://opencartforum.com/en/files/file/9243-vipiska-z-rahunku-fop-v-privatbanku-ta-monobanku-dlya-opencart/)

### Importer-pattern prior art
- [ynab-bank-importer — N26 duplicate transactions #36](https://github.com/gitviola/ynab-bank-importer/issues/36)
- [beancount-import — How does deduplication work? #20](https://github.com/jbms/beancount-import/issues/20)

---

*Feature research for: self-hosted personal-finance tool (Monobank-only v1, single-user, visibility-focused)*
*Researched: 2026-05-10*
