# Phase 5: Honest Totals - Context

**Gathered:** 2026-05-31
**Status:** Ready for planning

<domain>
## Phase Boundary

A backend/API-only reconciliation engine (`UI hint: no`) that runs **after categorization** in the import pipeline. It makes spending math honest by:

- **Internal transfers** (REC-01): detecting opposite-sign legs between user-owned Mono accounts/jars/cards, auto-pairing high-confidence matches as `internal_transfer`, excluding them from spending, and surfacing lower-confidence pairs via `GET /api/links/pending` (never auto-hidden).
- **Refunds** (REC-02): pairing a charge with its refund (same account, opposite sign, overlapping counterparty/MCC, ±60d) as `refund`; the pair nets to zero (or to the remainder for partial refunds) in spending views, while the original charge remains visible in transaction detail.
- **Dedup safety** (REC-03): duplicate detection on re-import/backfill overlap stays **idempotency-key based** (`(account_id, source_tx_id)`), never heuristic — two legitimate identical-amount coffees on the same day are never collapsed.
- **Reversibility** (REC-01 SC#4): every auto-paired or confirmed link is reversible via `DELETE /api/transactions/{id}/link/{link_id}`; re-running reconciliation does not re-create an unlinked pair without a fresh signal.

**Out of phase (other phases own these):** the dashboard/feed UI and the visible "this month spent" surface (Phase 6 — this phase only exposes the netting mechanism it consumes); manual merge/split/cash entry (Phase 6); LLM/ML pairing; cross-currency *auto*-pairing (v1 surfaces these as candidates only); split/multi-refund matching (deferred).

</domain>

<decisions>
## Implementation Decisions

### Cross-Currency Transfer Matching (REC-01)
- **D-01:** **Auto-pair requires same-currency exact match.** Two transfer legs auto-pair only when they share a currency and `abs(amount_minor)` is exactly equal (plus the other gates). No FX math on the auto-pair path → zero FX-rounding false positives.
- **D-02:** **Mixed-currency moves are candidates only, never auto-paired.** For *candidate detection*, FX-normalize both legs to UAH (reuse the Phase 3 LATERAL rollup) and flag a pending candidate when the normalized amounts match within a small percentage tolerance (to absorb FX rounding) AND the other gates hold. Surfaced via `GET /api/links/pending` for user confirmation.

### Refund Matching Depth (REC-02)
- **D-03:** **Scope = full + partial single refunds.** A charge may be paired with one refund. A full refund (`refund == charge`) nets the pair to zero; a partial refund (`refund < charge`, opposite sign) nets to the **remainder** (charge 500 + refund −200 → 300 counts as spent). Split / one-charge-many-refunds (many-to-one) is **deferred** — not in v1.
- **D-04:** **Partial-refund attachment heuristic:** among unrefunded charges with overlapping counterparty/MCC in the ±60d window, attach the refund to the **smallest charge whose amount ≥ the refund**, tie-broken by **most recent**. Avoids attaching a small refund to a large unrelated charge.
- **D-05:** **Partial refunds auto-pair on the same gate set as full refunds** (no candidate-only treatment) — if a partial passes all four hard gates (D-07), it auto-pairs like a full refund.

### Confidence & Auto-Pair Model (REC-01, REC-02; Pitfall 9)
- **D-06:** **Confidence = signal-count buckets, not a weighted score.** Confidence is derived from how many discrete signals match — no per-signal weights to tune (no training data). Auto-pair pairs store confidence ≥ 0.8.
- **D-07:** **Four hard gates are mandatory before any AUTO-pair** (per pairing type):
  1. **Opposite sign** (one outflow, one inflow).
  2. **Amount match** — transfers: exact same-currency `amount_minor`; refunds: `refund ≤ charge`.
  3. **Tight time window** — transfers ±2 days; refunds ±60 days.
  4. **Ownership / identity** — transfers: both legs are user-owned accounts (i.e. both imported); refunds: same account + overlapping counterparty/MCC.

  All four exact gates pass → **auto-pair** (confidence ≥ 0.8).
- **D-08:** **Candidate path = soft-miss of exactly ONE gate** → surfaced in `GET /api/links/pending` (not ignored). The three accepted soft-misses:
  - **Amount:** FX-approximate instead of exact (the D-02 mixed-currency case).
  - **Identity:** MCC overlaps but counterparty/IBAN differs (refunds).
  - **Time:** outside the tight window but inside an outer grace band.

  **Soft-miss of two or more gates → ignored** (not surfaced).

### Reversal & Re-Run Memory (REC-01 SC#4)
- **D-09:** **Suppression = a tombstone keyed on the ordered transaction pair `(tx_a_id, tx_b_id)` plus a fingerprint of the signals at unlink time** (amounts, times). A `DELETE` of a link (or a reject of a pending candidate) writes a live tombstone; reconciliation skips any pair with a live tombstone. Survives re-imports and history re-sweeps.
- **D-10:** **Fresh signal = an underlying leg changed since the tombstone was written.** If either leg's `amount_minor` / `hold` / `time` changed after the tombstone (fingerprint mismatch — e.g. a hold cleared to a different amount via the frozen-by-omission upsert), the tombstone is stale and the pair may be reconsidered. Otherwise it stays suppressed.
- **D-11:** **A re-surfaced (post-fresh-signal) pair always returns as a pending CANDIDATE, never a silent auto-pair.** The user already rejected it once; a changed leg lets it be reconsidered, but only with explicit re-confirmation.

### Netting Exposure to Phase 6 (REC-01, REC-02 — "excluded from spent" / "net to zero")
- **D-12:** **Netting is computed on read by joining the links table — no denormalized flags on `transactions`.** The links/tombstone tables are the single source of truth; this phase provides a reusable query/helper (or SQL view) that derives "excluded from spend" (transfers, both legs) and "net-adjusted amount" (refunds) by joining links at query time. Consistent with the Phase 3 compute-on-read convention; Phase 6 totals call this helper rather than reading a flag. No `excluded_from_spend` / `net_amount_minor` column written onto transaction rows.

### Claude's Discretion
- **Links / tombstone schema details:** table name(s), the link-type enum (`internal_transfer` / `refund`), status enum (`auto` / `confirmed` / `pending` / `rejected`-as-tombstone), column types, indexes, and exactly how the ordered-pair key + signal fingerprint are stored. (Researcher should verify against real Mono jar-transfer and refund `raw_payload` shapes first.)
- **Outer grace-band numbers (D-08 time soft-miss):** the concrete widths (e.g. transfers ±2d tight / up to ±4d candidate; refunds ±60d tight / up to ~±90d candidate). Planner proposes; intent is "just outside tight, not unbounded."
- **FX tolerance percentage (D-02):** the exact tolerance for mixed-currency candidate detection.
- **Reconciliation trigger & scope:** on-import incremental pass (after the categorizer step in `import_service`, per the Phase 4 D-11 seam pattern) vs. a manual "reconcile over history" endpoint — and whether a sweep needs a preview/commit + staleness-token handshake like `rules_history.py`. Mirror the categorizer's reuse-one-engine-in-both-paths shape (D-11). Roadmap locks "runs after categorization in the pipeline"; the manual-sweep surface is the planner's to design.
- **`GET /api/links/pending` / link response DTO shapes** and the confirm endpoint verb/path for promoting a candidate to a confirmed link.
- **Engine form** (pure function module vs small `Reconciler` class) — as long as it's reused by both the import-time pass and any history sweep.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & Roadmap
- `.planning/REQUIREMENTS.md` — REC-01, REC-02, REC-03 (internal transfers, refunds/reversals, idempotency-key dedup); note `V2-REC-01` (smart "looks like a transfer" prompt) is explicitly deferred to v2.
- `.planning/ROADMAP.md` §"Phase 5: Honest Totals" — goal, 4 success criteria, and Notes/Risks: **Pitfall 9** (≥3 signals to auto-pair, 2-signal candidates surface, never silent auto-hide), **Pitfall 28** (refunds must require overlapping counterparty/MCC, not amount+window alone), **Pitfall 22** (card↔jar transfers fire on both sides with mirrored signs; caught via same-user-owned signal).
- `CLAUDE.md` — Money/Decimal handling (`amount_minor` integer minor units; all comparisons are integer ops, never floats), single-user / network-gated / no-auth constraints, "visibility not planning" scope discipline.

### Existing code this phase builds on
- `src/finance_bro/db/models.py` §`Transaction` (lines 46-83) — `account_id`, `amount_minor`, `currency`, `time`, `attributed_day`, `mcc`, `hold`, `is_deleted`, and `raw_payload` JSONB (carries counterparty IBAN/EDRPOU). The pairing signals read from these columns + `raw_payload`. No FK/link table exists yet — this phase adds it (migration `0005_*`).
- `src/finance_bro/services/import_service.py` — the pipeline; the reconciliation pass hooks in **after** the categorizer step (`apply_categories`), mirroring how Phase 4 added the categorizer step after `insert_many` (D-11 seam).
- `src/finance_bro/services/rules_history.py` — the preview→commit + **staleness-token** handshake (Phase 4 D-13); the analog if a manual reconciliation sweep needs a preview.
- `src/finance_bro/db/transaction_repo.py` — `insert_many` **frozen-by-omission** ON CONFLICT upsert (re-imports never clobber added columns — the DB-level backing for tombstone/link persistence surviving re-import, exactly as it protects category columns); `list_for_account` LATERAL read shape to extend with link/netting data.
- `src/finance_bro/services/fx_rollup.py` + the Phase 3 LATERAL `transactions × fx_rates` join — the UAH-normalization the D-02 mixed-currency candidate path reuses.
- `src/finance_bro/db/account_repo.py` — "user-owned account" = any row in `accounts` (all from the single user's token); the D-07 transfer ownership gate is "both legs reference imported accounts."
- `src/finance_bro/api/routes_transactions.py`, `routes_rules.py`, `schemas.py`, `deps.py` — router/DTO patterns for the new `routes_links.py` (`GET /api/links/pending`, confirm, `DELETE …/link/…`).
- `alembic/versions/0004_categorized_spending.py` — most recent migration; this phase's migration is `0005_*` (links table + tombstone/suppression table).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Frozen-by-omission upsert** (`transaction_repo.insert_many`): excludes added columns from the ON CONFLICT SET, so re-imports cannot clobber link/tombstone state — same protection categories got in Phase 4. Link/tombstone tables key off transaction ids that the upsert preserves.
- **Reuse-one-engine-in-both-paths** (Phase 4 D-11, `categorizer/engine.py` + `rules_history.py`): the template for a reconciliation engine that runs on import AND via a manual sweep, independently testable.
- **Preview→commit + staleness token** (`rules_history.py`, Phase 4 D-13): available analog if a reconciliation history sweep needs a confirm step.
- **FX rollup LATERAL join** (`fx_rollup.py`, Phase 3): gives a UAH-normalized amount for any transaction — the D-02 mixed-currency candidate comparison basis.
- **Repo + ON CONFLICT seeding / migration pattern** (`fx_rate_repo`, `category_repo`, migrations `0003`/`0004`): the template for the new links + tombstone repos and migration `0005`.

### Established Patterns
- `text()` raw SQL accepted in repos for non-trivial reads; ORM for simple CRUD.
- **Compute-on-read** (Phase 3 FX, no denormalized `uah_amount`): D-12 follows this — netting is a query-time join, not a stored flag.
- `amount_minor` integer discipline — all signal comparisons (amount match, sign) are integer ops; FX-normalized comparison (D-02) is the only place a tolerance is involved, and only for candidates.
- Sequential migrations (`0001`→`0004`); seed/DDL via Alembic ops. Next is `0005_*`.

### Integration Points
- `import_service` post-categorizer → new reconciliation pass (mirrors D-11).
- New migration `0005_*` — links table (`internal_transfer` / `refund`, status, confidence, ordered-pair key) + tombstone/suppression table (ordered-pair key + signal fingerprint).
- New `routes_links.py` mounted alongside existing routers; `DELETE /api/transactions/{id}/link/{link_id}` on the transactions router; `schemas.py` gains Link / PendingCandidate DTOs.
- `transaction_repo.list_for_account` / read path extended to expose linked-pair + net-adjusted data (consumed by Phase 6).

</code_context>

<specifics>
## Specific Ideas

- **Canonical transfer case to keep working end-to-end:** moving 5,000 UAH from card → jar produces two same-currency mirror legs (opposite sign, same `amount_minor`, both user-owned, within ±2d) → auto-pairs as `internal_transfer` at ≥0.8, both legs excluded from "this month spent." (Roadmap SC#1; Pitfall 22.)
- **Canonical false-positive to AVOID:** a salary deposit and an unrelated same-day same-amount expense must NOT auto-pair — the D-07 gates (ownership for transfers, counterparty/MCC for refunds) are what prevent this.
- **Canonical dedup-safety case:** re-importing an overlapping 7-day window when 6 days exist must NOT collapse two legitimate identical 50-UAH coffees from different cards — dedup stays `(account_id, source_tx_id)`-keyed (REC-03), entirely separate from the heuristic *pairing* logic here.
- **Refund net wording:** the original charge stays visible in transaction-detail with its linked refund; only spending *aggregations* net it out (full → 0, partial → remainder).

</specifics>

<deferred>
## Deferred Ideas

- **Cross-currency AUTO-pairing** — v1 only surfaces mixed-currency transfers as candidates (D-01/D-02). Auto-pairing FX-normalized legs is a future upgrade once real-data confidence in the tolerance is established.
- **Split / one-charge-many-refunds (many-to-one) matching** — D-03 ships full + partial *single* refunds only. Revisit if real refund data shows frequent split refunds.
- **Weighted confidence scoring** — D-06 ships signal-count buckets; a weighted/learned model is a v2 path once there's labeled data.
- **Smart "looks like a transfer" prompt for low-confidence pairs** (V2-REC-01) — explicitly v2; this phase's `GET /api/links/pending` surface is the API such a UI would consume.
- **Manual merge / split / cash entry and the visible spending UI** — Phase 6 (MAN-01..03, UI-01..05). This phase only exposes the netting helper/query they build on.

### Reviewed Todos (not folded)
None — no pending todos matched this phase.

</deferred>

---

*Phase: 05-honest-totals*
*Context gathered: 2026-05-31*
