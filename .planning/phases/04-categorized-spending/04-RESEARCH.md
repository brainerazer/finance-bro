# Phase 4: Categorized Spending - Research

**Researched:** 2026-05-30
**Domain:** Rules-driven transaction categorization engine (backend/API only)
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Ship a **fixed ~15-category default taxonomy** seeded by migration. Fully editable after seed (rename/recolor/add/delete via API). No special-cased categories.
- **D-02:** Uncategorized transactions keep `category_id = NULL`. **NULL only** — no `is_categorized` flag, no sentinel "Uncategorized" row. A null `category_id` *is* the uncategorized signal. Unmatched rows are never silently bucketed.
- **D-03:** Create a real `categories` table; add a **FK from `transactions.category_id` → `categories.id`** (column exists today as bare `BigInteger`, no FK). Rules reference categories by FK.
- **D-04:** MCC coverage ships as a **curated set of pre-seeded RULES** (not a hardcoded MCC→category map). Ordinary editable/deletable rules, present on first boot. The rules engine is the *only* categorization path.
- **D-05:** **Substring/equality ops only — NO regex in v1.** Fixed op vocabulary: `ICONTAINS`/`EQUALS` (description, comment, counterparty IBAN/EDRPOU), `IN` (mcc, originalMcc, account, currency), amount sign+range, `hold` boolean. Predicates evaluated by a fixed interpreter over a closed op set — **never `eval()`.**
- **D-06:** **Flat AND-only combination.** A rule is a list of conditions; all must match. `IN [...]` handles the common OR case. For cross-field OR, the user writes two rules. No nested AND/OR tree in v1.
- **D-07:** **Rule action = category only.** A matching rule sets `category_id` and stamps `category_source = 'rule'`. Rules do NOT set notes/description/tags.
- **D-08:** Predicate fields in `raw_payload` (JSONB) — `originalMcc`, counterparty `IBAN`/`EDRPOU`, `comment` — read out of `raw_payload` at eval time. `mcc`, `amount_minor`, `currency`, `hold`, `account_id` are first-class columns.
- **D-09:** A **manual recategorize sets both** `category_source = 'manual'` **and** `is_user_locked = true`. Rule runs (auto or history) **skip `is_user_locked = true` rows unconditionally.** Rule-categorized unlocked rows (`category_source = 'rule'`) **remain re-evaluable**. Backed at DB level by the importer's frozen-by-omission upsert.
- **D-10:** Rules **auto-run on import**, against newly-touched **non-locked** rows only. Creating/editing a rule does **NOT** auto-apply to history — that goes through the CAT-05 preview path.
- **D-11:** **Integration point: a distinct categorizer step in `import_service`, called after `TransactionRepo.insert_many` returns** (categorize the rows it just inserted/updated, skipping locked). Repo stays a pure data layer; engine is independently testable and **reused verbatim** by the CAT-05 history sweep. Establishes the `Categorizer` seam for the v2 LLM categorizer.
- **D-12:** Preview endpoint returns **summary counts AND the per-row change list** (tx id, old→new category) plus a **skipped-locked count**. One API shape serves summary + future review UI. No cap/sample in v1.
- **D-13:** **Preview→commit handshake = stateless re-run + staleness token.** Preview computes the diff fresh + returns a token (hash of current rules + matched-row state). Commit re-runs; if token matches it applies, else returns a "stale — re-preview" error. No persisted diff-job table.
- **D-14:** **Overwrite scope = all non-locked rows.** A history run re-evaluates every non-locked row: NULL-category rows get categorized; rule-categorized rows change if rules changed. Locked rows always skipped. No uncategorized-only/opt-in mode.
- **D-15:** **Category delete = block if referenced (`ON DELETE RESTRICT` + pre-check).** Deleting a referenced category fails with `409` listing what references it (N rules, M transactions). No cascade-to-uncategorized, no soft-delete in v1.

### Claude's Discretion
- Exact final list/naming of the ~15 default categories, colors, and the precise MCC ranges per seed rule (Ukrainian-context groupings).
- Rule/category table column details, indexes, and the predicate JSON's exact field names/schema (must encode the D-05/D-06 closed op vocabulary).
- Endpoint paths/verbs beyond those named, rule-list pagination, and how rule priority is represented (integer order column vs reorder endpoint).
- The staleness-token hashing scheme (D-13) and how "matched-row state" is captured.
- Whether the categorizer engine is a pure-function module or a small `Categorizer` class — as long as it's reused by both the import step and the history sweep (D-11).

