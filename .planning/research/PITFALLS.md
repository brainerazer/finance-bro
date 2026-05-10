# Pitfalls Research

**Domain:** Self-hosted personal-finance importer/dashboard built on Monobank's personal API. Multi-currency (UAH/USD/EUR). Single-user. Python backend + JS frontend. Docker on homelab. Polling-only.
**Researched:** 2026-05-10
**Confidence:** HIGH for Mono API behavior and SQLite/Docker/money-representation pitfalls (multiple corroborating sources). MEDIUM for FX/NBU weekend behavior and reconciliation specifics (some claims are anecdotal — flagged inline). LOW for "what users complain about in Lunch Money/Maybe" — those communities are private/unsearchable; flagged inline.

> **Bias of this document:** I deliberately omitted generic SE advice ("write tests", "version control your code"). Every entry below is something this exact project shape will hit. Where a pitfall is anecdotal I label it `[anecdotal]` rather than padding with false certainty.

---

## Critical Pitfalls

### Pitfall 1: Floats for money

**What goes wrong:**
Storing UAH/USD/EUR amounts as Python `float`. `2.32 * 3` returns `6.959999999999999`; thousands of summed transactions produce category totals that drift by hundreds of kopecks. Equality comparisons silently fail (`if total == 100.00` is never true). Sums computed twice from the same data return different values depending on transaction order.

**Why it happens:**
JSON-decoded numbers in Python are `float` by default. SQLAlchemy's `Float` column maps to `DOUBLE PRECISION`. ORMs and JSON libraries don't push back — the bug ships, then surfaces months later as "category totals don't add up to my bank balance."

