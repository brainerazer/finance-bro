---
phase: 02-reliable-sync
plan: 02
type: execute
wave: 2
depends_on: ["02-01"]
files_modified:
  - src/finance_bro/importers/base.py
  - src/finance_bro/db/transaction_repo.py
  - src/finance_bro/api/schemas.py
  - tests/test_hold_cleared_upsert.py
  - tests/test_transactions_route.py
  - tests/test_idempotency.py
autonomous: true
requirements:
  - ING-05
tags: [phase-02, upsert, hold-cleared, on-conflict-do-update, transaction-out]

must_haves:
  truths:
    - "A first-seen transaction with hold:true is inserted with hold=true and amount_minor=A; the row appears in GET /api/transactions with hold:true."
    - "A subsequent payload with the same (account_id, source_tx_id), hold:false, amount_minor=B (B != A), and a different raw_payload UPDATEs the existing row in place — single row remains; hold becomes false; amount_minor becomes B; raw_payload is the new shape."
    - "On the same upsert, currency, time, account_id, source_tx_id, created_at are unchanged."
    - "On the same upsert, is_user_locked, category_id, category_source, is_deleted, description, mcc, attributed_day are unchanged (D-10 frozen-fields invariant)."
    - "TransactionRepo.insert_many returns (inserted, updated_in_place) where inserted counts xmax=0 rows and updated counts the rest."
    - "TransactionOut.hold: bool is present in the GET /api/transactions JSON shape."
  artifacts:
    - path: "src/finance_bro/importers/base.py"
      provides: "CanonicalTransaction extended with hold/description/mcc fields"
      contains: "hold: bool"
    - path: "src/finance_bro/db/transaction_repo.py"
      provides: "insert_many uses on_conflict_do_update with EXACTLY hold/amount_minor/raw_payload mutated; xmax=0 returning"
      contains: "on_conflict_do_update"
    - path: "src/finance_bro/api/schemas.py"
      provides: "TransactionOut.hold + AccountOut.mono_type"
      contains: "hold: bool"
    - path: "tests/test_hold_cleared_upsert.py"
      provides: "ING-05 + SC#3 + D-10 frozen-fields invariant"
      exports: ["test_hold_inserted_with_flag", "test_cleared_updates_in_place", "test_e2e_hold_then_cleared"]
  key_links:
    - from: "src/finance_bro/db/transaction_repo.py"
      to: "uq_transactions_account_source_tx partial unique index (Phase 1)"
      via: "index_elements + index_where=text('NOT is_deleted')"
      pattern: "index_where=text"
    - from: "src/finance_bro/db/transaction_repo.py"
      to: "(xmax = 0) RETURNING"
      via: "literal_column to detect insert vs update"
      pattern: "literal_column.*xmax"
    - from: "src/finance_bro/api/schemas.py::TransactionOut"
      to: "Transaction.hold ORM column (Phase 1 schema, now actively populated)"
      via: "ConfigDict(from_attributes=True) hydrates hold into the response"
      pattern: "hold: bool"
    - from: "tests/test_hold_cleared_upsert.py"
      to: "tests/fixtures/statement_with_hold.json + statement_cleared_followup.json"
      via: "json.load fixtures; assert single row, mutated fields, frozen fields"
      pattern: "HOLD-FIXTURE-ID-1"
---

<objective>
Switch `TransactionRepo.insert_many` from `ON CONFLICT DO NOTHING` (Phase 1) to `ON CONFLICT DO UPDATE` with EXACTLY THREE EXCLUDED fields (`hold`, `amount_minor`, `raw_payload`), and surface `hold` on the API. After this plan, a `hold:true` transaction received once and a `hold:false` follow-up of the same Mono `id` produce a single row in the DB whose `hold`, `amount_minor`, `raw_payload` reflect the cleared payload while every other column — including manual edit columns from Phases 4–6 — remains untouched.

