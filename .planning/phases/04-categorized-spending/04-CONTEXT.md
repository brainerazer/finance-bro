# Phase 4: Categorized Spending - Context

**Gathered:** 2026-05-30
**Status:** Ready for planning

<domain>
## Phase Boundary

A rules-driven categorization engine — backend/API only (`UI hint: no`). Delivers:
- A default ~15-category taxonomy seeded by migration, fully user-editable afterward.
- A composable-predicate rules engine (priority-ordered, first-match-wins) that assigns a category to a transaction.
- MCC→category coverage shipped as **pre-seeded editable rules**, not hardcoded mappings — the rules engine is the single categorization mechanism.
- Category CRUD (`POST`/`PATCH`/`DELETE /api/categories`) and rule CRUD (`POST /api/rules`, priority reorder).
- Auto-categorization of newly-imported, non-locked rows on each import tick.
- A "run rules over history" path with a diff preview before commit (CAT-05).

The one hard invariant: **`is_user_locked` rows are never touched by any categorizer re-run.**

Out of phase (other phases own these): the dashboard/feed UI (Phase 6), transfer/refund netting (Phase 5), the LLM categorizer (v2), regex predicates (deferred).

</domain>

<decisions>
## Implementation Decisions

### Taxonomy & Categories (CAT-03)
- **D-01:** Ship a **fixed ~15-category default taxonomy** seeded by migration (e.g. Groceries, Cafe/Restaurants, Transport, Fuel, Utilities, Entertainment, Health, Shopping, Income, etc.). Fully editable after seed — rename/recolor/add/delete via the API. No special-cased categories.
- **D-02:** Uncategorized transactions keep `category_id = NULL`. **NULL only** — the API does not add an `is_categorized` flag or a sentinel "Uncategorized" row; a null `category_id` *is* the uncategorized signal. Unmatched rows are never silently bucketed into a real category.
- **D-03:** Create a real `categories` table; add a **FK from `transactions.category_id` → `categories.id`** (the column exists today as a bare `BigInteger` with no FK — Phase 4 adds the constraint). Likewise rules reference categories by FK.

### MCC Seed (CAT-01, CAT-03; Pitfall 21)
- **D-04:** MCC coverage ships as a **curated set of pre-seeded RULES** (not a hardcoded MCC→category map). E.g. `mcc IN [5411,5412] → Groceries`, `mcc IN [5812,5814] → Cafe/Restaurants`, `mcc IN [4121,4111] → Transport`, `mcc IN [5541,5542] → Fuel`, utilities, etc. These are ordinary editable/deletable rules, just present on first boot. This makes the rules engine the *only* categorization path and lets the user override MCC defaults per Pitfall 21.

### Predicate Model & Rule Shape (CAT-01, CAT-02; Pitfall 8 / Anti-pattern 8)
- **D-05:** **Substring/equality ops only — NO regex in v1.** Fixed op vocabulary over a structured-JSON predicate: `ICONTAINS` / `EQUALS` (description, comment, counterparty IBAN/EDRPOU), `IN` (mcc, originalMcc, account, currency), amount sign + range (`amount_minor < 0`, ranges), `hold` boolean. This eliminates ReDoS entirely and covers the canonical example (`mcc IN [5411,5499] AND amount_minor < 0 AND description ICONTAINS "ATB"`). Predicates are evaluated by a fixed interpreter over a closed op set — **never `eval()` of an expression string.**
- **D-06:** **Flat AND-only combination.** A rule is a list of conditions; all must match. Set-membership (`IN [...]`) handles the common "OR" case. For true OR across different fields, the user writes two rules. No nested AND/OR tree in v1.
- **D-07:** **Rule action = category only.** A matching rule sets `category_id` and stamps `category_source = 'rule'`. Rules do not set notes/description/tags (keeps the frozen-by-omission importer story clean and stays in CAT-01/CAT-02 scope).
- **D-08:** Predicate fields that live in `raw_payload` (JSONB) — `originalMcc`, counterparty `IBAN`/`EDRPOU`, `comment` — are read out of `raw_payload` at evaluation time. `mcc`, `amount_minor`, `currency`, `hold`, `account_id` are first-class transaction columns.

