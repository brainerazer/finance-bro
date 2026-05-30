# Phase 4: Categorized Spending - Pattern Map

**Mapped:** 2026-05-30
**Files analyzed:** 16 (8 new, 5 modified, 3+ new test files)
**Analogs found:** 16 / 16 (every file has a strong in-repo analog — this phase is purely additive over a mature stack)

This is a **backend/API-only** phase (Python 3.13 + FastAPI 0.136 + SQLAlchemy 2.0 async + psycopg3 + Alembic + Postgres 17). No new external packages. Every seam already exists in the codebase; the planner should mirror the analogs below verbatim.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/finance_bro/categorizer/predicate.py` (NEW) | model (Pydantic AST) | transform | `src/finance_bro/api/schemas.py` (discriminated DTOs) | role-match (no existing tagged-union, but DTO idiom exact) |
| `src/finance_bro/categorizer/fields.py` (NEW) | utility (field resolver) | transform | `transaction_repo.py` `_op_currency_alpha` (raw_payload `.get()` discipline) | partial (same `.get()`-safety pattern) |
| `src/finance_bro/categorizer/interpreter.py` (NEW) | utility (pure interpreter) | transform | none direct — closest is pure helper `services/fx_rollup.py` | role-match (pure, no-DB module) |
| `src/finance_bro/categorizer/engine.py` (NEW) | service (pure) | transform / batch | `services/fx_rollup.py` (pure compute reused by repo) | role-match |
| `src/finance_bro/db/category_repo.py` (NEW) | repo | CRUD | `db/tracked_fx_currency_repo.py` | exact |
| `src/finance_bro/db/rule_repo.py` (NEW) | repo | CRUD | `db/tracked_fx_currency_repo.py` + `fx_rate_repo.py` | exact |
| `src/finance_bro/services/rules_history.py` (NEW) | service | batch / request-response | `services/import_service.py` (session-per-step orchestration) | role-match |
| `src/finance_bro/api/routes_categories.py` (NEW) | route | CRUD / request-response | `api/routes_backfill.py` (POST + 4xx) + `routes_transactions.py` (GET) | exact |
| `src/finance_bro/api/routes_rules.py` (NEW) | route | CRUD / request-response | `api/routes_backfill.py` + `routes_transactions.py` | exact |
| `alembic/versions/0004_categorized_spending.py` (NEW) | migration | DDL + seed | `alembic/versions/0003_fx_truth.py` | exact |
| `src/finance_bro/db/models.py` (MOD) | model (ORM) | — | `FxRate` / `TrackedFxCurrency` classes (same file) | exact |
| `src/finance_bro/db/transaction_repo.py` (MOD) | repo | CRUD (LATERAL read extend + new write methods) | same file `list_for_account` / `insert_many` | exact |
| `src/finance_bro/services/import_service.py` (MOD) | service | event-driven (post-insert step) | same file `run_one_card` step 4 | exact |
| `src/finance_bro/api/schemas.py` (MOD) | model (DTO) | — | same file (`TransactionOut`, `BackfillEnqueueIn`) | exact |
| `src/finance_bro/api/deps.py` (MOD) | config (DI) | — | same file `get_session` / `get_import_service` | exact |
| `src/finance_bro/main.py` (MOD) | config (router mount) | — | same file `app.include_router(...)` block | exact |
| `tests/test_*.py` (NEW, Wave 0 list) | test | — | `tests/test_fx_repos.py` (integration) + pure unit tests | exact |

## Pattern Assignments

---

### `src/finance_bro/db/category_repo.py` (repo, CRUD)

**Analog:** `src/finance_bro/db/tracked_fx_currency_repo.py` (lines 15-57) — same constructor shape, `select()` for reads, `text()` for targeted UPDATEs, `on_conflict_do_nothing` for idempotent seed-adjacent inserts.

**Constructor + imports** (`tracked_fx_currency_repo.py:15-24`):
```python
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import TrackedFxCurrency   # -> from .models import Category

class TrackedFxCurrencyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
```

**Ordered list read** (`tracked_fx_currency_repo.py:26-30`) — mirror for `list_all()` ordered by name/id:
```python
async def list_currencies(self) -> list[str]:
    result = await self._s.execute(
        select(TrackedFxCurrency.currency).order_by(TrackedFxCurrency.currency)
    )
    return list(result.scalars().all())