This is the **central correctness invariant** of Phase 2 (D-10 + ING-05 + SC#3). Any deviation in the SET clause silently breaks Phase 1's Pitfall-10 promise that the importer never overwrites manual edits.

Purpose: deliver ING-05 end-to-end as a vertical slice (importer field plumbing → DB upsert clause → API response field), each step independently testable, with the central correctness test going through every layer.
Output: 3 modified source files, 1 new test file, 2 modified test files — all green under `uv run pytest -x`.
</objective>

<phase_goal>
Bohdan stops clicking import. The app polls Mono on its own at the rate-limit budget, ingests holds correctly (and updates them in place when they clear), can backfill 12 months on first connect, and surfaces "last poll N min ago" plus 401/429 distinctly so silent failures are impossible.
</phase_goal>

<plan_scope>
**Delivers:**
- `CanonicalTransaction` (`src/finance_bro/importers/base.py`) gains `hold: bool = False`, `description: str | None = None`, `mcc: int | None = None` so the importer can populate them on first INSERT (D-10 + Discretion bullet 8).
- `TransactionRepo.insert_many` (`src/finance_bro/db/transaction_repo.py`):
  - Return type changes from `int` to `tuple[int, int]` (`inserted`, `updated_in_place`).
  - `rows` dict gains `description`, `mcc`, `hold`.
  - Uses `on_conflict_do_update` with `set_={"hold": stmt.excluded.hold, "amount_minor": stmt.excluded.amount_minor, "raw_payload": stmt.excluded.raw_payload}` — **THESE THREE AND NO OTHERS** (D-10 invariant).
  - `.returning(Transaction.id, literal_column("(xmax = 0)").label("inserted"))` to count inserts vs updates (RESEARCH.md Pattern 3).
- `TransactionOut.hold: bool` (`src/finance_bro/api/schemas.py`).
- `AccountOut.mono_type: str | None = None` (small wiring; the column exists from 02-01 — `GET /api/accounts` should expose it for the status surface in 02-04).
- `tests/test_hold_cleared_upsert.py` (NEW) — covers ING-05 + SC#3 + D-10 with three test functions per VALIDATION.md.
- `tests/test_transactions_route.py` (MODIFIED) — add `test_hold_field_in_response` per VALIDATION.md.
- `tests/test_idempotency.py` (MODIFIED) — Phase 1's idempotency test now operates against an UPDATE-not-NOTHING upsert; the test's contract ("second import is a no-op") needs subtle change: it's still a no-op for the user (one row), but `inserted` is now `(0, N)` not `0`. Update assertions accordingly.

**Does NOT deliver (in this plan):**
- `ImportService.run_one_card` adaptation to consume the new tuple return — Phase 1 still calls the old 1-int return; this plan makes `insert_many` return a tuple, so a CALL-SITE update is required. **Decision (per the inline rule below):** because changing `insert_many`'s signature breaks `ImportService.run_one_card`, this plan must also update that call site to unpack the tuple. The `ImportResultOut` shape is preserved (Phase 1 contract); `inserted = inserted+updated`, `skipped_duplicates = statement_count - inserted - updated` (which becomes 0 for hold→cleared transitions). This is a minimal, surgical change to `import_service.py` and the existing `tests/test_import_route.py` continues to pass UNCHANGED. Plan 02-04 is the one that reshapes `routes_import.py` per D-16.
- The `MonobankImporter` change to populate `hold`/`description`/`mcc` from Mono payloads — that's Plan 02-03 (it lands alongside the typed-error work in the same module). Until 02-03, the importer yields `CanonicalTransaction` objects without those fields populated; `getattr(t, "hold", False)` in the upsert handles the transitional case (always defaults to False — same effective behavior as Phase 1's missing-column write).

**Why this slice is end-to-end testable on its own:** the new test calls `TransactionRepo.insert_many` directly (Archetype B from PATTERNS.md) with synthesized `CanonicalTransaction` instances carrying `hold=True/False`. No HTTP, no Mono, no scheduler — just SQL + ORM verification. The transactions-route test extension exercises the full hydration path (Transaction.hold → ORM → Pydantic ConfigDict → JSON).
</plan_scope>