### Lock Semantics (CAT-04; Pitfall 10)
- **D-09:** A **manual recategorize sets both** `category_source = 'manual'` **and** `is_user_locked = true`. Rule runs (auto or history) **skip `is_user_locked = true` rows unconditionally.** Rule-categorized rows (`category_source = 'rule'`, unlocked) **remain re-evaluable** by later runs. This is the exact CAT-04 / Pitfall-10 contract and is already backed at the DB level by the importer's frozen-by-omission upsert (Phases 1/3 leave `category_id`/`category_source`/`is_user_locked` out of the ON CONFLICT SET clause).

### Auto-Run Trigger & Integration (CAT-01, zero-upkeep core value)
- **D-10:** Rules **auto-run on import**, against newly-touched **non-locked** rows only, so new transactions arrive already categorized. Creating or editing a rule does **NOT** auto-apply it to history — that goes through the CAT-05 preview path. (Explicit-only and auto-apply-on-edit were both rejected.)
- **D-11:** **Integration point: a distinct categorizer step in `import_service`, called after `TransactionRepo.insert_many` returns** (categorize the rows it just inserted/updated, skipping locked). The repo stays a pure data layer; the engine is independently testable and **reused verbatim** by the CAT-05 history sweep. This also establishes the `Categorizer` seam for the v2 LLM categorizer.

### Run-Over-History Diff & Commit (CAT-05)
- **D-12:** Preview endpoint returns **summary counts AND the per-row change list** (tx id, old category → new category) for rows that would change, plus a **skipped-locked count**. One API shape serves both summary and a future review UI. (No cap/sample in v1 — single-user histories are small; revisit if payloads grow.)
- **D-13:** **Preview→commit handshake = stateless re-run + staleness token.** Preview computes the diff fresh and returns a token (hash of current rules + matched-row state). Commit re-runs the same computation; if the token still matches it applies, otherwise it returns a "stale — re-preview" error. No persisted diff-job table.
- **D-14:** **Overwrite scope = all non-locked rows.** A history run re-evaluates every non-locked row: NULL-category rows get categorized, and rule-categorized rows change if the rules changed (these are the "will be overwritten" count). Locked rows always skipped. No separate uncategorized-only/opt-in mode.
- **D-15:** **Category delete = block if referenced (`ON DELETE RESTRICT` + pre-check).** Deleting a category referenced by any rule or transaction fails with a clear `409` listing what references it (N rules, M transactions). The user must reassign/clear references first. No cascade-to-uncategorized, no soft-delete/archive in v1.