```

**Single-row get** (`tracked_fx_currency_repo.py:32-36`):
```python
async def get(self, currency: str) -> TrackedFxCurrency | None:
    result = await self._s.execute(
        select(TrackedFxCurrency).where(TrackedFxCurrency.currency == currency)
    )
    return result.scalar_one_or_none()
```

**Reference-count pre-check for D-15** — no exact analog; build with the established `text()` raw-SQL read idiom from `fx_rate_repo.py:34-40`:
```python
# fx_rate_repo.py count_in_window — the template for reference_counts(category_id)
async def count_in_window(self, currency: str, since_date: date) -> int:
    result = await self._s.execute(
        text("SELECT count(*) FROM fx_rates WHERE currency = :ccy AND rate_date >= :since"),
        {"ccy": currency, "since": since_date},
    )
    row = result.first()
    return int(row[0]) if row else 0
# -> two parameterized counts: rules WHERE category_id=:cid, transactions WHERE category_id=:cid
```

**Notes:** Repo owns SQL; ORM for simple CRUD, `text()` with bound params for counts/targeted updates (code_context "Established Patterns"). Never f-string a query.

---

### `src/finance_bro/db/rule_repo.py` (repo, CRUD)

**Analog:** `tracked_fx_currency_repo.py` (list/get/update) + `fx_rate_repo.py` (`upsert_many`).

**`list_active_ordered()`** — the engine consumer requires `ORDER BY priority ASC, id ASC` (RESEARCH Pitfall 6 deterministic tiebreak). Mirror `list_currencies` ordering:
```python
# from tracked_fx_currency_repo.py:26-30 — add .order_by(Rule.priority, Rule.id)
select(Rule).order_by(Rule.priority, Rule.id)
```

**Priority reorder** — single-transaction rewrite via `text()` UPDATE, idiom from `tracked_fx_currency_repo.py:43-47`:
```python
async def set_bootstrap_done(self, currency: str) -> None:
    await self._s.execute(
        text("UPDATE tracked_fx_currencies SET bootstrap_done = true WHERE currency = :ccy"),
        {"ccy": currency},
    )
```

**JSONB predicate column:** Rule.predicate is `JSONB` (see models pattern below); store the Pydantic predicate via `predicate.model_dump(mode="json")` on write and re-validate with `RulePredicate.model_validate(...)` on read.

---

### `src/finance_bro/db/models.py` (MOD — add `Category` + `Rule`, add FK on `Transaction.category_id`)

**Analog:** `FxRate` (models.py:136-157) and `TrackedFxCurrency` (models.py:160-179) in the same file.

**Import additions already present** (models.py:5-21) — `BigInteger, ForeignKey, Index, Integer, Text, UniqueConstraint, text` and `from sqlalchemy.dialects.postgresql import JSONB` are all imported already. No new imports needed.

**Table + PK + unique + server_default `now()`** (mirror `FxRate`/`TrackedFxCurrency`):
```python
# models.py:160-175 TrackedFxCurrency — the seed-table shape to copy
class TrackedFxCurrency(Base):
    __tablename__ = "tracked_fx_currencies"
    currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    bootstrap_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
```

**FK with `ondelete="RESTRICT"`** — the project already uses this exact idiom (models.py:50-54, `Transaction.account_id`). For D-15, the new `Category` FK on `Transaction.category_id` and `Rule.category_id` use the same:
```python
# models.py:50-54 — Transaction.account_id FK, the RESTRICT template for D-03/D-15
account_id: Mapped[int] = mapped_column(
    BigInteger,
    ForeignKey("accounts.id", ondelete="RESTRICT"),
    nullable=False,
)
```

**JSONB column** (models.py:59 `raw_payload`):
```python
raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
# -> Rule.predicate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
```

**Existing `Transaction.category_id`** (models.py:63) is a bare `BigInteger` nullable with NO FK — the migration adds the FK; the ORM model gains the `ForeignKey(...)` arg. `category_source` (line 64) and `is_user_locked` (lines 65-67) already exist.

---

### `alembic/versions/0004_categorized_spending.py` (NEW migration, DDL + seed)

**Analog:** `alembic/versions/0003_fx_truth.py` (entire file, lines 1-89) — exact template.

**Revision header** (`0003_fx_truth.py:1-16`):
```python
revision: str = "0003"
down_revision: str | None = "0002"
# -> revision = "0004", down_revision = "0003"
```

**`create_table` with PK/unique/server_default** (`0003_fx_truth.py:25-37`):
```python
op.create_table(
    "fx_rates",
    sa.Column("rate_date", sa.Date, nullable=False),
    sa.Column("currency", sa.CHAR(3), nullable=False),
    sa.Column("rate", sa.Numeric(18, 8), nullable=False),
    sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
              server_default=sa.text("now()")),
    sa.PrimaryKeyConstraint("rate_date", "currency"),
)
```

**Index creation** (`0003_fx_truth.py:41-46`):
```python
op.create_index("ix_fx_rates_currency_rate_date", "fx_rates",
                ["currency", "rate_date"], postgresql_using="btree")