<plan_dependencies>
- **Hard depends on:** `02-01-schema-repos-PLAN.md` (Wave 1) — uses fixtures `statement_with_hold.json` / `statement_cleared_followup.json` and the conftest truncate that covers `import_runs`/`scheduler_state` (so HTTP-route tests inherit a clean slate).
- **Independent of:** 02-03 and 02-04. Can execute in parallel with 02-03's scheduler-runner work because no shared file is touched (02-03 modifies `monobank.py`/`main.py`/new files; this plan modifies `transaction_repo.py`/`schemas.py`/`base.py`/`import_service.py`).
- **Blocks:** 02-03's hold-cleared end-to-end test (`tests/test_hold_cleared_upsert.py::test_e2e_hold_then_cleared`) only as a logical dependency — that test exercises the upsert via the runner; if 02-02 hasn't landed, the runner test would observe DO NOTHING semantics. Wave assignment: 02-02 is Wave 2, 02-03 is Wave 3 (because it consumes 02-02's tuple return).
</plan_dependencies>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/REQUIREMENTS.md
@.planning/ROADMAP.md
@.planning/phases/02-reliable-sync/02-CONTEXT.md
@.planning/phases/02-reliable-sync/02-RESEARCH.md
@.planning/phases/02-reliable-sync/02-VALIDATION.md
@.planning/phases/02-reliable-sync/02-PATTERNS.md
@.planning/phases/02-reliable-sync/02-01-schema-repos-PLAN.md
@CLAUDE.md
@src/finance_bro/db/transaction_repo.py
@src/finance_bro/db/models.py
@src/finance_bro/importers/base.py
@src/finance_bro/api/schemas.py
@src/finance_bro/services/import_service.py
@tests/test_idempotency.py
@tests/test_partial_unique_index.py
@tests/test_transactions_route.py

<interfaces>
<!-- Key types/contracts the executor will modify or extend. Extracted from the codebase. -->

From `src/finance_bro/importers/base.py` (current — extend, do NOT change existing fields):
```python
@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str
    source_account_id: str
    occurred_at: datetime
    amount_minor: int
    currency: str
    raw: dict[str, Any]
    # NEW (this plan):
    # hold: bool = False
    # description: str | None = None
    # mcc: int | None = None
```

From `src/finance_bro/db/transaction_repo.py` (current — replace `on_conflict_do_nothing(...)` with `on_conflict_do_update(...)`, add xmax counting):
```python
# CURRENT signature
async def insert_many(self, account_id: int, items: list[CanonicalTransaction]) -> int: ...

# AFTER this plan
async def insert_many(self, account_id: int, items: list[CanonicalTransaction]) -> tuple[int, int]:
    """Returns (inserted, updated_in_place). On conflict, only hold/amount_minor/raw_payload mutate (D-10)."""
```

From `src/finance_bro/db/models.py::Transaction` (Phase 1 — DO NOT MODIFY in this plan, just READ; the `hold` column already exists from 0001 migration as `Boolean NOT NULL DEFAULT false`):
```python
hold: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
```

From `src/finance_bro/api/schemas.py::TransactionOut` (current — add ONLY `hold: bool`):
```python
class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    source_tx_id: str
    amount_minor: int
    currency: str = Field(min_length=3, max_length=3)
    time: datetime
    raw_payload: dict[str, Any]
    # NEW: hold: bool
```

From `src/finance_bro/api/schemas.py::AccountOut` (add `mono_type: str | None = None`):
```python
class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_kind: str
    source_account_id: str
    currency: str = Field(min_length=3, max_length=3)
    # NEW: mono_type: str | None = None
```

From `src/finance_bro/services/import_service.py` line 84 (the call site that breaks when insert_many returns a tuple):
```python
inserted = await TransactionRepo(session).insert_many(card.id, items)  # CURRENT
# AFTER: inserted, updated = await TransactionRepo(session).insert_many(card.id, items)
# Then in ImportResult construction (line 86-91), preserve Phase 1's body shape:
#   inserted = inserted + updated   # for hold→cleared no-double-count
#   skipped_duplicates = len(items) - inserted   # remains correct because updates count toward "touched"
# Document the call-site decision in a code comment.
```

The verbatim upsert SQL is RESEARCH.md Code Examples §1 (lines 776-803) and PATTERNS.md lines 116-127 — copy verbatim. The xmax detection trick is RESEARCH.md Pattern 3 lines 437-484.

From `02-PATTERNS.md` Pattern S6 (lines 906-916) — the upsert invariants you MUST preserve:
- `index_elements=["account_id", "source_tx_id"]`
- `index_where=text("NOT is_deleted")`
- The partial unique index `uq_transactions_account_source_tx` is unchanged
- The SET clause MUST contain ONLY `hold`, `amount_minor`, `raw_payload` — anything else is a bug
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Extend CanonicalTransaction; switch insert_many to ON CONFLICT DO UPDATE; update ImportService call site</name>
  <files>
    src/finance_bro/importers/base.py,
    src/finance_bro/db/transaction_repo.py,
    src/finance_bro/services/import_service.py,
    tests/test_idempotency.py
  </files>
  <action>
**1) `src/finance_bro/importers/base.py`** — extend `CanonicalTransaction`.