### Claude's Discretion
- Exact final list and naming of the ~15 default categories, their colors, and the precise MCC ranges in each seed rule (use Ukrainian-context groupings; the planner/researcher proposes the concrete table).
- Rule/category table column details, indexes, and the predicate JSON's exact field names/schema (must encode the closed op vocabulary from D-05/D-06).
- Endpoint paths/verbs beyond those named, pagination of rule lists, and how rule priority is represented (integer order column vs explicit reorder endpoint).
- The staleness-token hashing scheme (D-13) and how "matched-row state" is captured.
- Whether the categorizer engine is a pure function module or a small `Categorizer` class — as long as it's reused by both the import step and the history sweep (D-11).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & Roadmap
- `.planning/REQUIREMENTS.md` — CAT-01..CAT-05 (lines 30-34); also MAN-03 (manual edits flagged so re-runs can't overwrite) and the v2 deferred CAT items (V2-CAT-01 auto-rule suggestion, V2-CAT-02 LLM categorizer via `Categorizer` port).
- `.planning/ROADMAP.md` §"Phase 4: Categorized Spending" — goal, 5 success criteria, and the Notes/Risks (Pitfall 10 manual-edit clobber, Pitfall 21 MCC long tail, Pitfall 8 / Anti-pattern 8 eval-based predicates).
- `CLAUDE.md` — Money/Decimal handling (amount_minor is integer minor units; predicates over it are integer comparisons), single-user / network-gated / no-auth constraints, Python+FastAPI+SQLAlchemy+Alembic stack.

### Existing code this phase builds on
- `src/finance_bro/db/models.py` §`Transaction` (lines 46-83) — `category_id` (BigInteger, **no FK yet** — add one), `category_source`, `is_user_locked`, `mcc` columns already exist; `raw_payload` JSONB carries originalMcc / counterparty / comment.
- `src/finance_bro/db/transaction_repo.py` — `insert_many` frozen-by-omission upsert (the DB-level lock-protection backing D-09); `list_for_account` LATERAL read shape to extend with category data.
- `src/finance_bro/services/import_service.py` — the service the D-11 categorizer step hooks into, after `insert_many`.
- `alembic/versions/0003_fx_truth.py` — most recent migration; Phase 4's migration is `0004_*` (create `categories` + `rules` tables, FK on `transactions.category_id`, seed taxonomy + MCC rules).
- `src/finance_bro/db/fx_rate_repo.py` / `tracked_fx_currency_repo.py` — repo + seeding patterns to mirror for `categories`/`rules`.
- `src/finance_bro/api/` (`routes_transactions.py`, `schemas.py`, `deps.py`) — router/DTO patterns for the new `routes_categories.py` / `routes_rules.py`.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Frozen-by-omission upsert** (`transaction_repo.insert_many`): already excludes `category_id`/`category_source`/`is_user_locked` from the ON CONFLICT SET, so the importer can never clobber categorization — D-09's lock invariant has DB-level backing for free.
- **Repo + ON CONFLICT seeding pattern** (`fx_rate_repo`, `tracked_fx_currency_repo`, migration `0003`): the template for `categories`/`rules` repos and the migration-time taxonomy + MCC-rule seed.
- **Service-step composition** (`import_service` calling repos, HTTP outside session): the shape for adding the post-insert categorizer step (D-11).
- **LATERAL read** (`transaction_repo.list_for_account`): extend to join category data into the transactions response.

### Established Patterns
- `text()` raw SQL is accepted in repos for non-trivial reads; ORM for simple CRUD.
- `Decimal`/minor-units discipline — predicates compare `amount_minor` as integers, never floats.
- Migrations are sequential (`0001`→`0004`); seed data via `op.execute("INSERT ...")` (see `0002` scheduler_state seed, `0003` USD/EUR seed).

### Integration Points
- `import_service` post-`insert_many` → new categorizer step (D-11).
- New migration `0004_*` adds `categories` + `rules` tables and the `transactions.category_id` FK.
- New routers `routes_categories.py` + `routes_rules.py` mounted alongside existing routers; `schemas.py` gains Category/Rule/diff-preview DTOs.

</code_context>

<specifics>
## Specific Ideas

- Canonical rule example to keep working end-to-end: `mcc IN [5411, 5499] AND amount_minor < 0 AND description ICONTAINS "ATB"` → Groceries.
- Diff-preview UX wording target from the roadmap: "47 transactions will change category, 3 will be overwritten" — the response must carry the data to render exactly that (changed count, overwritten count, skipped-locked count, per-row old→new).
- Ukrainian-merchant context for the MCC seed rules (ATB/Silpo groceries, Uklon/Bolt transport, etc.) — informs the default rule set, but merchant-name rules are user-authored, not seeded.

</specifics>

<deferred>
## Deferred Ideas

- **Regex predicates** — CAT-01's literal wording says "merchant substring/regex", but D-05 ships substring/equality only in v1 (Pitfall 8). A future RE2/linear-time-regex op is the deferred upgrade path; document the narrowing in the plan.
- **Nested AND/OR predicate trees** (D-06 ships flat AND-only) — revisit only if real rules need cross-field OR beyond `IN` lists.
- **Capped/sampled diff payloads** (D-12 returns full per-row) — add truncation only if single-user histories ever make the payload large.
- **Auto-rule suggestion from manual overrides** (V2-CAT-01) and **LLM categorizer via `Categorizer` port** (V2-CAT-02) — explicitly v2; D-11's engine seam is designed so the LLM categorizer can slot in behind the same interface.
- **Quick re-categorize from the feed** (UI-03) and **detail drawer showing matched rule** (UI-04) — Phase 6 UI work; this phase exposes the API they'll consume.

</deferred>

---

*Phase: 04-categorized-spending*
*Context gathered: 2026-05-30*