```

**Idempotent seed via `op.execute` INSERT** (`0003_fx_truth.py:70-73`) — the exact idiom for seeding the ~15-category taxonomy and the MCC rules (D-04). JSONB predicates are written as JSON literals in the INSERT:
```python
op.execute(
    "INSERT INTO tracked_fx_currencies (currency, bootstrap_done) "
    "VALUES ('USD', false), ('EUR', false)"
)
```

**FK on a pre-existing column** — use `op.create_foreign_key` (RESEARCH §Code Examples confirms; the column `transactions.category_id` already exists with all-NULL data, so the constraint is safe):
```python
op.create_foreign_key(
    "fk_transactions_category", "transactions", "categories",
    ["category_id"], ["id"], ondelete="RESTRICT",
)
```

**Downgrade** (`0003_fx_truth.py:85-89`) — reverse order: drop FK, drop indexes, drop `rules`, drop `categories`:
```python
def downgrade() -> None:
    op.alter_column("transactions", "attributed_day", nullable=True)
    op.drop_table("tracked_fx_currencies")
    op.drop_index("ix_fx_rates_currency_rate_date", table_name="fx_rates")
    op.drop_table("fx_rates")
```

**Critical:** Postgres DDL is transactional — entire revision is one transaction (`0003_fx_truth.py:19-22` comment). Verify `SELECT count(*) FROM transactions WHERE category_id IS NOT NULL` is 0 before the FK (RESEARCH §Runtime State Inventory) — expected 0.

---

### `src/finance_bro/services/import_service.py` (MOD — add post-`insert_many` categorize step, D-11)

**Analog:** same file, `run_one_card` step 4 (import_service.py:86-95) — the session-per-step composition the new categorizer step hooks into.

**Existing hook point** (import_service.py:86-95) — the categorize step is inserted INSIDE this same `session.begin()` block, after `insert_many`:
```python
# Step 4: idempotent upsert (Phase 2: ON CONFLICT DO UPDATE; D-10 mutates
# only hold/amount_minor/raw_payload — all other columns frozen by omission).
async with self._session_factory() as session, session.begin():
    inserted, updated = await TransactionRepo(session).insert_many(card.id, items)
    # NEW (D-11): rules = await RuleRepo(session).list_active_ordered()
    #             rows = await TransactionRepo(session).fetch_for_categorize(card.id, touched_ids)  # NOT is_user_locked
    #             updates = engine.categorize_rows(rows, rules)   # pure, no session
    #             await TransactionRepo(session).apply_categories(updates)