Add three fields with defaults (so Phase 1 callers — Plan 02-03 hasn't landed yet — keep working):
```python
@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str
    source_account_id: str
    occurred_at: datetime
    amount_minor: int
    currency: str
    raw: dict[str, Any]
    hold: bool = False
    description: str | None = None
    mcc: int | None = None
```
Default values are mandatory because `frozen=True` + adding non-default fields breaks existing instantiations (Phase 1 importer code in `monobank.py` doesn't yet pass these — that's 02-03's responsibility). With defaults, both code paths coexist during the wave-2/wave-3 transition.

**2) `src/finance_bro/db/transaction_repo.py`** — replace the upsert.

Read the current file in full first. Then:

(a) Add to imports: `from sqlalchemy import literal_column` (alongside `select`, `text`).

(b) Update the `insert_many` signature and body. **Verbatim from RESEARCH.md Code Examples §1 (lines 776-803) + Pattern 3 (lines 437-477) + PATTERNS.md transformation §1 (lines 116-126).** Copy the structure exactly:

```python
async def insert_many(
    self,
    account_id: int,
    items: list[CanonicalTransaction],
) -> tuple[int, int]:
    """Upsert canonical transactions idempotently.

    On conflict (i.e., a row with the same (account_id, source_tx_id) WHERE NOT
    is_deleted already exists), the upsert mutates EXACTLY THREE columns:
    `hold`, `amount_minor`, `raw_payload` (D-10). All other columns — currency,
    time, account_id, source_tx_id, created_at, is_user_locked, category_id,
    category_source, is_deleted, description, mcc, attributed_day — are FROZEN
    BY OMISSION. Phase 1's Pitfall-10 promise that the importer never overwrites
    manual edits stays a hard invariant.

    Returns `(inserted, updated_in_place)`. The `xmax = 0` trick: PostgreSQL's
    `xmax` system column is 0 on freshly-inserted rows; ON CONFLICT DO UPDATE
    sets it to the current transaction id. RESEARCH.md Pattern 3 + Pitfall 6.
    """
    if not items:
        return (0, 0)
    rows = [
        {
            "account_id": account_id,
            "source_tx_id": t.source_tx_id,
            "amount_minor": t.amount_minor,
            "currency": t.currency,
            "time": t.occurred_at,
            "raw_payload": t.raw,
            # On first INSERT, the importer is allowed to populate description/mcc
            # (Discretion bullet 8 + PATTERNS.md transformation §2). They become
            # immutable after the row exists because they are absent from set_={...}.
            "description": t.description,
            "mcc": t.mcc,
            "hold": t.hold,
        }
        for t in items
    ]
    stmt = insert(Transaction).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id", "source_tx_id"],
        index_where=text("NOT is_deleted"),
        set_={
            "hold": stmt.excluded.hold,
            "amount_minor": stmt.excluded.amount_minor,
            "raw_payload": stmt.excluded.raw_payload,
        },
    ).returning(
        Transaction.id,
        literal_column("(xmax = 0)").label("inserted"),
    )
    result = await self._s.execute(stmt)
    rows_back = result.all()
    inserted = sum(1 for r in rows_back if r.inserted)
    updated = len(rows_back) - inserted
    return (inserted, updated)
```

**Anti-patterns to AVOID (RESEARCH.md + PATTERNS.md Pattern S6):**
- Adding `description`, `mcc`, `is_user_locked`, `category_id`, `category_source`, `is_deleted`, `currency`, `time`, `attributed_day`, `account_id`, `source_tx_id`, `created_at` to the `set_={...}` dict. **Each addition is a bug.** D-10 freezes them by omission.
- Using `result.scalars().all()` (Phase 1 idiom) — that drops the `inserted` boolean column. Must use `result.all()` and access via `r.inserted` row attribute.
- Removing the `index_where=text("NOT is_deleted")` predicate — Postgres requires it for the partial unique index to participate in conflict detection.

**3) Update `src/finance_bro/db/transaction_repo.py` module docstring** — Phase 1's docstring (lines 1-9) describes DO NOTHING semantics. Replace with the new D-10 contract. Keep it concise (≤8 lines): mention DO UPDATE, the three mutated fields, the xmax trick, and the frozen-by-omission invariant.