**How to avoid:**
- Store amounts in **minor units (kopecks/cents) as `BIGINT`** in the database. Mono already returns `amount` and `operationAmount` in kopecks — keep them that way end-to-end. Convert to `Decimal` only for display/aggregation.
- In Python, use `decimal.Decimal` and configure JSON deserialization with `parse_float=Decimal`. Never use `float` for money.
- In JS frontend, use `BigInt` or a library like `dinero.js` or `currency.js`. Do not use `Number` for amounts.
- Set `getcontext().prec = 28` and `ROUND_HALF_EVEN` (banker's rounding) for any aggregation step.

**Warning signs:**
- Any column type in the schema named `Float`, `Real`, `Double`, or `Numeric` without a `scale`. (Postgres `NUMERIC` without scale is variable-precision but slow; `NUMERIC(20, 4)` is fine.)
- A test that does `assert total == expected` against a float and passes only because the test data is small.
- Sum of category totals != sum of all transactions (off by < 1 kopeck per transaction).

**Phase to address:** **Early — data model phase, before any importer runs.** Schema decisions here are forward-only painful to fix.

**Sources:**
- [Still Using Python float for Money? Here's Why That's Dangerous (Medium)](https://medium.com/the-pythonworld/still-using-python-float-for-money-heres-why-that-s-dangerous-c761b994c526)
- [How I Lost $10,000 Because of a Python Float (Medium)](https://medium.com/@pranaysuyash/how-i-lost-10-000-because-of-a-python-float-and-how-you-can-avoid-my-mistake-3bd2e5b4094d)
- [Python decimal docs — `decimal` module](https://docs.python.org/3/library/decimal.html)

---

### Pitfall 2: Off-by-100 on Mono's minor-unit amounts

**What goes wrong:**
Mono returns `amount` and `operationAmount` already in **kopecks (minor units)**. Display code does `amount` instead of `amount / 100` — every transaction shows up 100x too large. Or the inverse: `int(input * 100)` is applied twice somewhere in the pipeline, and a 50-UAH coffee becomes 0.005 UAH.

**Why it happens:**
Most banking APIs return major units. Mono is the exception in this stack. Engineers from a Stripe/Plaid/CSV-import background assume major units; engineers used to Stripe assume minor units but forget that the FX provider (NBU) returns major units (e.g. `28.34` UAH per USD).

**How to avoid:**
- Adopt one rule, write it on the schema: **DB stores minor units (kopecks) as `BIGINT`. NBU FX rates are stored as `DECIMAL(20, 8)` in major units.** Document at the column level.
- Wrap conversion in two named functions used everywhere: `kopecks_to_decimal(n)` and `decimal_to_kopecks(d)`. Ban raw `/100` and `*100` in the codebase via grep / lint rule.
- Round-trip test: import a fixture, render dashboard, assert displayed total equals the bank app's displayed total to the kopeck.

**Warning signs:**
- Numbers in the dashboard 100x too large or 100x too small.
- A test fixture using `100.00` for "100 UAH" — half the codebase expects 100 (kopecks = 1 UAH), half expects 10000 (kopecks = 100 UAH).

**Phase to address:** **Early — importer phase.** A test that round-trips one real transaction end-to-end catches this in minutes.

**Sources:**
- [Monobank API spec via vergilet/monobank Ruby client (documents `amount` / `operationAmount` in cents)](https://vergilet.github.io/monobank/)
- [python-monobank README — `amount` and `operationAmount` shape](https://github.com/vitalik/python-monobank/blob/master/README.md)

---

### Pitfall 3: Mono `id` is per-account, not globally unique — naive dedup eats real transactions

**What goes wrong:**
Two assumptions both bite:
1. **`id` collisions across accounts.** Mono's `StatementItem.id` is documented as a transaction identifier, but its **uniqueness scope is not officially documented as global**. Treating `id` as a global PK risks future collisions; treating it as the only dedup key on a re-import causes legitimate duplicates (e.g. two identical 50-UAH coffees on the same day from different cards) to be silently dropped if a hash-on-(date, amount, mcc, description) is also used.
2. **Hold→cleared transitions look like duplicates.** A pending transaction has `hold: true` and a tentative `amount`. When it clears, the bank may re-emit with `hold: false` and a possibly-different `amount` (restaurant tip, gas-pump pre-auth, FX settlement). Naive importers store both, then double-count.

**Why it happens:**
Mono docs are sparse on uniqueness guarantees. Firefly III's [issue #2358](https://github.com/firefly-iii/firefly-iii/issues/2358) is precisely this class of bug ("only credit duplicates are being detected"); the proposed fix is a content hash. But pure content-hash dedup throws away the legitimate-duplicate-coffees case.

**How to avoid:**
- **Composite primary key** for transactions: `(account_id, mono_id)`. Treat `mono_id` as scoped per account. This is the safe assumption regardless of Mono's actual guarantee.
- **Never auto-merge two rows with the same content.** When you see (date, amount, mcc, description) match on a re-import, only treat as duplicate if `mono_id` matches. Otherwise keep both.
- **Hold semantics:** store the source payload verbatim (a `raw_jsonb` column) for every transaction, including holds. When a non-hold version with the same `id` arrives, **update in place** rather than insert a second row. If the cleared `amount` differs from the hold `amount`, that's expected — record the delta in an audit field, don't create a second row.
- For backfill overlap, use `INSERT ... ON CONFLICT (account_id, mono_id) DO UPDATE` (Postgres) so re-imports are idempotent.

**Warning signs:**
- Total spending shows a small but persistent drift from the bank's app.
- A `transactions` count that grows on every poll cycle because hold→cleared re-emit counts as new.
- A unique constraint defined as just `(mono_id)` rather than `(account_id, mono_id)`.
- Two identical coffees on the same day showing as one in the dashboard.

**Phase to address:** **Early — schema and importer phase.** This is the central correctness invariant. Get it wrong and every metric downstream is a lie.

**Sources:**
- [Firefly III #2358 — Import duplicate transaction failed to be detected](https://github.com/firefly-iii/firefly-iii/issues/2358)
- [Firefly III duplicate detection discussion #10579](https://github.com/orgs/firefly-iii/discussions/10579)
- [smaugfm/monobudget — uses Mono `id` for dedup against YNAB/Lunchmoney](https://github.com/smaugfm/monobudget)
- [What Is a Pending Transaction (Capital One)](https://www.capitalone.com/learn-grow/money-management/pending-transactions/) — the hold-amount-can-change behavior is industry-standard, not Mono-specific.

---

### Pitfall 4: Mono's 1-req/60s rate limit and naive retry loops

**What goes wrong:**
The Personal API limit is **1 request per 60 seconds per token**, hard. Common failure modes:
1. Loop with `try / except / sleep(1) / retry` from copy-pasted examples — burns through retries, gets the token throttled or banned. (`python-monobank` README's example is `time.sleep(1)` then retry, which is wrong if the limit is 60s, not 1s.)
2. A backfill script that fires `get_statement` for 12 months in parallel — first request succeeds, next 11 fail, scheduler thrashes.
3. Polling `client_info` (account list) on every poll cycle even though it changes monthly.
4. A scheduler that fires multiple jobs (statement-poll + jar-poll + balance-check) inside the same minute and races them.

**Why it happens:**
The README of one of the most-starred Python clients literally suggests `time.sleep(1)`. The 1-req/60s limit is per token, not per endpoint — most rate-limit libraries default to per-endpoint.

**How to avoid:**
- Single global token-bucket / leaky-bucket gate, **per token**, with a refill rate of 1 token / 60s. All API calls go through this gate. Implement once, in one module. Use `aiolimiter` or `pyrate-limiter` or hand-roll with `asyncio.Lock` + a timestamp.
- On `429`, respect the `Retry-After` header if present; otherwise back off **at least 60s**, not 1s.
- Sequential, not parallel: a backfill of 12 months × 31-day windows = 12 calls = 12 minutes. Plan for it. Run it as a one-shot job, not on every poll.
- Cache `client_info` for at least 1 hour. Account list changes rarely; jar metadata changes never on a normal day.
- The poll cadence should be **strictly slower than 60s** (e.g. 90s) to give margin against clock drift between scheduler and Mono's gate.

**Warning signs:**
- Any test that calls `get_statement` more than once in a unit-test run without mocking.
- Logs full of 429s clustered in time (signals parallel calls).
- Mono support emails / token deactivation. (`[anecdotal]` — Mono does reserve the right to revoke tokens for misuse per their API terms.)

**Phase to address:** **Early — importer phase.** The rate limiter should be the first thing built in the Mono module, before any business logic.

**Sources:**
- [python-monobank README — note the suggested `time.sleep(1)` retry, which is too short](https://github.com/vitalik/python-monobank/blob/master/README.md)
- [Monobank API docs (general)](https://api.monobank.ua/docs/index.html)
- [Monobank Corporate API docs — explicitly no rate limit; Personal has 1/60s](https://api.monobank.ua/docs/corporate.html)

---

### Pitfall 5: Statement endpoint 31-day window — backfill bugs

**What goes wrong:**
The `/personal/statement` endpoint accepts `from`/`to` Unix timestamps but rejects ranges longer than **31 days + 1 hour (2,682,000 seconds)**. Common bugs:
1. Backfill code passes a 1-year range, gets an empty body or 400, treats it as "no transactions exist", skips the year silently.
2. Iteration uses 30-day chunks and hits boundary-overlap dedup edge cases (last txn of one window = first txn of the next, and the dedup logic can't tell).
3. Time arithmetic in seconds vs milliseconds — passing `time.time()` works (seconds); passing `int(time.time() * 1000)` makes Mono interpret your "2026" as the year 4970.
4. Backfill is run before the rate limiter is in place; first call succeeds, rest 429.

**Why it happens:**
The constant 2,682,000 seconds doesn't appear in most third-party client READMEs as a callout. The empty/error response on out-of-range is not loud.

**How to avoid:**
- Constant-name it: `MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000` (31d + 1h).
- Backfill helper iterates from `now` backwards in **30-day chunks** (not 31 — leave headroom for the +1h slop), waits 90s between calls, dedups on `(account_id, mono_id)` so window overlap is harmless.
- Treat empty result as "this window had no transactions" only after asserting the window is within max bounds; otherwise fail loud.
- All time math in seconds, sourced from `int(datetime.now(tz=UTC).timestamp())`. Never multiply by 1000.

**Warning signs:**
- Backfill completes "successfully" but stops at exactly 31 days back from today.
- Logs show a 4xx response treated as "no transactions" without a warning.
- A unit test that fakes a 1-year window and never tests the 31-day clamp.

**Phase to address:** **Early — importer phase, after rate limiter.** Build the chunked backfill helper as part of the first vertical slice.

**Sources:**
- [Monobank API spec via vergilet/monobank — 2,682,000s and 1-per-60s constants](https://vergilet.github.io/monobank/)
- [siomochkin's Monobank Open API documentation summary](https://github.com/siomochkin/monobank-open-api-documentation)

---

### Pitfall 6: `time` field semantics — there is no `operationDate`

**What goes wrong:**
This is a **terminology trap, not a Mono one**. Mono returns a single `time` field (Unix seconds), which is the **bank's recording time** (closer to "posting time" than "transaction time"). It is **not** what a user thinks of as "when I bought the coffee" in cases like:
- Late-night purchase where the bank books it the next morning → spending attributed to wrong calendar day → wrong calendar month.
- FX settlement on a foreign-currency card: the merchant charges Tuesday in EUR; Mono finalizes the UAH-equivalent Thursday; `time` reflects Thursday.
- Refund that comes in two weeks later — `time` reflects the refund date, not the original purchase date — naive matching breaks.

There is no separate `operationDate` field in Mono's response despite what the question's framing implies.

**Why it happens:**
The question itself reflects a common mental model from APIs that distinguish "transaction date" vs "posting date". Mono only exposes one. Devs assume `time` is what they want and don't notice the cross-month drift.

**How to avoid:**
- Surface this in the data model: store `mono_time` (the API's `time`), and a derived `attributed_day` (date in Europe/Kyiv). Document that `mono_time` may be later than the user-perceived purchase moment.
- For "this month" calculations, treat the **Europe/Kyiv calendar day of `mono_time`** as the spending day. Accept that ~1% of transactions will straddle a month boundary and be attributed to the "wrong" month from a strict purchase-date perspective. Allow manual override on the transaction.
- For refund matching, do NOT match by date proximity alone; match by amount + merchant + description token, allowing arbitrary date gap.

**Warning signs:**
- "This month" total in the app at 23:55 differs from the same total at 00:05 by more than the cost of one purchase.
- A purchase made at 23:50 local time appears in next month's bucket.
- `attributed_day` derived from a UTC timestamp instead of Europe/Kyiv (off by 2-3h in UA winter, can flip the date).

**Phase to address:** **Middle — month-attribution and dashboard phase.** Lock the timezone semantics before building "this month" widgets.

**Sources:**
- [python-monobank README — fields list (no `operationDate`)](https://github.com/vitalik/python-monobank/blob/master/README.md)
- [Mono API doc summary in vergilet/monobank Ruby client](https://vergilet.github.io/monobank/)
- [Generic transaction-date vs posting-date primer](https://financeband.com/what-is-the-difference-between-transaction-date-and-posting-date)

---

### Pitfall 7: NBU rates have weekend/holiday gaps — treating "no rate" as "zero"

**What goes wrong:**
NBU publishes daily official rates **only on banking business days**. Weekends, public holidays (1 Jan, Christmas, Independence Day, etc.), and recently-introduced unscheduled non-working days have **no published rate**. Common failures:
1. Querying `bank.gov.ua/NBUStatService/v1/statdirectory/exchange?date=20260104` (a Sunday) returns an empty array or a 200 with `[]`. Code treats empty as "rate is 0", divides → `inf` or 0-UAH amounts everywhere.
2. Code falls back to "today's rate" for historical transactions, blending a 2024 USD purchase with a 2026 rate.
3. Code uses NBU's rate for converting **a USD card purchase to UAH for display**, but Mono already exposes `currencyCode` + `operationAmount` (the actual UAH amount the bank settled) — using NBU on top double-converts, drifting from the user's real bank balance by 1-3%.

**Why it happens:**
NBU publishes a single daily snapshot. Their API doesn't return "yesterday's rate" automatically. Holiday calendar isn't programmatic — it changes year to year (and during wartime, can change unpredictably).

**How to avoid:**
- **Source of truth for FX is Mono, not NBU, for transactions Mono already converted.** When `currencyCode != account.currency`, Mono's `amount` is already in account currency. Don't reconvert.
- For UAH-rollup of USD/EUR balance held in a foreign-currency Mono account, use NBU as the rate source. **Cache rates indexed by date.** On a date with no NBU rate (weekend/holiday), fall back to the **most recent prior business-day rate**. Document this in the rollup view ("UAH equivalent at NBU rate of Friday 2026-05-08").
- Pre-fetch rates: on every poll, fetch any missing rates between the oldest unrated transaction and today. Cache forever — historical NBU rates don't change.
- Never use a future date for a past transaction.
- **NBU publishes the official mid-rate only.** There is no buy/sell. For personal-finance display purposes the mid-rate is correct; do not pretend you have spread data.

**Warning signs:**
- Sunday-dated transactions show UAH equivalent of `0` or `null` or `inf`.
- A transaction made in 2024 in USD shows a UAH equivalent that matches today's USD/UAH rate, not the historical rate.
- Bank-reported UAH balance differs from app-computed UAH equivalent of the same transactions by >1%.

**Phase to address:** **Middle — multi-currency rollup phase.** Build the cache + business-day-fallback abstraction once and pin the contract: "every transaction has a rate-on-or-before its day."

**Sources:**
- [NBU developer API directory](https://bank.gov.ua/en/open-data/api-dev)
- [floatrates.com mirror of NBU rates — explicitly weekday-only](https://www.floatrates.com/source/nbu/)
- [kastaneda/nbu_rates — historical archive showing the gaps](https://github.com/kastaneda/nbu_rates)
- [Double-conversion explainer (Payoneer)](https://www.payoneer.com/resources/what-does-double-conversion-mean/) — why re-converting Mono's already-converted UAH amount is wrong.

---

### Pitfall 8: Multi-hop FX ambiguity — EUR card, USD merchant, UAH issuer

**What goes wrong:**
Mono's currency model: every account has a `currencyCode` (the account currency). A transaction has `currencyCode` (the **operation** currency — what the merchant charged) and `operationAmount` (in operation currency). `amount` is always in account currency.

When the user holds an EUR card and pays at a USD merchant, the **bank** picks a rate. There can be up to three rates involved: EUR↔USD (bank's), USD↔UAH (NBU's, for tax/reporting), and the user's app cares about EUR↔UAH for the rollup.

Naive UAH rollup: `amount_uah = amount_eur * NBU_eur_uah_rate`. But Mono's `amount` is already EUR (account currency), and the EUR amount Mono settled with includes the bank's spread on the EUR↔USD leg. The bank's effective EUR↔USD differs from NBU's USD↔EUR cross-rate. Drift accumulates on cross-currency purchases.

**Why it happens:**
Three-currency triangulation isn't intuitive. Most articles say "use base currency rollup" without addressing that the conversion path matters.

**How to avoid:**
- For UAH rollup of a EUR-account transaction made in USD: use `amount` (EUR, what the bank actually debited) × NBU EUR/UAH rate on the day. **Do not** compute via the USD leg. Mono's spread on EUR↔USD is the bank's spread — surface it as "bank fee" if you care, but for total spent in UAH terms, the EUR-leg-only path matches what the user actually lost from their account balance.
- For "what did this purchase cost in USD" (rare display need), use `operationAmount` directly — no math needed.
- Document the three FX scenarios in code comments at the rollup module: (a) operation currency == account currency: trivial; (b) operation != account, account == UAH: use Mono's amount; (c) operation != account, account != UAH: use account currency × NBU on day.

**Warning signs:**
- UAH spending total drifts from "sum of (Mono UAH amount + NBU-converted EUR amount + NBU-converted USD amount)" by >0.5% per cross-currency purchase.
- A USD merchant charge through EUR card shows in the dashboard as "X UAH" computed via two different paths giving different answers.

**Phase to address:** **Middle — multi-currency rollup phase.** Pin the path explicitly. Write a property test: rollup must equal `account_currency_amount × rate_on_day` regardless of operation currency.

**Sources:**
- [Mono API field semantics — `amount` vs `operationAmount`](https://github.com/vitalik/python-monobank/blob/master/README.md)
- [How to Calculate Foreign Currency (treasurers.org)](https://learning.treasurers.org/resources/how-to-calculate-foreign-currency)
- [Double-conversion mechanics (Payoneer)](https://www.payoneer.com/resources/what-does-double-conversion-mean/)

---

### Pitfall 9: Internal transfers between own accounts/jars/cards — false positives both ways

**What goes wrong:**
The dashboard wants to **suppress** transfers between your own Mono accounts so they don't show up as `expense + income`. Two failure modes:
1. **False positive:** A 5,000-UAH expense at one merchant matches a 5,000-UAH income from a different merchant on the same day (refund, salary remainder, etc.) — the engine pairs them as "transfer" and hides both. User wonders where their salary went.
2. **False negative:** A real transfer between own accounts has a 1-2 second delay between debit and credit, slightly different fees, or one side records on day X and the other on day X+1. Engine misses the pair, both sides show.

**Why it happens:**
Mono doesn't tag self-transfers explicitly. Heuristic-based matching is the only option. Firefly III has multiple long-running issues on this exact class of bug: [#1349](https://github.com/firefly-iii/firefly-iii/issues/1349), [#4071](https://github.com/firefly-iii/firefly-iii/issues/4071), [#6377](https://github.com/firefly-iii/firefly-iii/issues/6377), [discussion #10191](https://github.com/orgs/firefly-iii/discussions/10191).

**How to avoid:**
- **Required signals for auto-pairing (all must match):** (a) sign opposite (one debit, one credit); (b) absolute amount equal in **a common currency** (use account currency × rate); (c) timestamp within a window — start at ±5 minutes, never more than 24h; (d) **both accounts are user's own** (Mono returns the full account list — internal transfers are between IDs in that list); (e) description on at least one side mentions transfer (`З: ` / `На: ` / `Переказ` / `Own` / "From"/"To" the other account name).
- **Conservative default:** only auto-pair when ≥3 signals match. Show as a "suggested transfer" with one-click confirm/reject when only 2 match. Never silently hide.
- **Idempotency:** an auto-paired transfer should be reversible. Store `(linked_pair_id, link_method: auto|manual, link_confidence)` so re-running detection doesn't re-create or duplicate links.
- **Refunds are not transfers.** Match refunds separately — same merchant string + opposite sign + within 60 days + amount within 1 unit (sometimes the refund is fractionally different due to fees) → suggest pairing, never auto-hide.
- **Hard rule:** never delete a transaction during auto-link. Mark it `linked_to=X`, render it suppressed in spending views, render it visible in the audit log.

**Warning signs:**
- "This month spending" total swings by a large round amount when a single transaction is recategorized — sign of a hidden transfer-pair flipping.
- The transaction feed shows a salary deposit one month and not the next (auto-paired with an unrelated outbound).
- A real transfer between Mono accounts shows up as both expense and income.

**Phase to address:** **Middle — reconciliation phase, after categorization.** Build the rules engine first; transfer detection sits on top.

**Sources:**
- [Firefly III #1349 — Import CSV: Transfer can be a "deposit"](https://github.com/firefly-iii/firefly-iii/issues/1349)
- [Firefly III #4071 — Transfers between asset and saving accounts always seem positive](https://github.com/firefly-iii/firefly-iii/issues/4071)
- [Firefly III #6377 — Imported transfers are always set positive](https://github.com/firefly-iii/firefly-iii/issues/6377)
- [Firefly III discussion #10191 — Help identifying transfers on import](https://github.com/orgs/firefly-iii/discussions/10191)

---

### Pitfall 10: Rules engine — manual edits overwritten by re-run

**What goes wrong:**
User manually recategorizes a transaction ("Glovo → Food, not Transport"). Next poll cycle re-runs the rules engine, the rule still matches, user's manual edit is silently overwritten.

**Why it happens:**
Naive rules engines apply rules to all transactions every run. Engineers don't think to flag "manual override" until the second time it bites them.

**How to avoid:**
- Per-transaction column: `category_source` (`enum: rule, manual, auto-llm`). Once `manual`, the rules engine **skips** that transaction unless explicitly re-applied with "force overwrite".
- In Actual Budget terms (see [#3702](https://github.com/actualbudget/actual/issues/3702)), make rule-application a **one-shot batch** with a clear UI: "Apply rules to N transactions, M will be overwritten — confirm." Never silent overwrite on import.
- New rules should default to applying to **future** transactions, with a separate explicit "back-apply to history" action that previews before committing.
- For each rule application, log: `(transaction_id, rule_id, prior_category, new_category, timestamp)` to a small audit table. This is the answer to "why was this categorized this way" — it should be browsable from the transaction detail page.

**Warning signs:**
- User opens dashboard the day after manually fixing a category; it's wrong again.
- No audit table; no way to explain why a transaction has its category.
- A unit test that runs rules twice in a row and expects identical state but actually loses manual edits.

**Phase to address:** **Middle — rules engine phase.** Bake `category_source` into the schema before you write the rules engine.

**Sources:**
- [Actual Budget #3702 — Rule "apply actions" not working when changing payee](https://github.com/actualbudget/actual/issues/3702)
- [Actual Budget #5154 — Run rules on the other side of a manually created transfer](https://github.com/actualbudget/actual/issues/5154)
- [Actual Budget rules docs — note "batch editor" framing](https://actualbudget.org/docs/budgeting/rules/)

---

### Pitfall 11: SQLite WAL mode on NFS / network storage — data corruption

**What goes wrong:**
User runs the app from a Synology / Unraid / TrueNAS share. SQLite database file lives on an NFS mount. WAL mode (the modern default) **does not work on network filesystems** — it relies on shared memory mappings and POSIX `fcntl()` advisory locks, both unreliable over NFS. Result: silent corruption, "database disk image is malformed", or random transaction loss after a power cycle.

**Why it happens:**
Synology's UI defaults to storing Docker volumes on the share. Users put the app's data dir on the same share without thinking. SQLite's defaults silently enable WAL when the writer wants it. Documented across Sonarr, Mozilla, GoToSocial, NixOS issues.

**How to avoid:**
- **Postgres in compose, not SQLite.** This stack is greenfield, single-user but with non-trivial query needs (reconciliation, multi-currency rollups, time-window aggregates). The cost of running a `postgres:16` container is a few hundred MB; the resilience improvement is large. (For the "I want this to be a single binary later" desire: cross that bridge later, don't pay for it now.)
- If keeping SQLite anyway: pin the data path to **a local filesystem inside the host**, not the NFS share. Use `journal_mode=DELETE` (not WAL) if you have any doubt. Document explicitly that the DB cannot live on `/mnt/share/*` paths.
- Either way, the docker-compose `volumes:` clause must point at a known local path with a clear comment.

**Warning signs:**
- `database disk image is malformed` in logs.
- Random `IntegrityError` on inserts after a clean restart.
- Data path resolves to an `nfs`/`smb`/`cifs` mount (`mount | grep <path>`).
- App works fine on dev laptop, corrupts within a week on the NAS.

**Phase to address:** **Early — deployment phase, even before the first real import.** The DB choice is forward-only painful to change once you have a year of data.

**Sources:**
- [SQLite docs — WAL mode does not work on network filesystems](https://sqlite.org/wal.html)
- [Sonarr #1886 — SQLite on Network Share](https://github.com/Sonarr/Sonarr/issues/1886)
- [GoToSocial — SQLite on networked storage docs (warning)](https://docs.gotosocial.org/en/latest/advanced/sqlite-networked-storage/)
- [Anomalyco/opencode #14970 — SQLite corruption on NFS](https://github.com/anomalyco/opencode/issues/14970)
- [SQLite — How to Corrupt a Database File](https://sqlite.org/howtocorrupt.html)

---

### Pitfall 12: Docker volume / bind-mount mistakes erase data on rebuild

**What goes wrong:**
Three flavors:
1. **Bind-mount path typo:** `/data/finance:/app/data` works first run; later edit becomes `/data/finance-bro:/app/data` — container starts with empty dir, app initializes a fresh DB, old data still on disk but no longer mounted.
2. **`docker compose down -v`** wipes named volumes. User runs it expecting to "stop the app" and loses all transaction history.
3. **Image rebuild + named volume**: when an image's `VOLUME` declaration ships with seed data, the *first* run populates the named volume. Subsequent rebuilds with new seed data **don't** update the volume — old seed data persists, user thinks the rebuild did nothing. (Documented in [docker/compose #7320](https://github.com/docker/compose/issues/7320).)
4. **Permissions:** Synology Docker UI doesn't honor `--user`; container runs as root, writes files owned by root; later `docker compose down && up` with PUID/PGID set runs as `1000:1000`, can't read its own data.

**Why it happens:**
Docker volume semantics are subtle. The "first run wins" behavior of named volumes is non-obvious. PUID/PGID conventions vary across Synology/Unraid/TrueNAS — see [vaultwarden discussion #2047](https://github.com/dani-garcia/vaultwarden/discussions/2047) and [pi-hole #328](https://github.com/pi-hole/docker-pi-hole/issues/328).

**How to avoid:**
- **Bind mount, not named volume**, for the database. User can see the file in their NAS file browser, back it up, restore it. Volume drivers are an extra layer of mystery for a homelab user.
- Pin the host path in `compose.yml` and document it in the README at the top: "Your data lives at `${DATA_DIR}/finance-bro/`. Back up this directory."
- `${DATA_DIR}` an env var with a sane default (`./data`). Never silently choose a hidden Docker-managed volume.
- Image runs as a fixed non-root UID (e.g. `appuser` UID 1000) by default. Document `PUID`/`PGID` env vars for users who need to override. Synology owners need an explicit note: "Set PUID=`id -u` of your share-owning user".
- Backup strategy is a **first-class feature**, not "we'll add it later" — see Pitfall 18.

**Warning signs:**
- After a `docker compose pull && up -d`, accounts and transactions list is empty.
- Schema migrations re-run from zero on every start.
- File ownership in the data dir is `root:root` — guaranteed permission grief later.
- `docker volume ls` shows an unfamiliar named volume holding all your data.

**Phase to address:** **Early — deployment phase.** Get the bind mount, default user, and backup story right in the first compose file.

**Sources:**
- [docker/compose #7320 — `--renew-named-volumes` reinitialize data](https://github.com/docker/compose/issues/7320)
- [docker/compose #9535 — `down` removing SQL data despite named volume](https://github.com/docker/compose/issues/9535)
- [docker/compose #4476 — `up --force-recreate` uses old volumes](https://github.com/docker/compose/issues/4476)
- [vaultwarden Synology PUID/PGID discussion #2047](https://github.com/dani-garcia/vaultwarden/discussions/2047)
- [pi-hole PUID/PGID issue #328](https://github.com/pi-hole/docker-pi-hole/issues/328)

---

### Pitfall 13: Schema migrations after a 6-month idle gap

**What goes wrong:**
User runs the app daily for 2 months, then doesn't touch the homelab for 6 months while traveling. They `docker compose pull` to update, the new image has 5 schema migrations queued. One of them: (a) renamed a column without preserving old data; (b) had a destructive `op.drop_column` on a column with user manual edits; (c) is non-idempotent and crashes mid-way leaving the DB in a half-migrated state with no rollback path.

**Why it happens:**
Alembic and Django migrations are forward-only. Their default behavior on partial failure leaves you in a wedged state where the `alembic_version` table shows the migration as not-yet-applied but the DDL is partially applied. Dev workflows of "delete the DB, re-init" don't survive into production. Personal projects skip migration testing because "I'll know when I make a change".

**How to avoid:**
- **Forward-only safety rule:** every migration must be safe to interrupt. Use SQL transactions where the DB supports them (Postgres yes, SQLite no for most DDL).
- **Pre-flight backup:** the app, on startup before running migrations, copies the current DB file (SQLite) or runs `pg_dump` (Postgres) to `${DATA_DIR}/backups/pre-migration-${git_sha}-${timestamp}`. Keep last 5.
- **Never drop columns in the same release as the code change that stops using them.** Two-phase: (1) release that ignores the column; (2) release N+1 that drops it. This rule applies even for solo projects — your future self in 6 months has no memory.
- **Test the gap explicitly:** in CI or as a scripted check, restore an old DB snapshot, run all migrations, assert no errors. Even one such test catches 90% of forward-incompat bugs.
- Document the migration tool prominently in the README: "Before updating, the app auto-backs-up to `${DATA_DIR}/backups/`. To roll back: stop container, restore the backup file, pin the previous image tag."

**Warning signs:**
- A migration uses `op.drop_column` directly with no comment about safety.
- No `backups/` directory is ever populated.
- Test suite never restores from a real DB snapshot.
- Image tag is `:latest` in compose (so user has no way to pin/rollback).

**Phase to address:** **Early — set up Alembic with a pre-flight backup hook from day one.** Cross-cutting throughout: every PR with a migration should be reviewed for forward-only safety.

**Sources:**
- [Alembic — Auto-Generating Migrations docs](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [Atlas — The Hidden Bias of Alembic and Django Migrations](https://atlasgo.io/blog/2025/02/10/the-hidden-bias-alembic-django-migrations)
- [Firefly III — backup-and-restore guidance #3107](https://github.com/firefly-iii/firefly-iii/issues/3107)
- [Firefly III #6435 — Data loss on Redeployment via helm chart (forward-only without backups)](https://github.com/firefly-iii/firefly-iii/issues/6435)

---

### Pitfall 14: Timezone — UTC in container, Europe/Kyiv for the user, Mono returns... what?

**What goes wrong:**
1. Mono returns `time` as Unix seconds (UTC, by definition). App stores it. Dashboard renders "this month" by `date(time)` — uses container's TZ, which is UTC by default. User in Kyiv at 22:00 sees a transaction in tomorrow's date.
2. Postgres `TIMESTAMPTZ` accepts the value, converts to UTC internally — fine. But code that filters by `time >= start_of_month` constructs the bound naively and gets the UTC start of month, not Kyiv's.
3. `pytz` is used for `Europe/Kiev`/`Europe/Kyiv` — wrong DST offset gets calculated because of pytz's known eager-offset bug. UA also abolished DST under wartime conditions in 2023+; pytz versions older than recent will be wrong about whether DST is active.
4. Cron / APScheduler uses container TZ (UTC) but user-facing "daily report at 8 AM" is meant in local time — fires at 11 AM Kyiv winter, 10 AM summer (or just always wrong post-DST-abolition).

**Why it happens:**
Defaults are convenient but wrong. UA's recent timezone policy churn (DST abolished, Kiev → Kyiv naming rollout, wartime considerations) means stale tzdata in containers is real risk.

**How to avoid:**
- **Storage layer:** all timestamps as `TIMESTAMPTZ` (Postgres) or `INTEGER` Unix seconds (SQLite). Never `TIMESTAMP WITHOUT TIME ZONE`.
- **Computation layer:** Python uses `zoneinfo` (stdlib), not `pytz`. `ZoneInfo("Europe/Kyiv")`. Pin `tzdata` package as an explicit dependency so it gets updated independent of the OS image.
- **Display + attribution layer:** "This month" uses `Europe/Kyiv` calendar boundaries. Document at the function level: `def attributed_day_kyiv(time_utc: datetime) -> date`.
- **Container TZ:** leave at UTC (default). Don't `TZ=Europe/Kyiv` in compose — it makes logs confusing and only kicks the can.
- Test: a 23:30 Kyiv-time purchase should be attributed to Kyiv's calendar day, not UTC's. Write that test once, in the date-attribution module.

**Warning signs:**
- A transaction made at 23:30 local time appears under tomorrow's date in the feed.
- `pytz` in `requirements.txt`.
- Container `TZ` env var is set (likely a workaround for an underlying bug).
- "Monthly" totals shift between two values depending on what time of day the user opens the app.

**Phase to address:** **Middle — calendar-attribution layer for the dashboard.** Setting it up correctly first time costs hours; fixing post-hoc means re-running attribution over all history.

**Sources:**
- [PostgreSQL Date/Time Types docs](https://www.postgresql.org/docs/current/datatype-datetime.html)
- [Lots of fun with Postgres and Python timezone shenanigans](https://jacopofarina.eu/posts/postgres-timezone-shenanigans/)
- [pytz vs zoneinfo discussion (psycopg #56)](https://github.com/psycopg/psycopg/discussions/56)
- [Python `zoneinfo` stdlib docs](https://docs.python.org/3/library/zoneinfo.html)

---

### Pitfall 15: Logging full Mono payloads at INFO — PII to disk and to wherever logs go

**What goes wrong:**
`logger.info(f"Got statement: {response.json()}")` logs every transaction's amount, merchant, MCC, balance, comment, and partial card mask. Logs ship to a file mounted on the host, possibly to a remote log aggregator if the user added one, possibly to stdout where Docker captures and (depending on driver) ships off-box. PII leaks. Logs not rotated → fill the disk → app crashes.

**Why it happens:**
Logger gets sprinkled in during debugging and never cleaned up. Default Python `logging` doesn't redact. `print()` in older code paths bypasses the logger entirely.

**How to avoid:**
- **Single rule: financial data goes to logs at DEBUG, never INFO+.** Default log level is INFO. To debug, you flip a switch.
- **Redaction filter on the logger:** keys `amount`, `description`, `comment`, `balance`, `cashbackAmount`, `accountId`, `id` (transaction id can be a known correlation key) get redacted at WARNING+. Below WARNING, only structured event names + non-sensitive correlation IDs are emitted.
- **Never** log the full Mono token. (`X-Token: ${token[:4]}…${token[-4:]}` if you must.)
- **Logs in a dedicated bind-mounted dir with rotation** (`logging.handlers.RotatingFileHandler` or `logrotate`). Cap at e.g. 100 MB.
- **No external monitoring SDK by default.** No Sentry, no Datadog, no Grafana Cloud. If user opts in to Sentry self-hosted, scrub PII keys via `before_send`. Document this prominently.

**Warning signs:**
- `grep -E '"amount"|"description"' /var/log/finance-bro/*.log` returns hits.
- A `requirements.txt` line for `sentry-sdk` with no scrub config.
- Container has no log rotation; `docker logs` returns gigabytes.
- Crash dumps / core dumps written to a world-readable path.

**Phase to address:** **Cross-cutting; lock the logging policy in Phase 1.** Easier to never-log than to grep-and-redact post-hoc.

**Sources:**
- [Python logging Filter docs (for redaction)](https://docs.python.org/3/library/logging.html#filter-objects)
- [Sentry `before_send` scrubbing docs](https://docs.sentry.io/platforms/python/configuration/filtering/)
- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)

---

### Pitfall 16: "This month" boundary ambiguity in UI

**What goes wrong:**
Dashboard says "This month: 25,000 UAH". User asks: is that calendar month (1st – today)? Rolling 30 days? Since-payday? Three different numbers, all defensible. User compares with bank app (calendar month) and they disagree → distrust.

**Why it happens:**
Spending tools don't agree. Qonto (per their docs) explicitly distinguishes "calendar month" vs "rolling 30". Emma users complain when budget interface doesn't align with biweekly pay. Most apps default-pick one and don't tell the user.

**How to avoid:**
- **Default: calendar month** (Europe/Kyiv). Match what the bank app shows so the user can sanity-check.
- Label the period explicitly in the widget: `May 2026` or `01 May – 10 May 2026`, never just "This month".
- Provide one alternative view: `last 30 days` — explicitly labeled. Don't surface the option until v1 is shipped, but design schema so adding it later is a one-line filter.
- For the comparison-to-prior-month widget: align periods (calendar-vs-calendar). If user is on day 10, comparing "10 days of May" to "all of April" is misleading. Comparison either: (a) clip both to today's day-of-month; (b) only show after day 28. Default to (a) with a tooltip.

**Warning signs:**
- Dashboard total disagrees with Mono app's "this month" by exactly the amount of last-day-of-prev-month transactions → timezone or boundary off by one.
- User asks "why is this number different from the bank app?" and there's no clear answer.
- Comparison widget shows "120% vs prior month" on day 1 — math is technically right but useless.

**Phase to address:** **Late — UI/dashboard phase.** The data model already has `attributed_day` (per Pitfall 6); UI just needs to declare its boundary.

**Sources:**
- [Qonto — calendar month vs rolling 30 days](https://support-fr.qonto.com/hc/en-us/articles/23947680708625-What-is-the-difference-between-the-budget-limit-over-a-calendar-month-and-30-rolling-days)
- [Why you shouldn't track metrics by calendar month](https://jeffmacaluso.github.io/post/WhyYouShouldntMeasureByCalendarMonth/)
- [Emma rolling-budget feedback thread](https://community.emma-app.com/t/rolling-budgets-feedback/3847)

---

### Pitfall 17: Single-user MVP scope creep — auth, multi-importer, LLM, before they're earned

**What goes wrong:**
Six weeks in, ~70% of the user-visible value is built. Then: "While I'm here, let me add login, since it's just a JWT middleware. And let me make the importer pluggable so I can add Wise next month. And let me try local Ollama categorization." Two more months pass. Nothing ships. The Mono path is still buggy because it didn't get the polish phase.

**Why it happens:**
Solo project, no external deadline, every "while we're here" feels free. Premature abstraction is the most common single-developer killer; PROJECT.md explicitly calls this out.

**How to avoid:**
- **Honor the PROJECT.md Out-of-Scope list literally.** Each entry has a documented reason. Re-adding an out-of-scope item requires deleting an Active item to fund it.
- **No abstractions for unbuilt importers.** The Mono importer is a Python module, not an interface. When (if) Wise/Privat appears in v1.5, you'll know the right interface from having two concrete implementations. Designing it from one is guessing.
- **No auth in v1.** Network gating is the trust boundary per PROJECT.md. Adding auth later requires designing it for one user with a clear access model, not retrofitting it as middleware. The Tailscale/LAN gating is the contract.
- **No LLM in v1.** Build the rules engine, observe what the long tail of un-categorized transactions actually looks like, then make an informed rules-vs-local-LLM-vs-API-LLM call. Building the interface speculatively means you'll pick the wrong abstraction.
- **Time-box scope.** Two months. Anything not contributing to "Visibility into where my money goes — zero manual upkeep" gets cut for v1. Maintain a `BACKLOG.md` so cut ideas don't feel lost.

**Warning signs:**
- A PR titled "add support for second importer" before the first importer has been in daily-use for 3+ weeks.
- A `BaseCategorizer` abstract class with one concrete subclass.
- Weekly time spent on `auth/` exceeds time spent on visualization.
- Frequent reads of "how to do X in Python/JS framework" — you're learning a new stack alongside building.

**Phase to address:** **Cross-cutting — every phase transition needs a "are we still in scope?" check.** The roadmapper should encode this as an explicit gate.

**Sources:**
- PROJECT.md (this repo)
- [Hacker News thread on premature abstraction in personal projects (anecdotal patterns)](https://news.ycombinator.com/item?id=27711292) — `[anecdotal]` for the broader pattern.

---

### Pitfall 18: "I'll add a backup later" — there is no backup

**What goes wrong:**
Six months in, NAS disk fails / Docker volume gets pruned by `docker system prune -a` / `down -v` typo / migration goes sideways. User has no copy of accounts, transactions, manual edits, rules. Token can be re-issued from Mono — but Mono only serves last 31 days from the moment of re-issue, and earlier history that was synced is gone forever (Mono doesn't keep arbitrarily long history accessible via `from`/`to`; the practical horizon is what their backend retains, which is bank-statement-bound, not API-tested for unlimited backfill).

**Why it happens:**
Backup is the single most-deferred feature in homelab projects. "It's running on my own hardware, I'll set it up next weekend." Six months pass.

**How to avoid:**
- **Day-one feature, not v1.5.** Compose includes a sidecar (or a cron in the app container) that runs `pg_dump` (or copies the SQLite file with `sqlite3 .backup`) every 24h to `${DATA_DIR}/backups/`. Keep the last 30 daily, last 12 monthly.
- **Document restore procedure** in the README before it's ever needed: "Stop the app, copy backup file to `${DATA_DIR}/db/finance-bro.db`, start the app." Run the procedure once, manually, before declaring v1 shipped — that's the only way to know the backup actually works.
- **Token export as separate concern:** the Mono token must be exportable / re-importable cleanly. Don't store it only in a Docker secret that disappears with the container.
- **"Leave anytime" CSV export** — even if not shown in UI, a small CLI / management command that dumps all transactions to CSV. This is both the data-export answer and a poor-man's backup.

**Warning signs:**
- `${DATA_DIR}/backups/` empty.
- User has never tested restoring.
- Token only lives in `secrets:` in compose with no separate copy.
- Re-issuing a Mono token requires re-running Mono's app onboarding flow — which the user might not remember in 6 months.

**Phase to address:** **Early — bake backup into the deploy phase, not later. Test restore before declaring v1 done.**

**Sources:**
- [Firefly III — How to make a backup](https://docs.firefly-iii.org/how-to/firefly-iii/advanced/backup/)
- [Firefly III backup script (gist)](https://gist.github.com/dawid-czarnecki/8fa3420531f88b2b2631250854e23381)
- [Firefly III #1704 — Backup and restore data the right way](https://github.com/firefly-iii/firefly-iii/issues/1704)
- [SQLite Online Backup API](https://sqlite.org/backup.html)
- [Postgres `pg_dump` docs](https://www.postgresql.org/docs/current/app-pgdump.html)

---

## Moderate Pitfalls

### Pitfall 19: Mono `comment` field is user-mutable — don't use it as a key

**What goes wrong:**
The `comment` field in Mono can be edited by the user **after** the transaction lands (in the Mono app). If your dedup, refund-matching, or rules engine keys off `comment`, edits in Mono break your app's invariants.

**How to avoid:** Treat `comment` as user content, never as a stable identifier. Persist a copy at first-seen time but match on `description` (merchant string from the merchant's terminal) for rules.

**Warning signs:** A rule's match flips after the user edits a comment in the Mono app.

**Phase to address:** Middle — rules engine.

**Sources:** Mono API behavior described in [vergilet/monobank docs](https://vergilet.github.io/monobank/) `[anecdotal]` for the user-mutability via the app — the app exposes the comment field for editing; verify against current Mono UX behavior at implementation time.

---

### Pitfall 20: Currency-code field is **numeric** ISO 4217, not alphabetic

**What goes wrong:**
Mono returns `currencyCode: 980` (UAH), not `"UAH"`. Code that does `if currency == "UAH"` always fails. Code that does `str(currency)` puts the literal `"980"` in the UI.

**How to avoid:**
- One mapping module: `{980: "UAH", 840: "USD", 978: "EUR", ...}`. Apply at the importer boundary; everything downstream uses alphabetic.
- Validate at import: any code not in the mapping is logged at WARNING and the transaction is still imported (don't block on unknown currencies — user might have made a TRY purchase). The mapping should cover at least UAH, USD, EUR, GBP, PLN, TRY for a UA user.

**Warning signs:** UI shows "980 25.00" instead of "₴25.00".

**Phase to address:** Early — importer.

**Sources:**
- [ISO 4217 — Wikipedia](https://en.wikipedia.org/wiki/ISO_4217)
- [iban.com currency code list](https://www.iban.com/currency-codes)

---

### Pitfall 21: MCC interpretation has long tail and regional variance

**What goes wrong:**
MCC (Merchant Category Code) is an ISO 18245 four-digit code. Mono returns it. Naive code maps it to a category via a hardcoded dict, but the same MCC can mean different things in different markets, and merchants frequently mis-classify themselves (a coworking space coded as `7372` "computer programming" instead of `7011` "lodging"). Over-trusting MCC creates a long tail of wrong categorizations.

**How to avoid:** MCC is a **signal, not the answer.** Use it as one rule input alongside merchant string regex. Allow user override at the per-merchant-pattern level. Track which categorizations came from MCC-only vs description-match so you can tune.

**Warning signs:** A surprisingly large "Other" category, or a category dominated by a single MCC the user keeps recategorizing.

**Phase to address:** Middle — rules engine.

**Sources:** [ISO 18245 / MCC reference](https://en.wikipedia.org/wiki/Merchant_category_code); MCC mis-classification is endemic in card processing — `[anecdotal]` for the specific examples.

---

### Pitfall 22: Jar transactions look different from card transactions

**What goes wrong:**
Mono "jars" (банки) are savings sub-accounts. They appear in `client_info` alongside cards but with different field semantics: a jar's `amount` is the running balance contribution, jar transfers (top-ups, withdrawals) often appear on **both** the source card and the jar with mirrored signs. Naive importers double-count jar top-ups as expense (from card) AND ignore jar income, or vice versa.

**How to avoid:** Treat jars as a distinct account type in the schema. Auto-pair card↔jar transfers via the internal-transfer logic (Pitfall 9) — both ends are user-owned. Render jar contributions as "Savings" not "Expense" in the dashboard.

**Warning signs:** Total spending in May includes the user's 5,000-UAH jar top-up as a "Coffee/Subscriptions/Other" category.

**Phase to address:** Middle — reconciliation.

**Sources:** [Mono FOP/jars distinctions in vtopc/go-monobank](https://pkg.go.dev/github.com/vtopc/go-monobank); jar-only-on-personal-API noted in `monobank-api` docs.

---

### Pitfall 23: Token revocation / rotation has no graceful path

**What goes wrong:**
Mono's personal token is bound to one Telegram OTP flow. If revoked (by the user, or by Mono on suspected abuse), the app starts returning 401s. If the app's only error path is "log and retry", it spams 401s forever.

**How to avoid:** Distinguish 401 (auth failure) from 429 (rate limit). On 401, **stop the scheduler** and surface a "token needed" state in the UI. Document the re-onboarding flow.

**Warning signs:** Logs show a steady stream of 401s; app keeps polling.

**Phase to address:** Early — importer error-handling.

**Sources:** [Monobank API docs](https://api.monobank.ua/docs/index.html); 401 vs 429 distinction is HTTP-standard.

---

### Pitfall 24: Single-process Python with blocking IO + scheduler — scheduler starves

**What goes wrong:**
APScheduler in `BlockingScheduler` mode + `requests` (synchronous) for Mono calls + Flask/FastAPI in dev mode all share the same event loop or process. A 30-second Mono call (rate-limited, slow response) blocks the scheduler, which then misses the next tick. Or worse: the web request handler blocks waiting for Mono and the UI hangs.

**How to avoid:** Separate processes / async-everything. Options:
- FastAPI + `httpx.AsyncClient` for Mono calls; APScheduler in `AsyncIOScheduler` mode; a single asyncio event loop.
- OR: a separate worker container (Celery / RQ / APScheduler in its own process) for poll jobs; web container only serves UI.

The second is more robust but heavier infrastructure for a single-user app. Pick one and document it.

**Warning signs:** UI hangs while the scheduler is mid-poll. Polls drift by minutes from their schedule.

**Phase to address:** Early — architecture.

**Sources:** APScheduler docs on schedulers; `[anecdotal]` for the specific failure pattern, but it's universal in Python web apps that mix sync IO with timers.

---

### Pitfall 25: Frontend localStorage with sensitive payloads

**What goes wrong:**
Frontend caches the raw transaction list in `localStorage` for offline / fast-load use. localStorage is plaintext, world-readable to any JS on the origin (XSS), and persists across sessions. Anyone with brief access to the user's browser sees their financial history.

**How to avoid:**
- For v1: don't cache transaction data in localStorage. SPA fetches fresh on load. The 2-second delay is fine for a single-user app.
- If caching is later wanted: use IndexedDB (still plaintext but slightly more obscure), or just `sessionStorage` (cleared on tab close). Better: don't.
- Mono token never leaves the backend. Frontend doesn't need it.

**Warning signs:** `localStorage.setItem("transactions", ...)` in the JS bundle. DevTools "Application → Storage" shows a 2 MB JSON blob.

**Phase to address:** Late — frontend phase, but called out early.

**Sources:** [OWASP — Don't store sensitive data in localStorage](https://owasp.org/www-community/HttpOnly); industry consensus on localStorage for finance data.

---

## Minor Pitfalls

### Pitfall 26: Sort order on a feed mixing hold and cleared
Feed sorted by `time` descending shows a hold-then-cleared pair as two adjacent items, which looks like a duplicate. Either suppress holds entirely once cleared (preferred) or visually distinguish.
**Phase:** Late — UI.

### Pitfall 27: Mobile viewport assumptions
Designing for a desktop browser dev tools "iPhone 14" preview is not the same as the user's actual phone. Touch targets, fixed-header overlap with iOS Safari, viewport-height (`100vh` bug on iOS) — all bite the first time the user opens it on a real phone.
**Phase:** Late — UI; test on the actual target phone before declaring v1 done.

### Pitfall 28: Refund-matching tying unrelated charges
Two coffees from the same café three weeks apart match as "purchase + refund" because amount and merchant match. Same problem as transfer false-positives. Same fix: surface as suggestion, never auto-hide; require user confirmation for the first match per merchant.
**Phase:** Middle — reconciliation.

### Pitfall 29: Regex-based rules slow on full-history re-runs
A regex-heavy rules engine running over 10,000 historical transactions on every poll is wasteful and may be slow. Mark rules dirty only on rule edit / new import. Don't re-evaluate clean transactions on every poll.
**Phase:** Middle — rules engine.

### Pitfall 30: Decimal precision drift in aggregation
Even with `Decimal`, summing thousands of values with the default precision and rounding mode can produce surprises if some values come from FX-converted amounts with arbitrary precision. Standardize: input precision = 2 decimal places (kopeck-equivalent), FX rate = 8 decimals, aggregate context = 28 with `ROUND_HALF_EVEN`.
**Phase:** Early — money library.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|---|---|---|---|
| SQLite instead of Postgres | Single-file deploy, no extra container | Dies on NFS (Pitfall 11); no real concurrency; harder migrations; reconciliation queries clunkier | **Acceptable** if user pins data path to local FS and stays single-user. Recommend Postgres anyway for this stack. |
| Skip backup on day one | Faster v1 shipping | Pitfall 18 — total data loss eventually | **Never acceptable.** Day-one feature. |
| `:latest` image tag in compose | Easy auto-update | No rollback path; migration breakage strands user on bad version | Never. Pin to a version, document update procedure. |
| Hardcoded category list in code | Quick start | User can't edit; PR needed for every new category | Acceptable for v0 (week 1); replace with DB-backed before v1 ships. |
| `print()` instead of structured logger | Faster dev | Pitfall 15 — PII in logs, no rotation | Never in code that handles transactions. |
| Skipping rate-limiter implementation in dev because "I won't hit it" | Faster local iteration | Token throttle in production / live test | Acceptable only against a recorded-fixture mock; never against real Mono. |
| Single-currency v0 (UAH only) | Faster first slice | Three months of data accumulate in single currency, then multi-currency retrofit destroys assumptions | **Avoid.** Multi-currency is in PROJECT.md "Active". Build the schema for it from day one even if UI is UAH-only at first. |
| Mock NBU in dev | Avoid weekend-rate testing complexity | Real weekend rate gap not handled | Acceptable for unit tests; integration test must hit a recorded-real-Sunday fixture. |
| No category override / `category_source` column | Simpler schema | Pitfall 10 — manual edits clobbered | Never. Add the column from day one. |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|---|---|---|
| Monobank `/personal/statement` | Pass year-long range, treat empty as "no data" | Chunk to 30-day windows; treat 4xx as error, not absence; rate-limit each call |
| Monobank `/personal/client-info` | Re-poll on every cycle | Cache for 1+ hour; this never changes during a session |
| Monobank rate limit | `time.sleep(1)` retry loop | Token-bucket gate; back off ≥60s on 429; respect `Retry-After` |
| Monobank token | Stored only in compose `secrets:` | Also kept in user-controlled backup; document re-issue flow |
| NBU rate API | Treat empty (weekend) as `0` | Fall back to most recent prior business-day rate; cache forever |
| NBU rate API | Re-fetch all history on every poll | Fetch only missing dates; rates don't change post-publication |
| Postgres in compose | No backup volume | Daily `pg_dump` to `${DATA_DIR}/backups/` |
| Docker volumes | Named volume for DB | Bind mount to user-visible path |
| Frontend ↔ backend | Token exposed to browser | Token stays server-side; browser only sees session cookie or gets nothing if network-gated |

---

## Performance Traps

These are mostly *not* a concern at single-user scale, but listed because two will bite even at small scale.

| Trap | Symptoms | Prevention | When It Breaks |
|---|---|---|---|
| Re-running all rules over full history on every poll | Poll cycle takes seconds → minutes as history grows | Apply rules to new transactions only; mark rules dirty on edit | ~5,000+ transactions |
| Frontend fetches full transaction list on every page load | Slow load on mobile cellular | Paginate; serve "last 30 days" by default | ~2,000+ transactions |
| Unindexed (account_id, time) on transactions | Slow "this month" queries | `CREATE INDEX` on `(account_id, attributed_day)` | ~1,000+ transactions |
| Synchronous Mono polling blocks scheduler / web | UI hangs during poll | Async client + separate poll process | Always — even at 1 account |

---

## Security Mistakes

| Mistake | Risk | Prevention |
|---|---|---|
| Monobank token committed to git | Token usable by anyone with read on the repo; revocation requires user re-onboarding | `.env` in `.gitignore`; pre-commit hook scans for `uX...` prefix; document token-rotation procedure |
| App listening on `0.0.0.0` without auth assumption | If user moves off Tailscale/LAN, instant exposure | Comment in compose: "do not expose port without re-doing threat model"; PROJECT.md Out-of-Scope explicitly bans this |
| Logs ship financial data off-box | PII leak via Sentry / logging service | No external monitoring SDK by default; if added, scrub `amount`, `description`, `comment` |
| Frontend stores transactions in localStorage | Browser-resident PII; persists across sessions | Don't cache; fetch fresh |
| Mono token in localStorage / cookie | Token usable from any JS on origin | Token never leaves backend |
| Backup files world-readable | NAS-share user can read everyone's data | Backups in dir mode 0700, owner = app user |

---

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---|---|---|
| "This month" without explicit period label | User thinks rolling 30, dashboard means calendar | Show period as "01–10 May 2026" |
| Auto-hiding suspected transfers without indication | User can't find a salary deposit; thinks it's lost | Visually mark hidden+linked transactions; show in audit log |
| Hold and cleared shown as two rows | Looks like double charge | Suppress hold once cleared; visual distinction otherwise |
| Comparison-to-prior-month on day 1 of month | "120% over last month!" — meaningless | Clip both periods to today's day-of-month or hide widget pre-day-7 |
| Foreign-currency transaction shown in UAH only | User loses sight of original amount | Always show both: `€25.00 (1,089 UAH)` |
| Categorization happens silently on import | User doesn't know which rule fired | Audit-log link on each transaction |
| Rules run automatically on existing transactions when added | Manual edits silently overwritten | New rules apply to future only by default; explicit "back-apply with preview" action |
| No way to manually re-categorize | User stuck with a wrong category | Quick re-categorize on transaction feed (PROJECT.md already lists this) |

---

## "Looks Done But Isn't" Checklist

- [ ] **Importer:** end-to-end round trip — import a real fixture, render, totals match Mono app to the kopeck.
- [ ] **Backup:** restore tested manually on a fresh container; verify all transactions present.
- [ ] **Rate limiter:** integration test that fires 5 calls in 30 seconds, verifies only 1 hits Mono and 4 are queued.
- [ ] **Backfill:** verifies the 31-day chunking; runs against a 12-month range and dedups overlap correctly.
- [ ] **Hold→cleared:** test that imports a hold, then a cleared with a different amount, and ends with one row in DB with the cleared amount, hold record audit-logged.
- [ ] **Internal transfer:** test that the salary deposit + same-day same-amount unrelated expense are NOT auto-paired (two-of-five-signals threshold).
- [ ] **Manual override:** test that re-running rules after a manual recategorize does not change `category_source = manual` rows.
- [ ] **Multi-currency:** test that a EUR account's UAH rollup is computed via `amount × NBU_eur_uah`, not via the operation-currency leg.
- [ ] **Weekend FX:** test that a Sunday-dated transaction's rollup uses Friday's NBU rate.
- [ ] **Timezone:** test that a 23:30 Kyiv-time transaction is attributed to the Kyiv calendar day, not UTC.
- [ ] **Migrations:** test that an old-schema DB snapshot upgrades cleanly through all migrations.
- [ ] **Backup-on-migrate:** verify that a pre-migration backup file is created on app start.
- [ ] **Token revocation:** test that 401 stops the scheduler and surfaces a UI state.
- [ ] **Logging:** grep logs at INFO for `amount`/`description`/`comment` after a poll cycle — should be zero hits.
- [ ] **Mobile:** opened on the actual target phone; touch targets, viewport, top-bar overlap all checked.
- [ ] **Data export:** CSV export works and round-trips back via a re-import (or at least preserves enough to reconstruct).
- [ ] **Network gating doc:** README explicitly states the trust model; compose port binding is local-only by default.
- [ ] **Currency code mapping:** unknown currency code logs a warning and still imports; UI doesn't show "980 25.00".

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---|---|---|
| Floats in DB schema | HIGH | Add new `BIGINT kopecks` column; migrate values; ban old column; deprecate. Plan a downtime window. |
| `mono_id` non-unique global PK | HIGH | Add composite PK migration; renumber FKs. Painful — invest the day to design correctly upfront. |
| WAL-on-NFS corruption | HIGH–TOTAL | Restore from backup (Pitfall 18). If no backup: re-issue Mono token, lose all manual edits and rules. |
| Backfill miscount due to 31-day bug | LOW | Wipe transactions, re-backfill with corrected helper. Manual edits + rules survive in their tables. |
| Auto-paired transfer false positive | LOW | Provide "unlink" UI; log all auto-pairs reversibly. |
| Rules clobbered manual edits | MEDIUM | If audit log exists: re-apply manual edits from log. Without audit log: lost. |
| Postgres data lost (no backup) | TOTAL | Same as WAL-on-NFS. Manual transaction reconstruction not feasible. |
| Token revoked unexpectedly | LOW | Re-issue via Mono onboarding; backfill last 31 days; older history already in DB if backup is intact. |
| Migration crashed mid-way | MEDIUM | Restore pre-migration backup (Pitfall 13); fix migration; re-run. |
| FX rollup drift | LOW | Re-run rollup with corrected logic over historical data; rollup is a derived view. |

---

## Pitfall-to-Phase Mapping

Map of pitfalls → which roadmap phase should prevent them. Roadmapper, lift these into phase success criteria.

| Pitfall | Prevention Phase | Verification |
|---|---|---|
| 1. Floats for money | Phase 1: Data model | Schema review; grep for `Float`/`Real` in models; round-trip test |
| 2. Off-by-100 minor units | Phase 2: Importer | Round-trip test against bank-app total |
| 3. Mono `id` uniqueness + hold dedup | Phase 1+2: Schema + Importer | Composite PK in migration; hold→cleared test |
| 4. Rate limit | Phase 2: Importer (first thing) | Integration test with 5 rapid calls |
| 5. 31-day backfill window | Phase 2: Importer | 12-month backfill test, dedup-overlap test |
| 6. `time` semantics + month attribution | Phase 4: Dashboard | 23:30-Kyiv-purchase test |
| 7. NBU weekend gaps | Phase 3: Multi-currency rollup | Sunday-rate fallback test |
| 8. Multi-hop FX | Phase 3: Multi-currency rollup | Cross-currency rollup property test |
| 9. Internal-transfer detection | Phase 5: Reconciliation | False-positive test (salary + same-amount expense) |
| 10. Rules overwriting manual edits | Phase 5: Rules engine | `category_source = manual` skip-rule test |
| 11. SQLite-on-NFS / DB choice | Phase 1: Deploy | Compose review; data path documented |
| 12. Volume / bind-mount mistakes | Phase 1: Deploy | Restart-and-data-survives test |
| 13. Migration after gap | Phase 1: Deploy + cross-cutting | Pre-flight backup hook; test old-snapshot upgrade |
| 14. Timezone | Phase 4: Dashboard (calendar attribution) | 23:30 Kyiv-purchase attribution test |
| 15. Logging PII | Phase 1: Cross-cutting (logging policy) | grep INFO logs for sensitive keys |
| 16. "This month" UX | Phase 6: UI polish | Period label visible; comparison clipped |
| 17. Scope creep | Cross-cutting (every phase transition) | PROJECT.md Out-of-Scope review at each transition |
| 18. Backup | Phase 1: Deploy | Manual restore test before declaring v1 done |
| 19. Comment field as key | Phase 5: Rules | Don't match on `comment`; description-only |
| 20. Numeric currency code | Phase 2: Importer | Mapping module; warning on unknown code |
| 21. MCC interpretation | Phase 5: Rules | MCC as one signal among many; user override path |
| 22. Jar transactions | Phase 2+5: Importer + Reconciliation | Jar accounts modeled distinctly; auto-paired with cards |
| 23. Token revocation | Phase 2: Importer (error handling) | 401 stops scheduler; UI state for "token needed" |
| 24. Scheduler starvation | Phase 1: Architecture | Async client + appropriate scheduler mode |
| 25. localStorage PII | Phase 6: Frontend | DevTools storage check; no transaction data persisted |
| 26. Hold/cleared sort | Phase 6: UI | Suppress hold once cleared |
| 27. Mobile viewport | Phase 6: UI | Test on actual phone |
| 28. Refund-match unrelated charges | Phase 5: Reconciliation | Suggestion-not-auto-hide UI |
| 29. Regex perf on full history | Phase 5: Rules | Apply only to dirty transactions |
| 30. Decimal precision drift | Phase 1: Money library | Aggregation context test |

---

## Sources

### Mono API
- [Monobank API spec (vergilet/monobank Ruby client — best summary of API constants)](https://vergilet.github.io/monobank/)
- [Monobank Open API docs (official)](https://api.monobank.ua/docs/index.html)
- [Monobank Corporate API docs (official) — explicit no-rate-limit comparison](https://api.monobank.ua/docs/corporate.html)
- [python-monobank (vitalik) README](https://github.com/vitalik/python-monobank/blob/master/README.md)
- [siomochkin/monobank-open-api-documentation (community v2303)](https://github.com/siomochkin/monobank-open-api-documentation)
- [smaugfm/monobudget — production Mono importer reference](https://github.com/smaugfm/monobudget)
- [vtopc/go-monobank — field semantics (especially `hold`, FOP fields)](https://pkg.go.dev/github.com/vtopc/go-monobank)
- [dnullproject/mono-to-actualbudget](https://github.com/dnullproject/mono-to-actualbudget)

### NBU / FX
- [NBU developer API directory](https://bank.gov.ua/en/open-data/api-dev)
- [NBU exchange rates page](https://bank.gov.ua/en/markets/exchangerates)
- [floatrates.com NBU mirror](https://www.floatrates.com/source/nbu/)
- [kastaneda/nbu_rates archive](https://github.com/kastaneda/nbu_rates)
- [Payoneer — double conversion explainer](https://www.payoneer.com/resources/what-does-double-conversion-mean/)

### Money / Decimal
- [Still Using Python float for Money? (Medium)](https://medium.com/the-pythonworld/still-using-python-float-for-money-heres-why-that-s-dangerous-c761b994c526)
- [How I Lost $10,000 Because of a Python Float (Medium)](https://medium.com/@pranaysuyash/how-i-lost-10-000-because-of-a-python-float-and-how-you-can-avoid-my-mistake-3bd2e5b4094d)
- [Python `decimal` stdlib docs](https://docs.python.org/3/library/decimal.html)
- [ISO 4217 currency codes](https://en.wikipedia.org/wiki/ISO_4217)

### Reconciliation / Rules / Categorization (existing project bug reports)
- [Firefly III #2358 — duplicate detection failure](https://github.com/firefly-iii/firefly-iii/issues/2358)
- [Firefly III #1349 — Transfer can be a "deposit" on import](https://github.com/firefly-iii/firefly-iii/issues/1349)
- [Firefly III #4071 — transfers between asset/saving accounts always positive](https://github.com/firefly-iii/firefly-iii/issues/4071)
- [Firefly III #6377 — imported transfers always positive](https://github.com/firefly-iii/firefly-iii/issues/6377)
- [Firefly III discussion #10191 — help identifying transfers](https://github.com/orgs/firefly-iii/discussions/10191)
- [Firefly III duplicate detection discussion #10579](https://github.com/orgs/firefly-iii/discussions/10579)
- [Actual Budget #3702 — rule "apply actions" not working](https://github.com/actualbudget/actual/issues/3702)
- [Actual Budget #5154 — rules on the other side of manual transfers](https://github.com/actualbudget/actual/issues/5154)
- [Actual Budget #3235 — regex rules not applying to imported transactions](https://github.com/actualbudget/actual/issues/3235)
- [Actual Budget rules docs](https://actualbudget.org/docs/budgeting/rules/)

### Self-hosted / Docker / Storage
- [SQLite WAL docs — does not work on network filesystems](https://sqlite.org/wal.html)
- [SQLite — How to Corrupt a Database File](https://sqlite.org/howtocorrupt.html)
- [Sonarr #1886 — SQLite on network share](https://github.com/Sonarr/Sonarr/issues/1886)
- [GoToSocial — SQLite on networked storage warning](https://docs.gotosocial.org/en/latest/advanced/sqlite-networked-storage/)
- [docker/compose #7320 — `--renew-named-volumes`](https://github.com/docker/compose/issues/7320)
- [docker/compose #9535 — `down` removing data](https://github.com/docker/compose/issues/9535)
- [docker/compose #4476 — `up --force-recreate` and old volumes](https://github.com/docker/compose/issues/4476)
- [vaultwarden Synology PUID/PGID discussion #2047](https://github.com/dani-garcia/vaultwarden/discussions/2047)
- [pi-hole PUID/PGID issue #328](https://github.com/pi-hole/docker-pi-hole/issues/328)
- [Firefly III backup docs](https://docs.firefly-iii.org/how-to/firefly-iii/advanced/backup/)
- [Firefly III #1704 — backup and restore](https://github.com/firefly-iii/firefly-iii/issues/1704)
- [Firefly III #6435 — data loss on redeployment](https://github.com/firefly-iii/firefly-iii/issues/6435)

### Migrations / Timezone
- [Atlas — The Hidden Bias of Alembic and Django Migrations](https://atlasgo.io/blog/2025/02/10/the-hidden-bias-alembic-django-migrations)
- [Alembic Auto-Generating Migrations docs](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [PostgreSQL Date/Time Types docs](https://www.postgresql.org/docs/current/datatype-datetime.html)
- [Postgres + Python timezone shenanigans (jacopofarina.eu)](https://jacopofarina.eu/posts/postgres-timezone-shenanigans/)
- [Python `zoneinfo` stdlib docs](https://docs.python.org/3/library/zoneinfo.html)

### UX
- [Qonto — calendar month vs rolling 30 days](https://support-fr.qonto.com/hc/en-us/articles/23947680708625-What-is-the-difference-between-the-budget-limit-over-a-calendar-month-and-30-rolling-days)
- [Why you shouldn't track metrics by calendar month](https://jeffmacaluso.github.io/post/WhyYouShouldntMeasureByCalendarMonth/)
- [Emma rolling-budgets feedback](https://community.emma-app.com/t/rolling-budgets-feedback/3847)

### Project context
- `.planning/PROJECT.md` (this repo)

---
*Pitfalls research for: self-hosted Monobank-based personal-finance importer/dashboard, multi-currency, single-user, Docker-on-homelab, polling-only.*
*Researched: 2026-05-10*