```

**Constructor injection idiom** (import_service.py:43-50) — the service already holds `session_factory`; the pure engine needs no injection (it is imported as a module function). Keep `categorizer/` import at top, called per-row inside the step.

**Critical (RESEARCH Pitfall 1):** `fetch_for_categorize` MUST filter `WHERE NOT is_user_locked` in SQL; the engine also refuses locked rows as defense-in-depth (D-09). The frozen-by-omission upsert (transaction_repo.py:114-122) already guarantees the importer can never clobber `category_id`/`category_source`/`is_user_locked` — so the categorize step is purely additive writes.

---

### `src/finance_bro/db/transaction_repo.py` (MOD — extend LATERAL read; add `fetch_for_categorize` + `apply_categories`)

**Analog:** same file. `list_for_account` (transaction_repo.py:133-161) is the LATERAL read to extend with category data; `insert_many` (lines 66-131) is the write-method template.

**LATERAL read to extend** (transaction_repo.py:32-46, `ROLLUP_SQL`) — add a join to `categories` (LEFT JOIN, since `category_id` may be NULL — D-02) so the transactions response carries category name/color:
```python
ROLLUP_SQL = text(
    """
    SELECT t.id, t.account_id, ..., fx.rate AS fx_rate, fx.rate_date AS fx_rate_date
    FROM transactions t
    LEFT JOIN LATERAL ( ... ) fx ON true
    WHERE t.account_id = :account_id AND NOT t.is_deleted
    ORDER BY t.time DESC
    """
)
# -> add: LEFT JOIN categories c ON c.id = t.category_id  (carry c.name, c.color, t.category_id, t.category_source)
```

**raw_payload `.get()` safety** (transaction_repo.py:49-59, `_op_currency_alpha`) — the exact "never raise on a missing raw_payload key" discipline the field resolver (`categorizer/fields.py`) must follow (RESEARCH Pitfall 5):
```python
def _op_currency_alpha(raw_payload: dict[str, Any]) -> str | None:
    code = raw_payload.get("currencyCode")   # .get() — absent key -> None, never KeyError
    if code is None:
        return None
    try:
        return numeric_to_alpha(int(code))
    except (ValueError, TypeError):
        return None
```

**Write-method template** (`insert_many`, transaction_repo.py:114-127) — `apply_categories` mirrors the `stmt = insert(...).values(rows)` + `execute` shape, but as an UPDATE of `category_id` + `category_source='rule'` over the touched ids.

---

### `src/finance_bro/services/rules_history.py` (NEW service — CAT-05 preview/commit, D-12/D-13)

**Analog:** `services/import_service.py` (session-per-step orchestration, HTTP/pure-compute outside the session) — `rules_history.py` is the same shape: fetch rules + non-locked rows in a session, run the **pure engine** (same call as the import step — D-11 reuse), compute diff + token outside the session.

**Service constructor + session_factory idiom** (import_service.py:43-50):
```python
class ImportService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], importer: MonobankImporter) -> None:
        self._session_factory = session_factory
        self._importer = importer
# -> RulesHistoryService.__init__(self, session_factory): self._session_factory = session_factory
```

**Token recipe** (RESEARCH Pattern 5, stdlib only): `hashlib.sha256` over `json.dumps({"rules": rules_sig, "rows": sorted((id, category_id))}, sort_keys=True, separators=(",",":"), default=str)`. Commit re-runs and compares; mismatch -> 409 (see route).

**Preview response DTOs** go in `schemas.py` (see below): `changed_count`, `overwritten_count`, `skipped_locked_count`, `changes: list[CategoryChange]`, `token`.

---

### `src/finance_bro/api/routes_categories.py` + `routes_rules.py` (NEW routes)

**Analog:** `api/routes_backfill.py` (POST + HTTPException, lines 1-55) for mutations and 4xx; `api/routes_transactions.py` (GET + repo call, lines 26-37) for list reads.

**Router + `Depends(get_session)` + `response_model`** (routes_transactions.py:16-37):
```python
from fastapi import APIRouter, Depends
from finance_bro.api.deps import get_session
router = APIRouter()

@router.get("/api/transactions", response_model=list[TransactionOut])
async def list_transactions(session: Annotated[AsyncSession, Depends(get_session)]) -> list[TransactionOut]:
    rows = await TransactionRepo(session).list_for_account(card.id)
    return [TransactionOut.model_validate(r) for r in rows]