**4) Update `src/finance_bro/services/import_service.py` line 84:**

Read the file first (full 92 lines). At line 84, change:
```python
inserted = await TransactionRepo(session).insert_many(card.id, items)
```
to:
```python
inserted, updated = await TransactionRepo(session).insert_many(card.id, items)
# Phase 1 ImportResultOut shape preserved (Plan 02-04 reshapes the route).
# `inserted_total` accounts for both first-insert rows and hold→cleared updates;
# Phase 1's "second-import is a no-op" semantics still hold for already-final cleared
# rows (those become no-op UPDATEs that we count as updated_in_place; user-facing
# "duplicates" are now the rows where neither inserted nor updated_in_place added new
# user-visible data — see RESEARCH.md Pitfall 2).
inserted_total = inserted + updated
```
Then at lines 86-91, change `inserted=inserted` to `inserted=inserted_total` and `skipped_duplicates=len(items) - inserted` to `skipped_duplicates=len(items) - inserted_total`. The `ImportResult` dataclass shape is unchanged.

This minimal change keeps `tests/test_import_route.py` passing UNCHANGED until Plan 02-04 reshapes `routes_import.py` per D-16. Confirm by running the existing route test after this task.

**5) Update `tests/test_idempotency.py`:**

Read the file first to understand how it asserts the second-import is a no-op. There are two likely shapes:
  - **Shape A (asserts `inserted == 0` directly):** the test calls `TransactionRepo.insert_many` twice and asserts the second call returns `0`. After this plan it returns `(0, N)` where N == statement_count. Update the assertion to `inserted, updated = await ...; assert inserted == 0; assert updated == N`.
  - **Shape B (asserts `skipped_duplicates == N` via the route):** uses HTTP and reads the route body. Plan 02-04 reshapes the route; for now the existing route still returns Phase 1 shape via the call-site adapter above (`inserted_total = inserted + updated`). Either the assertion is `assert body["inserted"] == N` (now becomes 0+N=N — still passes) or `assert body["skipped_duplicates"] == 0` (now also passes because `len(items) - N = 0`).
  
  Read carefully and adjust ONLY the assertions that change because of the new tuple return — do not refactor the test architecture.

If the test still passes unchanged after the import_service.py adapter, leave it alone and document this in the SUMMARY. **Do not preemptively refactor the test.**
  </action>
  <verify>
    <automated>uv run pytest tests/test_idempotency.py tests/test_partial_unique_index.py tests/test_import_route.py tests/test_importer_statement.py -x &amp;&amp; grep -c "set_=" src/finance_bro/db/transaction_repo.py | grep -qE '^1$' &amp;&amp; grep -E '"hold"|"amount_minor"|"raw_payload"' src/finance_bro/db/transaction_repo.py | grep -v '^#' | wc -l | awk '$1 &gt;= 4 {exit 0} {exit 1}' &amp;&amp; ! grep -E '"description"\s*:\s*stmt\.excluded|"mcc"\s*:\s*stmt\.excluded|"is_user_locked"\s*:\s*stmt\.excluded|"category_id"\s*:\s*stmt\.excluded' src/finance_bro/db/transaction_repo.py &amp;&amp; grep -q "literal_column" src/finance_bro/db/transaction_repo.py</automated>
  </verify>
  <done>insert_many returns a tuple; the SET clause contains EXACTLY hold/amount_minor/raw_payload (verified by grep — see commands); xmax=0 detection is wired; ImportService still produces a Phase 1-shaped ImportResult; idempotency test passes (with adjusted assertions if needed); no Phase 1 test regresses.</done>
</task>

<task type="auto">
  <name>Task 2: TransactionOut.hold + AccountOut.mono_type; new test_hold_cleared_upsert.py + extend test_transactions_route.py</name>
  <files>
    src/finance_bro/api/schemas.py,
    tests/test_hold_cleared_upsert.py,
    tests/test_transactions_route.py
  </files>
  <action>
**1) `src/finance_bro/api/schemas.py`** — additive changes only.

(a) Add `hold: bool` to `TransactionOut` (right before `raw_payload` so the field order is logical: identifiers → money → time → status → payload):
```python
class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    source_tx_id: str
    amount_minor: int
    currency: str = Field(min_length=3, max_length=3)
    time: datetime
    hold: bool
    raw_payload: dict[str, Any]
```
`ConfigDict(from_attributes=True)` already auto-hydrates `hold` from the `Transaction.hold` ORM column (which is non-null with `server_default='false'` per Phase 1 schema, so the field is always populated).