### Deferred Ideas (OUT OF SCOPE)
- **Regex predicates** (deferred — Pitfall 8; future RE2/linear-time-regex op).
- **Nested AND/OR predicate trees** (D-06 ships flat AND-only).
- **Capped/sampled diff payloads** (D-12 returns full per-row).
- **Auto-rule suggestion from manual overrides** (V2-CAT-01) and **LLM categorizer via `Categorizer` port** (V2-CAT-02).
- **Quick re-categorize from feed** (UI-03) and **detail drawer showing matched rule** (UI-04) — Phase 6 UI.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CAT-01 | Rules engine with composable predicates (merchant substring/regex, mcc/originalMcc, amount sign/range, account, currency, counterparty IBAN/EDRPOU, comment, hold flag) | Predicate AST (§Pattern 1) + closed-op interpreter (§Pattern 2). Regex narrowed to substring/equality in v1 per D-05; documented as deferred. |
| CAT-02 | User-controlled rule priority list; first-match-wins | Integer `priority` column + first-match-wins evaluation loop (§Pattern 3). Reorder via PATCH (§Pattern 6). |
| CAT-03 | Default ~15-category taxonomy seeded from MCC groups; user-editable categories | `categories` table + seeded taxonomy (§Standard Stack / §Default Taxonomy) + MCC seed-rules (§Default MCC Rules). Category CRUD (§Pattern 6). |
| CAT-04 | `category_source` + `is_user_locked` from day one; locked rows skipped by every re-run | Columns exist (models.py:63-67). Engine skips `is_user_locked` unconditionally (§Pattern 3, §Pitfall 1). Frozen-by-omission upsert backs it (§Don't Hand-Roll). |
| CAT-05 | Run-rules-on-history with diff preview before commit | Stateless re-run + staleness-token handshake (§Pattern 4). Preview/commit endpoints (§Pattern 5). |
</phase_requirements>

## Summary

This phase is a self-contained backend slice over an already-mature FastAPI + SQLAlchemy 2.0 (async, psycopg3) + Alembic + Postgres 17 codebase. Every architectural seam it needs already exists: the `transactions` table already carries `category_id`, `category_source`, `is_user_locked`, `mcc`, and `raw_payload` (JSONB); the frozen-by-omission upsert in `TransactionRepo.insert_many` already protects those columns from importer clobber (the DB-level backing for D-09); the repo+seed migration pattern (`fx_rate_repo`/`tracked_fx_currency_repo` + migration 0003) is the exact template for `categories`/`rules`; and `import_service.run_one_card` has an obvious post-`insert_many` hook point for D-11. There are **no new external packages** — this is built entirely from the existing stack.

The single non-trivial design problem is the **predicate model and its interpreter**. The locked decisions (D-05/D-06/D-08) define a deliberately tiny, closed problem: a flat AND-list of typed conditions over a fixed op vocabulary (`ICONTAINS`, `EQUALS`, `IN`, amount sign+range, `hold` bool), with field values drawn from either first-class columns or `raw_payload` JSON. This is modeled as a Pydantic-discriminated-union AST and evaluated by a `match`-statement interpreter in Python — no `eval()`, no regex, no dynamic dispatch. This eliminates ReDoS and arbitrary-code-execution risk entirely (Pitfall 8 / Anti-pattern 8). The engine is a pure module reused verbatim by both the import-time step and the CAT-05 history sweep.

The second design problem is the **CAT-05 preview→commit handshake** (D-13). Recommended scheme: a stateless re-run that hashes `(ordered rule set, sorted matched-row (id, old_category_id) tuples)` into a hex token; commit recomputes the same hash and applies only on match, else `409`. This needs no diff-job table and is naturally correct under single-user concurrency.

**Primary recommendation:** Add migration `0004` (categories + rules tables, FK on `transactions.category_id` ON DELETE RESTRICT, seed taxonomy + MCC rules via `op.execute`). Build a pure `categorizer` package (Pydantic predicate AST + `match`-based interpreter + first-match-wins engine), reused by a new post-`insert_many` step in `import_service` and by a `RulesHistoryService`. Add `routes_categories.py` and `routes_rules.py` mirroring existing router/DTO idioms. No new dependencies.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Predicate evaluation (op interpreter) | Pure Python module (`categorizer/`) | — | No I/O, no DB, no session; must be unit-testable in isolation and reused verbatim by import-step and history-sweep (D-11). |
| First-match-wins rule application | Pure Python module (`categorizer/`) | DB (rule fetch) | Engine is pure over an in-memory rule list; the *fetch* of rules is the DB boundary. |
| Category/Rule persistence + CRUD | DB / Repo (`categories_repo`, `rules_repo`) | API (routers) | Mirrors existing repo-owns-SQL pattern (`fx_rate_repo`). |
| Auto-categorize on import | Service (`import_service` step) | Pure engine + repo | D-11: a distinct service step calls the pure engine on rows the repo just returned, then writes back. |
| Run-over-history preview/commit | Service (`RulesHistoryService`) | Pure engine + repo | Stateless re-run; token handshake is service-layer orchestration over the same pure engine. |
| Predicate field resolution (column vs JSONB) | Pure Python (field-resolver) | — | D-08: `originalMcc`/IBAN/EDRPOU/`comment` read from `raw_payload`; rest from columns. Pure mapping function. |
| Category-delete referential guard | DB (FK RESTRICT) | API (pre-check → 409) | D-15: DB enforces; API pre-checks to produce a friendly 409 with reference counts. |

## Standard Stack

### Core
No new libraries. Phase 4 is built entirely from the installed stack.

| Library | Version (installed) | Purpose | Why Standard |
|---------|---------------------|---------|--------------|
| SQLAlchemy | 2.0.x | `categories`/`rules` ORM models + repos | Already the project's ORM; `JSONB` predicate column, `ForeignKey(..., ondelete="RESTRICT")` (D-03/D-15). `[CITED: models.py imports]` |
| Alembic | 1.18.x | Migration `0004` (tables + FK + seed) | Sequential migration chain `0001→0003`; `op.execute("INSERT ...")` seed idiom from 0003. `[VERIFIED: alembic/versions/0003_fx_truth.py]` |
| Pydantic | 2.13.x | Predicate AST (discriminated union) + request/response DTOs | Already used for all DTOs (`schemas.py`); v2 discriminated unions are the safe, declarative way to model the closed predicate op set. `[CITED: api/schemas.py]` |
| psycopg | 3.3.x (async) | Postgres driver | Existing; `postgresql+psycopg://`. No change. |
| FastAPI | 0.136.x | `routes_categories.py` + `routes_rules.py` + history endpoints | Existing router idiom (`routes_transactions.py`, `routes_backfill.py`). `[CITED: api/routes_*.py]` |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| stdlib `hashlib` | — | Staleness-token hash (D-13) | `sha256` over a canonical serialization of `(rules, matched-row state)`. No dependency. |
| stdlib `json` | — | Canonical (sorted-key) serialization for the token | `json.dumps(..., sort_keys=True, separators=...)` for stable hashing. |
| Pydantic `Field(discriminator=...)` | 2.13.x | Tagged-union predicate condition decode | Maps the JSON `op` tag to a concrete condition model without `eval`. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Pydantic discriminated-union AST | Hand-rolled `dict` walking + isinstance checks | Loses schema validation at the API boundary; a malformed predicate would reach the interpreter instead of being rejected at request parse. Pydantic is already a dependency — use it. |
| Integer `priority` column | Linked-list / fractional ranking | Single-user, small rule count — a plain integer with a reorder endpoint is simplest. Fractional ranking solves a concurrency problem that doesn't exist here. |
| `match`-statement interpreter | Visitor pattern / strategy registry | For ~6 ops a `match` on the discriminated union is clearer and equally safe. Revisit only if op count grows large. |

**Installation:** None required. (`uv sync` already provides everything.)

**Version verification:** All libraries are already pinned in `pyproject.toml`/`uv.lock` and exercised by the existing test suite (Phases 1–3 green). No registry lookup needed — nothing new is added.

## Package Legitimacy Audit

**Not applicable — this phase installs zero external packages.** All capabilities use the already-installed, already-locked stack (SQLAlchemy, Alembic, Pydantic, FastAPI, psycopg) plus Python stdlib (`hashlib`, `json`, `decimal`). No `npm install` / `pip install` / `uv add` occurs in Phase 4. The slopcheck gate is vacuously satisfied (empty package set).

## Architecture Patterns

### System Architecture Diagram

```
                         ┌──────────────────────────────────────────────┐
                         │            categorizer/ (PURE)               │
                         │  predicate AST  ─►  interpreter (match)      │
                         │  field-resolver (column | raw_payload[..])   │
                         │  engine: first-match-wins over priority list │
                         └───────────────▲──────────────▲───────────────┘
                                         │ reused        │ reused
                                         │ verbatim      │ verbatim
   IMPORT PATH (D-10/D-11)               │               │   HISTORY PATH (CAT-05/D-12..D-14)
   ┌──────────────────────────┐         │               │   ┌──────────────────────────────────┐
   │ import_service.run_one_  │         │               │   │ POST /api/rules/run/preview        │
   │   card()                 │         │               │   │   → RulesHistoryService.preview()  │
   │  1. importer.fetch       │         │               │   │     fetch non-locked rows + rules  │
   │  2. repo.insert_many ────┼─► (ids) │               │   │     run engine → diff + token      │
   │  3. categorize_step ─────┼─────────┘               │   │   returns {summary, changes[],     │
   │     (skip locked, write  │                         │   │            skipped_locked, token}  │
   │      category_id+source) │                         └───┤ POST /api/rules/run/commit {token} │
   └──────────────────────────┘                             │   re-run → recompute token         │
                                                            │   match? apply : 409 stale          │
   CRUD                                                     └──────────────────────────────────┘
   ┌─────────────────────────┐   ┌──────────────────────────┐
   │ routes_categories.py    │   │ routes_rules.py          │
   │  POST/PATCH/DELETE      │   │  POST/PATCH/DELETE/list  │
   │  delete → 409 if refd   │   │  PATCH priority (reorder)│
   └──────────▲──────────────┘   └──────────▲───────────────┘
              │ repo                          │ repo
       ┌──────┴───────┐               ┌───────┴────────┐
       │ categories   │  FK RESTRICT  │ rules          │
       │ (id,name,…)  │◄──────────────│ (id,priority,  │
       └──────────────┘   category_id │  predicate JSONB)
              ▲                        └────────────────┘
              │ FK RESTRICT (transactions.category_id, D-03/D-15)
       ┌──────┴──────────────────────────────┐
       │ transactions (existing)             │
       │  category_id, category_source,      │
       │  is_user_locked, mcc, raw_payload   │
       └─────────────────────────────────────┘
```

Primary use case trace (auto-categorize): Mono statement → `fetch_statement` → `insert_many` returns touched ids → `categorize_step` loads active rules + the touched non-locked rows → pure engine runs first-match-wins per row → writes `category_id` + `category_source='rule'` back (NULL if no rule matched, D-02).

### Recommended Project Structure
```
src/finance_bro/
├── categorizer/                 # NEW — pure, no DB/session imports
│   ├── __init__.py
│   ├── predicate.py             # Pydantic AST: Condition union + RulePredicate
│   ├── fields.py                # field-resolver: column vs raw_payload (D-08)
│   ├── interpreter.py           # match-based op evaluation (D-05) — NO eval/regex
│   └── engine.py                # categorize_row / categorize_rows (first-match-wins)
├── db/
│   ├── models.py                # +Category, +Rule; +FK on Transaction.category_id
│   ├── category_repo.py         # NEW — CRUD + reference-count pre-check (D-15)
│   └── rule_repo.py             # NEW — CRUD, ordered list, priority reorder
├── services/
│   ├── import_service.py        # +categorize step after insert_many (D-11)
│   └── rules_history.py         # NEW — preview/commit + staleness token (D-13)
├── api/
│   ├── routes_categories.py     # NEW
│   ├── routes_rules.py          # NEW (incl. run/preview, run/commit)
│   └── schemas.py               # +Category/Rule/Predicate/Diff DTOs
alembic/versions/
└── 0004_categorized_spending.py # tables + FK + seed taxonomy + seed MCC rules
```

### Pattern 1: Pydantic Discriminated-Union Predicate AST (D-05/D-06/D-08)
**What:** Model the closed op vocabulary as a tagged union; a rule predicate is a flat AND-list of conditions.
**When to use:** Always — this is the request/storage shape for rules.
**Example:**
```python
# src/finance_bro/categorizer/predicate.py  [ASSUMED — schema is Claude's discretion per CONTEXT.md]
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

# --- field enums: column-backed vs raw_payload-backed (D-08) ---
TextField = Literal["description", "comment", "counter_iban", "counter_edrpou"]
IntSetField = Literal["mcc", "original_mcc", "account_id"]
StrSetField = Literal["currency"]

class IContains(BaseModel):
    op: Literal["icontains"] = "icontains"
    field: TextField
    value: str                       # case-insensitive substring; NOT a regex (D-05)

class Equals(BaseModel):
    op: Literal["equals"] = "equals"
    field: TextField | StrSetField
    value: str

class InInt(BaseModel):
    op: Literal["in_int"] = "in_int"
    field: IntSetField
    values: list[int]                # handles the common OR case (D-06)

class InStr(BaseModel):
    op: Literal["in_str"] = "in_str"
    field: StrSetField
    values: list[str]

class AmountSign(BaseModel):
    op: Literal["amount_sign"] = "amount_sign"
    sign: Literal["debit", "credit"]   # debit => amount_minor < 0

class AmountRange(BaseModel):
    op: Literal["amount_range"] = "amount_range"
    # integer minor units only — never float (CLAUDE.md §Money)
    min_minor: int | None = None       # inclusive
    max_minor: int | None = None       # inclusive

class HoldIs(BaseModel):
    op: Literal["hold_is"] = "hold_is"
    value: bool

Condition = Annotated[
    Union[IContains, Equals, InInt, InStr, AmountSign, AmountRange, HoldIs],
    Field(discriminator="op"),
]

class RulePredicate(BaseModel):
    # flat AND-only (D-06): all conditions must match
    all: list[Condition] = Field(min_length=1)
```

### Pattern 2: `match`-Based Interpreter — Closed Op Set, No `eval`/Regex (D-05, Pitfall 8)
**What:** Evaluate one condition against a resolved field value via a `match` on the discriminated union.
**When to use:** The single evaluation primitive; all matching flows through it.
**Example:**
```python
# src/finance_bro/categorizer/interpreter.py  [ASSUMED — Claude's discretion]
def eval_condition(cond: Condition, row: "RowView") -> bool:
    match cond:
        case IContains():
            v = row.text(cond.field)
            return v is not None and cond.value.casefold() in v.casefold()
        case Equals():
            v = row.text(cond.field)
            return v is not None and v == cond.value
        case InInt():
            v = row.int_field(cond.field)
            return v is not None and v in set(cond.values)
        case InStr():
            v = row.str_field(cond.field)
            return v is not None and v in set(cond.values)
        case AmountSign():
            return (row.amount_minor < 0) if cond.sign == "debit" else (row.amount_minor > 0)
        case AmountRange():
            a = row.amount_minor
            return (cond.min_minor is None or a >= cond.min_minor) and \
                   (cond.max_minor is None or a <= cond.max_minor)
        case HoldIs():
            return row.hold == cond.value
    # exhaustiveness: a new op without a case is a typing error, never a silent True
```
**Critical:** there is no string→code path anywhere. The `op` tag selects a pre-written branch; values are only ever compared, never executed.

### Pattern 3: First-Match-Wins Engine + Unconditional Lock Skip (CAT-02/CAT-04/D-09)
**What:** Iterate rules in `priority ASC`; the first rule whose predicate fully matches wins; `is_user_locked` rows are skipped before any rule is considered.
**Example:**
```python
# src/finance_bro/categorizer/engine.py  [ASSUMED — Claude's discretion]
def categorize_row(row: RowView, rules: list[CompiledRule]) -> int | None:
    # D-09 invariant — the engine NEVER touches a locked row. Caller must also
    # filter, but the engine refuses as defense-in-depth.
    if row.is_user_locked:
        return _SKIP  # sentinel distinct from "no match -> NULL"
    for rule in rules:                      # pre-sorted priority ASC
        if all(eval_condition(c, row) for c in rule.predicate.all):  # AND-only, D-06
            return rule.category_id          # first-match-wins (CAT-02)
    return None                              # no rule matched -> category_id NULL (D-02)
```
Sort once at fetch time (`ORDER BY priority ASC, id ASC`), not per row.

### Pattern 4: Field Resolver — column vs `raw_payload` (D-08)
**What:** A `RowView` adapter that reads `mcc`/`amount_minor`/`currency`/`hold`/`account_id` from columns and `originalMcc`/counterparty IBAN/EDRPOU/`comment` from `raw_payload`.
**Mono `raw_payload` field names** (verified against existing fixture + Mono docs): `originalMcc`, `comment`, `counterIban`, `counterEdrpou` (the last two are FOP-only and may be absent — resolver returns `None`). `[VERIFIED: tests/fixtures/statement_two_items.json]` `[CITED: api.monobank.ua/docs/index.html]`
```python
# fields.py  [ASSUMED — Claude's discretion]
_RAW_TEXT = {"comment": "comment", "counter_iban": "counterIban", "counter_edrpou": "counterEdrpou"}
def text(self, field: str) -> str | None:
    if field == "description": return self._description     # column
    return self._raw.get(_RAW_TEXT[field])                  # raw_payload (may be absent)
def int_field(self, field: str) -> int | None:
    if field == "mcc": return self._mcc                     # column
    if field == "account_id": return self._account_id       # column
    if field == "original_mcc":
        v = self._raw.get("originalMcc"); return int(v) if v is not None else None
```

### Pattern 5: CAT-05 Preview→Commit with Staleness Token (D-12/D-13/D-14)
**What:** Stateless re-run. Preview computes the diff over all non-locked rows and returns a token = `sha256` of `(ordered rules signature, sorted matched-row state)`. Commit re-runs, recomputes the token, applies iff it matches.
**Token recipe (recommended):**
```python
# services/rules_history.py  [ASSUMED — hashing scheme is Claude's discretion per D-13]
import hashlib, json
def compute_token(rules: list[CompiledRule], rows: list[RowView]) -> str:
    rules_sig = [(r.priority, r.category_id, r.predicate.model_dump(mode="json")) for r in rules]
    # "matched-row state" = the exact inputs that determine the diff: every
    # non-locked row's id + its CURRENT category_id. If a row changes category
    # (manual edit) or is locked between preview and commit, the token shifts.
    row_state = sorted((row.id, row.category_id) for row in rows)
    blob = json.dumps({"rules": rules_sig, "rows": row_state},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
```
**Preview response shape (D-12):**
```python
class CategoryChange(BaseModel):
    transaction_id: int
    old_category_id: int | None
    new_category_id: int | None
class RunPreviewOut(BaseModel):
    changed_count: int          # rows whose category differs
    overwritten_count: int      # subset where old_category_id is not None (rule→rule change)
    skipped_locked_count: int   # is_user_locked rows excluded (D-09)
    changes: list[CategoryChange]
    token: str
```
The roadmap UX string "47 transactions will change category, 3 will be overwritten" maps to `changed_count=47`, `overwritten_count=3`. Commit with a stale token → `409 {"detail": "stale", "message": "Rules or data changed; re-preview."}`.

### Pattern 6: Category/Rule CRUD + Priority Reorder + Delete Guard (CAT-03/D-15)
**What:** Mirror existing router idiom. Category delete pre-checks references and returns `409` with counts; the `ON DELETE RESTRICT` FK is the backstop.
```python
# category_repo.py  [ASSUMED — Claude's discretion]
async def reference_counts(self, category_id: int) -> tuple[int, int]:
    # (rules_referencing, transactions_referencing) — drives the 409 body (D-15)
    ...
# routes_categories.py
@router.delete("/api/categories/{cid}")
async def delete_category(cid: int, session=Depends(get_session)):
    rules_n, tx_n = await CategoryRepo(session).reference_counts(cid)
    if rules_n or tx_n:
        raise HTTPException(409, detail={"rules": rules_n, "transactions": tx_n})
    await CategoryRepo(session).delete(cid)
```
**Priority representation (recommended):** integer `priority` column, unique per row; expose a `PATCH /api/rules/{id}` accepting a new `priority`, plus an optional `PATCH /api/rules/reorder` taking an ordered id list that rewrites priorities in one transaction. Single-user → no contention; keep it simple.

### Anti-Patterns to Avoid
- **`eval()` / `exec()` of a predicate string** (Anti-pattern 8): arbitrary code execution. Use the closed-op AST + `match` interpreter.
- **Regex evaluation** (D-05): ReDoS risk; deferred. `ICONTAINS` is a plain `casefold` substring test.
- **Hardcoded MCC→category dict** (Pitfall 21): violates D-04. MCC coverage is *seeded rules*, fully editable.
- **Float comparisons on amounts**: `amount_minor` is `int`; all amount predicates are integer comparisons (CLAUDE.md §Money).
- **Letting the engine write locked rows**: D-09 — filter at the query AND refuse in the engine (defense-in-depth).
- **Persisting a diff-job table** for CAT-05 (D-13): use the stateless token instead.
- **Categorizer reaching into the DB session**: keep `categorizer/` pure so it is reused verbatim by both import-step and history-sweep (D-11) and unit-testable without Postgres.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Lock-protection on re-import | A "skip if locked" branch in the importer | Existing frozen-by-omission upsert | `insert_many` already omits `category_id`/`category_source`/`is_user_locked` from the ON CONFLICT SET — the importer *cannot* clobber them. `[VERIFIED: transaction_repo.py:115-122]` |
| Predicate parsing/validation | Manual dict traversal + type checks | Pydantic discriminated union | Rejects malformed predicates at the API boundary before they reach the interpreter. |
| Idempotent seed of taxonomy/rules | App-startup INSERT-if-missing | `op.execute("INSERT ...")` in migration 0004 | Matches 0003's USD/EUR seed; runs exactly once with the schema change. `[VERIFIED: 0003_fx_truth.py:70-73]` |
| Referential integrity on delete | App-only check | FK `ON DELETE RESTRICT` + app pre-check for the 409 body | DB guarantees correctness even if a code path forgets; pre-check supplies the friendly message (D-15). |
| Stable token hashing | Custom string concat | `hashlib.sha256` over `json.dumps(sort_keys=True)` | Canonical serialization → deterministic token across runs. |

**Key insight:** The hard correctness invariant of this phase (manual edits survive every re-run) is *already enforced at the database level* by Phase 2's upsert. Phase 4 only has to (a) not write locked rows in the categorizer, and (b) set both `category_source='manual'` and `is_user_locked=true` on manual recategorize. Nothing custom is required for the durability guarantee itself.

## Runtime State Inventory

> This is a greenfield additive phase (new tables, new code), not a rename/refactor/migration of existing runtime state. The only "migration" is additive DDL + idempotent seed data.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `transactions.category_id` exists as bare BigInteger with NO FK; currently all NULL (no categorization has run). Adding the FK is safe because no non-NULL values exist yet. | Add FK in 0004; no data backfill needed (NULL is valid under the new FK). Verify with `SELECT count(*) FROM transactions WHERE category_id IS NOT NULL` (expected 0) before adding the constraint — if non-zero, those ids must reference seeded categories or be NULLed first. |
| Live service config | None — categorization adds no external service config. | None. |
| OS-registered state | None — no schedulers/tasks reference categories. | None — verified by reading `main.py` lifespan (only Mono tick + FX cron jobs exist). |
| Secrets/env vars | None — no new secrets. | None. |
| Build artifacts | None — pure-Python additions; no package rename. | None. |

**Nothing found in OS/secrets/build categories:** confirmed by reading `main.py`, `deps.py`, and the alembic chain.

## Common Pitfalls

### Pitfall 1: A re-run silently overwrites a manual edit (Pitfall 10)
**What goes wrong:** The history sweep or import-step writes a category onto a row the user manually fixed.
**Why it happens:** Forgetting to filter `is_user_locked = true` in the row-fetch query, or fetching all rows and relying solely on the engine.
**How to avoid:** Filter `WHERE NOT is_user_locked` in *both* the import-step categorize query and the history-sweep query (D-09/D-14), AND have the engine return a `_SKIP` sentinel for locked rows (defense-in-depth). Manual recategorize must set both `category_source='manual'` and `is_user_locked=true`.
**Warning signs:** A test that locks a row, runs history, and asserts the category is unchanged fails. This is the headline CAT-04 test.

### Pitfall 2: `eval`/regex creeps into the predicate (Pitfall 8 / Anti-pattern 8)
**What goes wrong:** Someone adds a "just use regex for flexibility" op, reintroducing ReDoS / RCE surface.
**Why it happens:** CAT-01's literal wording mentions "regex"; D-05 deliberately narrows it.
**How to avoid:** The op vocabulary is closed in the Pydantic union; there is no string-eval branch anywhere. Document the narrowing in the plan; regex is a deferred v2 op (RE2/linear-time).
**Warning signs:** A grep for `re.compile`, `eval(`, `exec(` in `categorizer/` returns anything. Add a test asserting none exist.

### Pitfall 3: MCC hardcoded instead of seeded as rules (Pitfall 21)
**What goes wrong:** MCC→category becomes a frozen Python dict the user can't override.
**Why it happens:** It's the "obvious" implementation.
**How to avoid:** MCC coverage ships as ordinary `rules` rows seeded in migration 0004 (D-04). The rules engine is the *single* categorization path; MCC rules have no special status and are user-editable/deletable.
**Warning signs:** A `MCC_MAP = {...}` constant in code rather than INSERTs in the migration.

### Pitfall 4: Stale preview applied after data changed (D-13)
**What goes wrong:** User previews, then manually edits a row (or another rule edit lands), then commits an out-of-date diff.
**Why it happens:** Treating preview output as a frozen plan rather than recomputing.
**How to avoid:** Commit re-runs the full computation and compares the freshly-computed token to the submitted one; mismatch → `409`. The token must include current rule set AND current per-row category state.
**Warning signs:** A test that previews, mutates a row's category, then commits and asserts `409` fails.

### Pitfall 5: Absent `raw_payload` keys raise instead of not-matching (D-08)
**What goes wrong:** Reading `raw_payload["counterIban"]` on a non-FOP transaction (key absent) raises `KeyError`.
**Why it happens:** Counterparty IBAN/EDRPOU/comment are FOP-only and frequently missing on card transactions.
**How to avoid:** Field resolver uses `.get()` and returns `None`; a condition over a `None` field evaluates to `False` (no match), never an error.
**Warning signs:** A test feeding a card transaction (no counterparty fields) through a rule that references `counter_iban` raises instead of cleanly not-matching.

### Pitfall 6: Non-deterministic first-match under equal priorities (CAT-02)
**What goes wrong:** Two rules share a priority; which wins is undefined → flaky categorization.
**Why it happens:** No tiebreaker in the ORDER BY.
**How to avoid:** `ORDER BY priority ASC, id ASC` — deterministic total order. Consider a UNIQUE constraint on `priority` to forbid ties entirely (reorder endpoint rewrites the whole sequence).
**Warning signs:** Categorization output changes between runs with identical inputs.

## Code Examples

### Migration 0004 — tables + FK + seed (mirrors 0003 idiom)
```python
# alembic/versions/0004_categorized_spending.py  [VERIFIED pattern: 0003_fx_truth.py]
def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("color", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    op.create_table(
        "rules",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("category_id", sa.BigInteger,
                  sa.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("predicate", postgresql.JSONB, nullable=False),
        sa.Column("description", sa.Text, nullable=True),  # human label, not a predicate field
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("priority", name="uq_rules_priority"),
    )
    # D-03/D-15: FK on the pre-existing column. Safe: no non-NULL category_id yet.
    op.create_foreign_key(
        "fk_transactions_category", "transactions", "categories",
        ["category_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_rules_priority", "rules", ["priority"])
    # Seed taxonomy + MCC rules via op.execute INSERTs (idiom: 0003 USD/EUR seed).
    # ... INSERT INTO categories ...; INSERT INTO rules ... (predicate as JSON literal)
```

### Default Taxonomy (Claude's discretion — proposed, Ukrainian context) `[ASSUMED]`
| Category | Color (hex) | Notes |
|----------|-------------|-------|
| Groceries | `#22c55e` | ATB, Silpo, Novus, Auchan |
| Cafe & Restaurants | `#f97316` | |
| Transport | `#3b82f6` | Uklon, Bolt, metro, public transit |
| Fuel | `#eab308` | WOG, OKKO, gas stations |
| Utilities | `#06b6d4` | electricity/water/gas/internet |
| Health & Pharmacy | `#ef4444` | |
| Shopping | `#a855f7` | retail, electronics, clothing |
| Entertainment | `#ec4899` | cinema, streaming, games |
| Communications | `#14b8a6` | mobile, internet top-ups |
| Cash & ATM | `#64748b` | withdrawals |
| Fees & Commissions | `#71717a` | bank fees |
| Income | `#16a34a` | salary, transfers in |
| Transfers | `#94a3b8` | (placeholder; netting is Phase 5) |
| Travel | `#0ea5e9` | hotels, airlines |
| Other / Misc | `#9ca3af` | catch-all the user can rename |

(~15 categories. Final names/colors/count are Claude's discretion per CONTEXT.md; this is a starting proposal.)

### Default MCC Seed Rules (Claude's discretion — proposed) `[ASSUMED]`
| Predicate (debit + `mcc IN [...]`) | → Category | MCC rationale |
|------------------------------------|-----------|---------------|
| `[5411, 5412, 5422, 5499]` | Groceries | grocery/supermarket/food stores |
| `[5811, 5812, 5813, 5814]` | Cafe & Restaurants | eating/drinking places |
| `[4111, 4121, 4131, 4789]` | Transport | transit, taxi/limo, bus |
| `[5541, 5542]` | Fuel | service stations |
| `[4900]` | Utilities | utilities |
| `[5912, 8011, 8021, 8062]` | Health & Pharmacy | drug stores, doctors, hospitals |
| `[5311, 5651, 5732, 5999]` | Shopping | dept stores, apparel, electronics |
| `[7832, 7841, 5815]` | Entertainment | cinema, streaming/digital goods |
| `[4814, 4812]` | Communications | telecom |
| `[6011]` | Cash & ATM | ATM withdrawals |
| `[3501..3999, 7011]` (`IN` list) | Travel | lodging/airlines (enumerate, not range) |

The canonical end-to-end example to keep green: `mcc IN [5411, 5499] AND amount_minor < 0 AND description ICONTAINS "ATB" → Groceries` (CONTEXT specifics). Encode as `RulePredicate(all=[InInt(field="mcc", values=[5411,5499]), AmountSign(sign="debit"), IContains(field="description", value="ATB")])`. Note: amounts compared as integer minor units; `amount_minor < 0` == `AmountSign(debit)`. `[ASSUMED — exact MCC ranges are Claude's discretion]`

### import_service categorize step (D-11)
```python
# services/import_service.py (added after insert_many)  [ASSUMED — Claude's discretion]
async with self._session_factory() as session, session.begin():
    inserted, updated = await TransactionRepo(session).insert_many(card.id, items)
    # D-11: distinct categorizer step; categorizes the rows just touched, skipping locked.
    rules = await RuleRepo(session).list_active_ordered()          # priority ASC, id ASC
    rows = await TransactionRepo(session).fetch_for_categorize(    # NOT is_user_locked
        card.id, touched_source_tx_ids=[t.source_tx_id for t in items])
    updates = engine.categorize_rows(rows, rules)                  # pure
    await TransactionRepo(session).apply_categories(updates)       # set category_id + source='rule'
```
(`fetch_for_categorize` and `apply_categories` are new repo methods; the engine call is the same one the history sweep uses.)

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Rule engines via `eval`/string DSL | Structured, typed AST + closed interpreter | Long-standing security best practice | Eliminates RCE/ReDoS; D-05 codifies it. |
| Manual dict-walking predicate validation | Pydantic v2 discriminated unions | Pydantic 2.x | Declarative, validated-at-boundary, exhaustive. |
| Persisted diff-job tables for preview/commit | Stateless re-run + content-hash token | — | No job-state cleanup; correct under single-user. |

**Deprecated/outdated:** none relevant — this phase introduces nothing that supersedes prior project decisions.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Predicate AST field names (`icontains`, `in_int`, `amount_sign`, etc.) and JSON shape | Pattern 1 | Low — explicitly Claude's discretion (CONTEXT). Planner/implementer finalizes; no user lock needed. |
| A2 | Default taxonomy list, names, colors (~15) | Default Taxonomy | Low — Claude's discretion. User may rename post-seed anyway (D-01). |
| A3 | Exact MCC ranges in seed rules | Default MCC Rules | Low — Claude's discretion; rules are editable (D-04). Verify a few MCCs against Mono's actual data before shipping. |
| A4 | Staleness-token recipe (sha256 over rules+row-state) | Pattern 5 | Low — D-13 leaves scheme to discretion; any deterministic content-hash satisfies the contract. |
| A5 | Integer `priority` with UNIQUE + reorder endpoint | Pattern 6 | Low — Claude's discretion; alternative is fractional ranking (rejected as overkill). |
| A6 | `counterIban`/`counterEdrpou`/`comment` are FOP-only and often absent on card rows | Pattern 4 / Pitfall 5 | Low — confirmed by Mono docs; resolver `.get()` handles absence safely regardless. |

**Note:** Every `[ASSUMED]` item here falls inside an explicit "Claude's Discretion" area of CONTEXT.md, so none require user confirmation before planning — they are design choices the planner is empowered to lock.

## Open Questions (RESOLVED)

1. **Should `priority` be UNIQUE (forbidding ties) or just indexed?**
   - What we know: deterministic ordering needs `(priority, id)` tiebreak regardless.
   - What's unclear: whether the reorder UX prefers gapless unique priorities or sparse ones.
   - Recommendation: UNIQUE `priority`, reorder endpoint rewrites the sequence in one transaction. Simplest correct option for single-user.

2. **Does the import-step categorize only newly-touched rows, or all non-locked rows in the account?**
   - What we know: D-10/D-11 say "newly-touched non-locked rows."
   - What's unclear: whether `fetch_for_categorize` filters to the `source_tx_id`s just inserted/updated.
   - Recommendation: filter to touched ids on import (cheap, matches D-10); the full-account sweep is the CAT-05 history path. Implemented as above.

## Environment Availability

> Phase 4 is pure code + additive DDL against the already-running Postgres. No new external tools/services.

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL | new tables + FK + JSONB predicate | ✓ | 17 (compose `postgres:17-bookworm`) | — |
| Alembic | migration 0004 | ✓ | 1.18.x (chain 0001→0003 present) | — |
| Python stdlib `hashlib`/`json` | staleness token | ✓ | 3.13 | — |
| testcontainers Postgres | integration tests | ✓ | used by conftest | — |

**Missing dependencies:** none. (Step 2.6 effectively a no-op — additive code/DDL only.)

## Validation Architecture

> nyquist_validation enabled. This section derives VALIDATION.md.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9 + pytest-asyncio 1.3 (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` (`filterwarnings=["error"]` is active — unclosed clients/resources hard-fail) |
| Quick run command | `uv run pytest tests/test_categorizer_interpreter.py -x -q` (pure unit tests, no Postgres) |
| Full suite command | `uv run pytest -q` (spins testcontainers Postgres via `conftest.py`) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CAT-01 | Each op (icontains/equals/in_int/in_str/amount_sign/amount_range/hold) matches/not-matches correctly; canonical `mcc IN […] AND debit AND ICONTAINS "ATB"` example matches | unit (pure) | `uv run pytest tests/test_categorizer_interpreter.py -x` | ❌ Wave 0 |
| CAT-01 | No `eval`/`re.compile`/`exec` anywhere in `categorizer/` | unit (grep-style guard) | `uv run pytest tests/test_no_eval_in_categorizer.py -x` | ❌ Wave 0 |
| CAT-01 | Absent `raw_payload` counterparty key → no-match, not KeyError | unit (pure) | `uv run pytest tests/test_field_resolver.py -x` | ❌ Wave 0 |
| CAT-02 | First-match-wins; deterministic under `(priority, id)`; equal-priority forbidden | unit (pure) | `uv run pytest tests/test_engine_first_match.py -x` | ❌ Wave 0 |
| CAT-03 | Migration 0004 seeds ~15 categories + MCC rules; FK present; category CRUD round-trips | integration | `uv run pytest tests/test_categories_crud.py tests/test_migration_0004.py -x` | ❌ Wave 0 |
| CAT-04 | Locked row untouched by import-step AND history sweep; manual recategorize sets both flags | integration | `uv run pytest tests/test_lock_invariant.py -x` | ❌ Wave 0 |
| CAT-05 | Preview returns changed/overwritten/skipped counts + per-row diff + token; commit applies on token match, 409 on stale | integration | `uv run pytest tests/test_history_preview_commit.py -x` | ❌ Wave 0 |
| D-11 | Same engine output for import-step rows and history-sweep rows (reused verbatim) | integration | `uv run pytest tests/test_categorize_on_import.py -x` | ❌ Wave 0 |
| D-15 | DELETE category referenced by rule/tx → 409 with counts; FK RESTRICT backstop | integration | `uv run pytest tests/test_category_delete_guard.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** quick pure-unit run (`test_categorizer_interpreter.py`) — sub-second, no container.
- **Per wave merge:** full suite (`uv run pytest -q`) — includes testcontainers Postgres + migration + integration tests.
- **Phase gate:** full suite green before `/gsd:verify-work`; the CAT-04 lock-invariant test and the CAT-05 stale-token test are the two must-pass headline cases.

### Wave 0 Gaps
- [ ] `tests/test_categorizer_interpreter.py` — per-op truth table (CAT-01), pure, no fixtures.
- [ ] `tests/test_no_eval_in_categorizer.py` — static guard asserting no `eval`/`exec`/`re` import in `categorizer/`.
- [ ] `tests/test_field_resolver.py` — column vs raw_payload resolution + absent-key safety (D-08, Pitfall 5).
- [ ] `tests/test_engine_first_match.py` — first-match-wins + lock skip (CAT-02/D-09).
- [ ] `tests/test_migration_0004.py` — seed counts, FK existence, downgrade.
- [ ] `tests/test_categories_crud.py` / `tests/test_rules_crud.py` — CRUD + priority reorder.
- [ ] `tests/test_lock_invariant.py` — the headline CAT-04 test (lock → run history → unchanged).
- [ ] `tests/test_history_preview_commit.py` — preview shape + token + 409-on-stale (CAT-05/D-13).
- [ ] `tests/test_categorize_on_import.py` — import-step categorizes touched non-locked rows (D-10/D-11).
- [ ] `tests/test_category_delete_guard.py` — 409 with reference counts (D-15).
- [ ] Shared fixtures: a `make_row(...)` helper for `RowView` construction in pure tests; reuse existing `session_factory`/`client` conftest fixtures for integration tests.
- Framework install: none — pytest/pytest-asyncio/testcontainers already present.

## Security Domain

> `security_enforcement` not set to false → included. v1 is network-gated, single-user, no app auth (DEP-02), so authn/session/access-control categories are out of scope by design.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | DEP-02: Tailscale/LAN is the trust boundary; no app-level auth in v1. |
| V3 Session Management | no | No sessions (single-user, no auth). |
| V4 Access Control | no | Single tenant; no per-user authorization. |
| V5 Input Validation | **yes** | Pydantic v2 validates all rule/category DTOs and the predicate AST at the API boundary; malformed predicates rejected before the interpreter. |
| V6 Cryptography | partial | `hashlib.sha256` for the staleness token only — a non-secret integrity tag, not a security primitive; no secrets involved. Never hand-roll a hash. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Code injection via predicate "expression" | Elevation of Privilege / Tampering | Closed-op AST + `match` interpreter; **no `eval`/`exec`** (D-05, Anti-pattern 8). Test guard asserts absence. |
| ReDoS via user-supplied regex | Denial of Service | No regex in v1 (D-05); `ICONTAINS` is linear substring. Regex deferred to RE2/linear-time op. |
| SQL injection in rule/category CRUD | Tampering | SQLAlchemy parameterized queries / ORM throughout; `text()` reads use bound params (existing convention). |
| JSONB predicate with unbounded `IN` list → slow eval | DoS | Single-user, small rule count; `IN` materialized to a Python `set` once. Not a practical concern; revisit only if rule volume grows. |
| Referential corruption on category delete | Tampering / data integrity | FK `ON DELETE RESTRICT` + app pre-check → 409 (D-15). |

## Sources

### Primary (HIGH confidence)
- `src/finance_bro/db/models.py` (Transaction §lines 46–83; FxRate/TrackedFxCurrency seed-pattern models) — existing schema, column types, frozen columns.
- `src/finance_bro/db/transaction_repo.py` (lines 62–131) — frozen-by-omission upsert backing D-09.
- `src/finance_bro/services/import_service.py` (lines 52–102) — the D-11 hook point.
- `src/finance_bro/db/fx_rate_repo.py` / `tracked_fx_currency_repo.py` — repo + ON CONFLICT seed template.
- `alembic/versions/0003_fx_truth.py` — migration + `op.execute` seed idiom for 0004.
- `src/finance_bro/api/{routes_transactions,routes_backfill,schemas,deps}.py` + `main.py` — router/DTO/registration idioms.
- `tests/conftest.py`, `tests/test_transactions_route.py` — test harness (testcontainers, lifespan, fixtures).
- `tests/fixtures/statement_two_items.json` — verified Mono `raw_payload` field names (`originalMcc`, etc.).

### Secondary (MEDIUM confidence)
- [Monobank open API (v250818)](https://api.monobank.ua/docs/index.html) — `statementItem` field catalog incl. `comment`, `counterIban`, `counterEdrpou`, `receiptId` (FOP-only).

### Tertiary (LOW confidence)
- Proposed default taxonomy colors and exact MCC ranges (Default Taxonomy / Default MCC Rules tables) — `[ASSUMED]`, Claude's discretion; verify a sample of MCCs against real Mono data before final seed.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new packages; everything verified against installed code and green Phases 1–3.
- Architecture: HIGH — every seam (frozen upsert, repo+seed pattern, import hook, router idiom) verified directly in source.
- Predicate model / interpreter design: HIGH on the security/no-eval contract (locked by D-05); exact field names MEDIUM (Claude's discretion).
- Default taxonomy / MCC ranges: LOW/`[ASSUMED]` — explicitly discretionary; editable post-seed.
- Pitfalls: HIGH — derived from locked decisions + verified upsert behavior.

**Research date:** 2026-05-30
**Valid until:** ~2026-06-29 (stable internal stack; 30 days). The only external dependency (Mono `statementItem` field names) is stable.