```

**POST with body DTO + `HTTPException` for the D-15 409** (routes_backfill.py:26-54) — the exact 4xx idiom; for category delete the 409 body carries reference counts:
```python
@router.post("/api/backfill", response_model=BackfillEnqueueOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_backfill(body: BackfillEnqueueIn, runner: Annotated[SchedulerRunner, Depends(get_scheduler_runner)]) -> BackfillEnqueueOut:
    try:
        run_ids = await runner.enqueue_backfill(account_id=body.account_id, months=body.months)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return BackfillEnqueueOut(run_ids=run_ids)
```

**D-15 delete pattern** (from RESEARCH Pattern 6, built on this idiom):
```python
@router.delete("/api/categories/{cid}")
async def delete_category(cid: int, session=Depends(get_session)):
    rules_n, tx_n = await CategoryRepo(session).reference_counts(cid)
    if rules_n or tx_n:
        raise HTTPException(409, detail={"rules": rules_n, "transactions": tx_n})
    await CategoryRepo(session).delete(cid)
```

**structlog usage** (routes_backfill.py:15,23,35) — `_log = structlog.get_logger()`; log `.start`/`.done` around mutations (optional, matches repo convention).

---

### `src/finance_bro/api/schemas.py` (MOD — add Category/Rule/Predicate/Diff DTOs)

**Analog:** same file. `TransactionOut` (schemas.py:40-61) for `from_attributes` read DTOs; `BackfillEnqueueIn` (schemas.py:124-126) for bounded request DTOs.

**`from_attributes` read DTO** (schemas.py:40-46):
```python
class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    ...
# -> CategoryOut(from_attributes=True): id, name, color
```

**Bounded request DTO with `Field`** (schemas.py:124-126):
```python
class BackfillEnqueueIn(BaseModel):
    account_id: int | None = None
    months: int = Field(default=12, ge=1, le=36)
```

**Discriminated-union predicate AST** (RESEARCH Pattern 1) — new to this file but uses only already-imported `BaseModel`, `Field` (schemas.py:19). Use `Annotated[Union[...], Field(discriminator="op")]`. This is the request/storage shape for rules; validates malformed predicates at the API boundary before the interpreter (RESEARCH §Don't Hand-Roll).

**Diff DTOs** (RESEARCH Pattern 5): `CategoryChange(transaction_id, old_category_id, new_category_id)` and `RunPreviewOut(changed_count, overwritten_count, skipped_locked_count, changes, token)`.

---

### `src/finance_bro/api/deps.py` (MOD) + `src/finance_bro/main.py` (MOD)

**deps.py analog** (deps.py:32-52): `get_session` already exists and is reused as-is. If `RulesHistoryService` needs a provider, mirror `get_import_service` (deps.py:49-52):
```python
def get_import_service(importer: Annotated[MonobankImporter, Depends(get_importer)]) -> ImportService:
    return ImportService(get_session_factory(), importer)
# -> get_rules_history_service() -> RulesHistoryService(get_session_factory())
```

**main.py router mount** (main.py:39-46 import block, 138-143 include block):
```python
from finance_bro.api import (routes_accounts, routes_backfill, routes_health,
    routes_import, routes_status, routes_transactions)
...
app.include_router(routes_backfill.router)
# -> add routes_categories, routes_rules to both the import tuple and the include block
```
No prefix, no middleware (DEP-02 — LAN/Tailscale is the trust boundary; main.py:23-26).

---

### Test files (NEW — Wave 0 list from RESEARCH §Validation Architecture)

**Pure unit tests** (no Postgres — `categorizer/` interpreter, engine, field resolver, no-eval guard): plain `pytest` with a `make_row(...)` helper. No conftest fixture needed. Run via `uv run pytest tests/test_categorizer_interpreter.py -x -q`.

**Integration tests** — **Analog:** `tests/test_fx_repos.py` (lines 1-102) — the exact `session_factory` fixture pattern for repo/migration/CRUD tests:
```python
@pytest.mark.asyncio
async def test_tracked_currency_lifecycle(session_factory):
    async with session_factory() as s, s.begin():
        repo = TrackedFxCurrencyRepo(s)
        await repo.upsert_currency("ZZZ")
    async with session_factory() as s:
        repo = TrackedFxCurrencyRepo(s)
        listed = await repo.list_currencies()
    assert listed == sorted(listed)
```

**Route/HTTP integration tests** use the `client` fixture (conftest.py:90-135, `AsyncClient` over `ASGITransport`); `runner` fixture (conftest.py:73-88) when `app.state.runner` is needed. Tests set `APP_DISABLE_SCHEDULER=1` (main.py:95) for deterministic route tests.

---

## Shared Patterns

### Frozen-by-omission lock protection (the phase's hard invariant — DB-backed for free)
**Source:** `src/finance_bro/db/transaction_repo.py:114-127` (`insert_many` ON CONFLICT SET clause omits `category_id`/`category_source`/`is_user_locked`).
**Apply to:** `import_service` categorize step, `rules_history` sweep. The durability guarantee (manual edits survive every re-run) is already enforced at the DB level. Phase 4 only must: (a) NOT write `is_user_locked` rows in the engine, (b) set both `category_source='manual'` AND `is_user_locked=true` on manual recategorize.
```python
stmt = stmt.on_conflict_do_update(
    index_elements=["account_id", "source_tx_id"],
    index_where=text("NOT is_deleted"),
    set_={ "hold": stmt.excluded.hold,
           "amount_minor": stmt.excluded.amount_minor,
           "raw_payload": stmt.excluded.raw_payload },  # category_* / is_user_locked NOT here
)
```

### raw_payload `.get()` safety (never raise on absent key — D-08 / Pitfall 5)
**Source:** `src/finance_bro/db/transaction_repo.py:49-59` (`_op_currency_alpha`).
**Apply to:** `categorizer/fields.py` field resolver. Counterparty IBAN/EDRPOU/comment are FOP-only and frequently absent on card rows; use `raw_payload.get(key)` → `None`, and a condition over a `None` field evaluates `False` (no match), never `KeyError`.

### Repo constructor + session ownership
**Source:** every repo (`tracked_fx_currency_repo.py:22-24`, `fx_rate_repo.py:21-23`, `transaction_repo.py:62-64`).
**Apply to:** `category_repo`, `rule_repo`. `def __init__(self, session: AsyncSession) -> None: self._s = session`. Repo owns SQL; instantiated fresh per session-block; ORM `select()` for simple reads, `text()` with bound params (never f-strings) for counts/targeted updates.

### Idempotent seed via migration `op.execute` (D-04 MCC rules as seeded rows, NOT a hardcoded dict)
**Source:** `alembic/versions/0003_fx_truth.py:70-73`.
**Apply to:** migration `0004` taxonomy + MCC-rule seed. RESEARCH Pitfall 3: there must be NO `MCC_MAP = {...}` constant in code — MCC coverage ships as ordinary editable `rules` rows.

### Money / minor-units discipline
**Source:** `CLAUDE.md §Money`; `schemas.py:1-14`, `transaction_repo.py` (amounts are `int` minor units).
**Apply to:** all predicate amount comparisons (`amount_minor` is `int`; `amount_sign`/`amount_range` are integer comparisons — never `float()`).

### No `eval`/regex in the predicate path (D-05 / Anti-pattern 8)
**Source:** RESEARCH Pattern 2 (closed-op `match` interpreter). Test guard `tests/test_no_eval_in_categorizer.py` greps `categorizer/` for `eval(`/`exec(`/`re.compile`/`import re` and asserts none.
**Apply to:** `categorizer/interpreter.py`. The `op` tag selects a pre-written `match` branch; values are only ever compared, never executed.

### Router mount, no-prefix, no-auth
**Source:** `main.py:138-143` + DEP-02.
**Apply to:** `routes_categories`, `routes_rules` — `app.include_router(...)` with no prefix/middleware; routes hard-code `/api/...` paths (LAN/Tailscale trust boundary).

## No Analog Found

No file lacks an analog. Two modules have only **partial** analogs (closest pure-compute or DTO idioms exist, but no identical construct):

| File | Role | Data Flow | Closest partial analog | Gap the planner fills from RESEARCH |
|------|------|-----------|------------------------|-------------------------------------|
| `categorizer/predicate.py` | Pydantic tagged-union AST | transform | `schemas.py` DTO idiom | No existing discriminated `Union[...]` in repo — use RESEARCH Pattern 1 (`Annotated[Union, Field(discriminator="op")]`). |
| `categorizer/interpreter.py` | pure `match`-based op evaluator | transform | `services/fx_rollup.py` (pure, no-DB) | No existing `match`-statement interpreter — use RESEARCH Pattern 2; closed op set, no `eval`/regex. |

Both gaps fall inside CONTEXT.md "Claude's Discretion" (predicate JSON shape, engine module-vs-class) — the planner is empowered to lock the RESEARCH-proposed shape without user confirmation.

## Metadata

**Analog search scope:** `src/finance_bro/db/`, `src/finance_bro/services/`, `src/finance_bro/api/`, `alembic/versions/`, `tests/`.
**Files scanned (read in full):** `db/models.py`, `db/transaction_repo.py`, `db/fx_rate_repo.py`, `db/tracked_fx_currency_repo.py`, `alembic/versions/0003_fx_truth.py`, `services/import_service.py`, `api/routes_transactions.py`, `api/routes_backfill.py`, `api/schemas.py`, `api/deps.py`, `main.py`, `tests/test_fx_repos.py`; plus `tests/conftest.py` (fixtures grep).
**Pattern extraction date:** 2026-05-30