(b) Add `mono_type: str | None = None` to `AccountOut` (after `currency`):
```python
class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_kind: str
    source_account_id: str
    currency: str = Field(min_length=3, max_length=3)
    mono_type: str | None = None
```
`mono_type` is nullable in the DB (per 02-01); the default makes the JSON field optional / null for non-card accounts.

(c) **Do NOT touch** `ImportResultOut` in this plan. Plan 02-04 owns the D-16 reshape that introduces `ImportEnqueuedOut` / `ImportEnqueueRowOut` and the new status schemas. Keep `ImportResultOut` as-is — `ImportService` still returns it.

**2) Create `tests/test_hold_cleared_upsert.py`** — covers ING-05 + SC#3 + D-10 frozen fields. Use Archetype B (testcontainers + raw SQL) per PATTERNS.md lines 745-759.

Required test functions (each one maps to a row in 02-VALIDATION.md):

```python
import json
from datetime import UTC, datetime
import pytest
from sqlalchemy import text
from finance_bro.db.transaction_repo import TransactionRepo
from finance_bro.importers.base import CanonicalTransaction


@pytest.mark.asyncio
async def test_hold_inserted_with_flag(session_factory):
    """ING-05: a hold:true CanonicalTransaction is inserted with hold=true."""
    # 1. Seed an account row directly via raw SQL (mirror test_partial_unique_index.py).
    # 2. Construct a CanonicalTransaction with hold=True, source_tx_id='HOLD-FIXTURE-ID-1', amount_minor=-12345.
    # 3. async with session_factory() as s, s.begin(): inserted, updated = await TransactionRepo(s).insert_many(account_id, [t])
    # 4. assert (inserted, updated) == (1, 0)
    # 5. Read back via raw SQL: SELECT hold, amount_minor FROM transactions WHERE source_tx_id='HOLD-FIXTURE-ID-1'
    # 6. assert hold is True, amount_minor == -12345.


@pytest.mark.asyncio
async def test_cleared_updates_in_place(session_factory):
    """ING-05 + D-10 + D-11: cleared payload UPDATEs single row, mutates only hold/amount_minor/raw_payload, freezes the rest."""
    # 1. Seed account, insert hold:true row with amount_minor=-12345, raw_payload={"k":"v1"}.
    # 2. CRITICAL: After insert, manually set is_user_locked=True, category_id=42, category_source='manual',
    #    description='user note', mcc=5411, attributed_day='2026-05-01'  via raw SQL UPDATE
    #    (simulates a Phase-4/5/6 manual edit).
    # 3. Construct cleared CanonicalTransaction: same source_tx_id='HOLD-FIXTURE-ID-1', hold=False,
    #    amount_minor=-12500, raw={"k":"v2"}, description='from importer', mcc=9999.
    # 4. Call insert_many. Assert returns (0, 1).
    # 5. Read back full row.
    # 6. Mutated fields (D-10): hold == False, amount_minor == -12500, raw_payload == {"k":"v2"}.
    # 7. FROZEN fields (D-10 invariant — THIS IS THE CENTRAL TEST):
    #     is_user_locked == True
    #     category_id == 42
    #     category_source == 'manual'
    #     description == 'user note'  # NOT 'from importer'
    #     mcc == 5411  # NOT 9999
    #     attributed_day == date(2026,5,1)
    #     time, currency, account_id, source_tx_id, created_at unchanged.
    # 8. Assert single row remains: SELECT count(*) FROM transactions WHERE source_tx_id='HOLD-FIXTURE-ID-1' == 1.


@pytest.mark.asyncio
async def test_e2e_hold_then_cleared(session_factory):
    """SC#3 end-to-end: hold:true insert, cleared:false follow-up via the same TransactionRepo path,
    using the JSON fixtures from 02-01. This is the integration anchor for the runner test in 02-03."""
    # 1. Load tests/fixtures/statement_with_hold.json (single-item array).
    # 2. Convert to CanonicalTransaction(hold=True, ...) inline (no MonobankImporter — that's 02-03).
    # 3. Seed account, insert (1, 0).
    # 4. Load tests/fixtures/statement_cleared_followup.json — same id, hold:false, different amount.
    # 5. Convert to CanonicalTransaction(hold=False, ...).
    # 6. insert_many returns (0, 1).
    # 7. Assert single row, hold=False, amount_minor matches the cleared payload.
```

**Critical fixture detail:** the fixtures from 02-01 are wrapped in a JSON array (Mono `/personal/statement` returns a list). Extract the first element with `json.load(open(...))[0]` and map fields:
- `id` → `source_tx_id`
- `time` (Unix seconds int) → `occurred_at = datetime.fromtimestamp(item["time"], tz=UTC)`
- `currencyCode` (numeric) → `currency = numeric_to_alpha(item["currencyCode"])` (import from `finance_bro.importers.currency_map`)
- `amount` → `amount_minor`
- `hold` → `hold`
- `description` → `description` (may be None)
- `mcc` → `mcc` (may be None)
- the full item dict → `raw`
- `source_account_id` is whatever account you seeded; pass it explicitly.

**3) Extend `tests/test_transactions_route.py`** — add `test_hold_field_in_response`.

Read the existing file first. Use Archetype C from PATTERNS.md (respx-mocked HTTP route test). The pattern: `_seed(client)` helper that inserts an account + transaction directly via SQL (no respx for THIS test — we're testing serialization, not Mono integration), then GET `/api/transactions`, assert the JSON shape.

```python
@pytest.mark.asyncio
async def test_hold_field_in_response(client, session_factory):
    """ING-05 D-12: TransactionOut.hold is present and reflects the DB column."""
    # Seed an account + two transactions: one hold=true, one hold=false.
    async with session_factory() as s:
        await s.execute(text("INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) VALUES ('mono.card', 'acct-A', 'UAH', '{}'::jsonb)"))
        await s.execute(text("""
            INSERT INTO transactions (account_id, source_tx_id, amount_minor, currency, time, raw_payload, hold)
            VALUES
              (1, 'tx-cleared', -100, 'UAH', now(), '{}'::jsonb, false),
              (1, 'tx-held',    -200, 'UAH', now(), '{}'::jsonb, true)
        """))
        await s.commit()

    r = await client.get("/api/transactions?account_id=1")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    by_id = {row["source_tx_id"]: row for row in rows}
    assert by_id["tx-cleared"]["hold"] is False
    assert by_id["tx-held"]["hold"] is True
    # Phase 1 fields still present (regression):
    assert "amount_minor" in by_id["tx-cleared"]
    assert isinstance(by_id["tx-cleared"]["amount_minor"], int)
```

If the existing transactions route doesn't accept `?account_id=` filter, omit the query param and just assert that both rows appear (the conftest TRUNCATE ensures isolation). Read `routes_transactions.py` first to confirm the actual API.
  </action>
  <verify>
    <automated>uv run pytest tests/test_hold_cleared_upsert.py tests/test_transactions_route.py -x &amp;&amp; uv run python -c "from finance_bro.api.schemas import TransactionOut, AccountOut; assert 'hold' in TransactionOut.model_fields; assert 'mono_type' in AccountOut.model_fields; print('schemas ok')" &amp;&amp; uv run pytest -x</automated>
  </verify>
  <done>All three new tests in test_hold_cleared_upsert.py pass; the FROZEN-fields assertion in test_cleared_updates_in_place is the central correctness gate (its failure means D-10 is violated); test_transactions_route.py::test_hold_field_in_response asserts both hold:true and hold:false rows; full suite is green.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Mono response → DB | `raw_payload` JSON written verbatim; this plan starts using `hold`/`description`/`mcc` from that payload. Type checks happen at the importer boundary (Mono returns these as documented JSON types). |
| Importer (later, Plan 02-03) → upsert | The DO UPDATE clause is the choke point: only three columns can be mutated by Mono, by SQL contract, regardless of what the importer sends. |
| User edits (Phases 4-6, future) → upsert | The frozen-by-omission rule is the privacy/integrity invariant: a future categorizer or manual-edit feature CANNOT have its writes overwritten by a Mono re-fetch. Test `test_cleared_updates_in_place` is the contract check. |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-02-04 | Tampering / Integrity | TransactionRepo.insert_many SET clause | mitigate | The SET clause is constrained to `hold`, `amount_minor`, `raw_payload` ONLY. Verified by grep gate in Task 1 verify (the `! grep -E '"description"\s*:\s*stmt\.excluded\|"mcc"...'` invariant). The central test `test_cleared_updates_in_place` exercises 6 frozen fields via a hold→cleared cycle; failure of any frozen-field assertion fails CI. **This is the strongest mitigation in Phase 2 — D-10 is enforced at the SQL layer, not by convention.** |
| T-02-06 | Information Disclosure | logging the upsert results at INFO | accept | The runner (Plan 02-03) logs `inserted` and `updated_in_place` counts only — no amount values, no payload bodies. Phase 1's structlog redaction already masks `amount`, `token`, `X-Token` substrings (`tests/test_log_redaction.py`). This plan introduces no new log keys at INFO. |
| T-02-07 | Tampering | xmax = 0 race during transaction rollback | accept | RESEARCH.md Pitfall 6: rows are returned to Python by the cursor before commit; rollback cannot retroactively change what was returned. Phase 1's existing pattern (`async with session.begin():` wraps every insert_many call) is unchanged; the caller commits before reading the counts. If a rollback happens, the caller never reads the counts. **Existing contract preserved.** |
| T-02-08 | Repudiation | hold→cleared transition has no audit trail | accept | D-13: explicitly no audit columns. The `import_runs.last_error` column (Plan 02-01) records which fetch produced which payload; combined with `raw_payload` as "the latest seen", this is enough for v1 debugging. Add a JSONB history array if a real Mono quirk surfaces — deferred to v1.5 per CONTEXT.md. |
</threat_model>

<verification>
**Plan-level checks (run before commit/handoff):**

1. `uv run pytest -x` — full suite green.
2. `grep -c "set_=" src/finance_bro/db/transaction_repo.py` returns `1`. Multiple `set_=` blocks would indicate scope leak (e.g., a separate code path mutating different fields).
3. `grep -E "set_={" -A 4 src/finance_bro/db/transaction_repo.py` returns ONLY the three lines for `hold`, `amount_minor`, `raw_payload`. No `description`, `mcc`, `is_user_locked`, etc. (D-10 invariant).
4. `grep -q "literal_column" src/finance_bro/db/transaction_repo.py` — xmax detection wired.
5. `grep -q "hold: bool" src/finance_bro/api/schemas.py` — TransactionOut field present.
6. `grep -q "mono_type: str | None" src/finance_bro/api/schemas.py` — AccountOut field present.
7. **Central correctness test:** `uv run pytest tests/test_hold_cleared_upsert.py::test_cleared_updates_in_place -v` — D-10 frozen-fields invariant.
8. **Phase 1 regression test:** `uv run pytest tests/test_idempotency.py tests/test_import_route.py tests/test_partial_unique_index.py -x` — must remain green.

**Sanity grep:** `grep -RE "(routes_status|routes_backfill|SchedulerRunner|MonoAuthError)" src/ | wc -l` should be `0`. None of those land here; if grep returns nonzero, scope leaked.
</verification>

<success_criteria>
- All Tasks' `<verify>` commands pass.
- The CENTRAL correctness test (`test_cleared_updates_in_place`) is green: the cleared payload mutates exactly `hold`, `amount_minor`, `raw_payload`, and the 6 manual-edit columns (is_user_locked, category_id, category_source, description, mcc, attributed_day) plus `currency`, `time`, `created_at` are untouched.
- `tests/test_hold_cleared_upsert.py::test_e2e_hold_then_cleared` proves the round trip via the new `ON CONFLICT DO UPDATE` path; this is the unit-level anchor for Plan 02-03's integration scheduler test.
- `GET /api/transactions` JSON shape now includes `hold: bool` per row; existing tests confirm Phase 1 fields untouched.
- `ImportService` continues to return a Phase 1-shaped `ImportResult` (Plan 02-04 will reshape this); `tests/test_import_route.py` passes UNCHANGED until 02-04.
- `must_haves.truths` verifiable: each truth has a passing assertion in `test_hold_cleared_upsert.py` or `test_transactions_route.py`.
</success_criteria>

<output>
After completion, create `.planning/phases/02-reliable-sync/02-02-SUMMARY.md` covering: the 3 mutated fields and the 12 frozen ones (call this out by name as the central D-10 invariant), the `xmax=0` detection, the `ImportService` call-site adapter (`inserted_total = inserted + updated`), and an explicit note that `routes_import.py` is intentionally untouched (Plan 02-04 owns D-16).
</output>
