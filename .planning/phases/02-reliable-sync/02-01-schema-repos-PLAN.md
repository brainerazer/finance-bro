---
phase: 02-reliable-sync
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - pyproject.toml
  - uv.lock
  - alembic/versions/0002_phase2_sync.py
  - src/finance_bro/db/models.py
  - src/finance_bro/db/import_run_repo.py
  - src/finance_bro/db/scheduler_state_repo.py
  - src/finance_bro/db/account_repo.py
  - tests/conftest.py
  - tests/fixtures/client_info_multi_card.json
  - tests/fixtures/statement_with_hold.json
  - tests/fixtures/statement_cleared_followup.json
  - tests/fixtures/statement_empty.json
  - tests/test_migrations.py
  - tests/test_import_run_repo.py
  - tests/test_scheduler_state_repo.py
autonomous: true
requirements:
  - ING-05
  - ING-06
  - ING-08
tags: [phase-02, schema, migration, repo, apscheduler-install]
context_warning: "13 files modified across 3 tasks; monitor executor context window during execution"

must_haves:
  truths:
    - "Migration 0002 upgrades cleanly from 0001 and downgrades cleanly back."
    - "After upgrade, accounts.mono_type is populated for every existing mono.card row from raw_payload->>'type'."
    - "After upgrade, scheduler_state has exactly one row (id=1, state='running')."
    - "After upgrade, import_runs is empty and accepts the (account_id, run_kind, status) shape from D-08."
    - "ImportRunRepo.claim_next_pending atomically transitions one pending row to in_flight, returning it."
    - "ImportRunRepo.recover_in_flight resets stale in_flight rows back to pending."
    - "SchedulerStateRepo.read returns the seeded singleton row; SchedulerStateRepo.write updates it."
    - "AccountRepo.list_pollable_cards returns only mono.card rows with mono_type IN ('black','platinum','white')."
    - "apscheduler==3.11.2 is installed and importable."
  artifacts:
    - path: "alembic/versions/0002_phase2_sync.py"
      provides: "Phase 2 migration: accounts.mono_type column, import_runs table, scheduler_state singleton table"
      contains: "import_runs"
    - path: "src/finance_bro/db/models.py"
      provides: "Account.mono_type field, ImportRun and SchedulerState ORM models"
      contains: "class ImportRun"
    - path: "src/finance_bro/db/import_run_repo.py"
      provides: "ImportRunRepo with claim_next_pending, enqueue_backfill, enqueue_live, mark_done, mark_error, recover_in_flight, count_pending_or_in_flight_backfill, last_live_per_account"
      exports: ["ImportRunRepo"]
    - path: "src/finance_bro/db/scheduler_state_repo.py"
      provides: "SchedulerStateRepo with read/write singleton helpers"
      exports: ["SchedulerStateRepo"]
    - path: "tests/fixtures/client_info_multi_card.json"
      provides: "4 cards with type ∈ {black, platinum, white, eAid} for round-robin/allowlist tests"
    - path: "tests/fixtures/statement_with_hold.json"
      provides: "Mono statementItem fixture with hold:true"
    - path: "tests/fixtures/statement_cleared_followup.json"
      provides: "same id as statement_with_hold but hold:false (possibly different amount)"
    - path: "tests/fixtures/statement_empty.json"
      provides: "[] payload for backfill chunks past Mono retention horizon"
  key_links:
    - from: "alembic/versions/0002_phase2_sync.py"
      to: "tests/test_migrations.py"
      via: "round-trip downgrade base -> upgrade head + table presence assertions"
      pattern: "import_runs"
    - from: "src/finance_bro/db/import_run_repo.py"
      to: "src/finance_bro/db/models.py::ImportRun"
      via: "imports + insert(ImportRun)/select(ImportRun)"
      pattern: "from finance_bro.db.models import ImportRun"
    - from: "src/finance_bro/db/scheduler_state_repo.py"
      to: "scheduler_state singleton row (id=1)"
      via: "raw text() UPDATE WHERE id=1"
      pattern: "WHERE id = 1"
    - from: "src/finance_bro/db/account_repo.py::list_pollable_cards"
      to: "accounts.mono_type column"
      via: "WHERE source_kind='mono.card' AND mono_type IN ('black','platinum','white')"
      pattern: "mono_type IN"
    - from: "tests/conftest.py"
      to: "TRUNCATE list"
      via: "extend the per-test truncation to cover import_runs and scheduler_state"
      pattern: "TRUNCATE TABLE"
---

<objective>
Lay the schema + repository foundation that every other Phase 2 plan reads from. After this plan lands, the DB has `accounts.mono_type`, `import_runs`, and `scheduler_state`; the ORM models reflect them; two new repos (`ImportRunRepo`, `SchedulerStateRepo`) provide the SQL the scheduler will call; `AccountRepo` gains the allowlist-aware `list_pollable_cards()`; and `apscheduler==3.11.2` is on disk so the runner module (Plan 02-03) imports cleanly.

This plan is the upstream dependency for 02-02 (hold-aware upsert calls into none of these directly, but its tests truncate the new tables), 02-03 (the SchedulerRunner uses every repo here), and 02-04 (the status surface + force-poll endpoint compose joins over `import_runs` and `accounts.mono_type`).

Purpose: unblock parallel work on the upsert (02-02), the runner (02-03), and the API surface (02-04).
Output: One Alembic migration, three modified DB modules, two new DB modules, four test fixtures, two new repo unit-tests, conftest extension, dependency install — all green under `uv run pytest -x`.
</objective>

<phase_goal>
Bohdan stops clicking import. The app polls Mono on its own at the rate-limit budget, ingests holds correctly (and updates them in place when they clear), can backfill 12 months on first connect, and surfaces "last poll N min ago" plus 401/429 distinctly so silent failures are impossible.
</phase_goal>

<plan_scope>
**Delivers:**
- `apscheduler==3.11.2` added to `pyproject.toml`/`uv.lock` (the only new top-level dep).
- Alembic revision `0002_phase2_sync.py` per RESEARCH.md Code Examples §5: adds `accounts.mono_type`, backfills it from `raw_payload->>'type'`, creates `scheduler_state` singleton (CHECK id=1) seeded with `(1,'running')`, creates `import_runs` (D-08 shape) with `(account_id, run_kind)` and `(status, created_at)` indexes (Pitfall 5).
- `src/finance_bro/db/models.py` gains `Account.mono_type` (nullable Text), `ImportRun`, `SchedulerState`.
- `src/finance_bro/db/import_run_repo.py` (NEW) — methods listed in must_haves.artifacts.
- `src/finance_bro/db/scheduler_state_repo.py` (NEW) — `read()` / `write()` against the seeded singleton.
- `src/finance_bro/db/account_repo.py` gains `list_pollable_cards()` per D-01 + D-02 (filter + ORDER BY id ASC).
- Test fixtures: `client_info_multi_card.json`, `statement_with_hold.json`, `statement_cleared_followup.json`, `statement_empty.json`.
- `tests/conftest.py` extended to truncate the two new tables in the per-test fixture (so ordering invariants hold across HTTP-route tests in 02-03/02-04).
- `tests/test_migrations.py` extended to assert presence of the new tables and the singleton seed after upgrade-head.
- `tests/test_import_run_repo.py` (NEW) — claim atomicity, recovery sweep, enqueue+complete round trip.
- `tests/test_scheduler_state_repo.py` (NEW) — read singleton, write singleton, UPDATE-only invariant (no INSERT path needed).

**Does NOT deliver (in this plan):**
- The `ON CONFLICT DO UPDATE` swap in `TransactionRepo.insert_many` (that's 02-02 — D-10).
- The `SchedulerRunner` class or the lifespan integration (that's 02-03).
- Any HTTP route changes (those are 02-03 lifespan + 02-04 status/backfill/force-poll).
- The `MonobankImporter` extension to extract `mono_type` (also 02-03 — keeps importer changes co-located with typed-error work).
- The `CanonicalTransaction.hold` / `description` / `mcc` field additions (02-02 owns those because the upsert is the only consumer).

**Why this slice is end-to-end testable on its own:** the migration round-trips, the repos have unit tests against testcontainers Postgres, conftest stays green for every existing Phase 1 test (regression). No HTTP behavior changes; everything is below the API.
</plan_scope>

<plan_dependencies>
- **None.** This is Wave 1 of Phase 2 and the foundation for all subsequent plans.
- Downstream:
  - `02-02-hold-aware-upsert-PLAN.md` — independent of this plan's modules but its conftest assumes the truncate list this plan extends; must land after 02-01 to avoid leftover `import_runs` rows from prior tests bleeding in.
  - `02-03-scheduler-backfill-PLAN.md` — imports `ImportRun`, `SchedulerState`, `ImportRunRepo`, `SchedulerStateRepo`, `AccountRepo.list_pollable_cards`. Hard-blocked.
  - `02-04-status-surface-PLAN.md` — joins over `import_runs`, reads `accounts.mono_type`, instantiates `ImportRunRepo`/`SchedulerStateRepo`. Hard-blocked.
</plan_dependencies>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/REQUIREMENTS.md
@.planning/ROADMAP.md
@.planning/STATE.md
@.planning/phases/02-reliable-sync/02-CONTEXT.md
@.planning/phases/02-reliable-sync/02-RESEARCH.md
@.planning/phases/02-reliable-sync/02-VALIDATION.md
@.planning/phases/02-reliable-sync/02-PATTERNS.md
@.planning/phases/01-first-real-transaction/01-04-SUMMARY.md
@CLAUDE.md
@src/finance_bro/db/models.py
@src/finance_bro/db/account_repo.py
@src/finance_bro/db/transaction_repo.py
@src/finance_bro/db/rate_state_repo.py
@alembic/versions/0001_walking_skeleton.py
@tests/conftest.py
@tests/test_migrations.py
@tests/test_partial_unique_index.py

<interfaces>
<!-- Key types/contracts the executor will consume or extend. Extracted from the codebase so no exploration is needed. -->

From `src/finance_bro/db/models.py` (current Phase 1 shape):
```python
class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (UniqueConstraint("source_kind", "source_account_id", name="uq_accounts_source"),)

class MonoRateState(Base):  # singleton-style analog Phase 2 mirrors for SchedulerState
    __tablename__ = "mono_rate_state"
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    last_acquired_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
```

From `src/finance_bro/db/account_repo.py` (current shape — extend, do NOT replace):
```python
class AccountRepo:
    def __init__(self, session: AsyncSession) -> None: ...
    async def list_all(self) -> list[Account]: ...
    async def get_first_card(self) -> Account | None: ...   # Phase 1 — KEEP, do not delete (still used by Phase 1 tests)
    async def upsert_many(self, items: list[CanonicalAccount]) -> int: ...
```

From `src/finance_bro/db/rate_state_repo.py` (the singleton-row analog for SchedulerStateRepo — copy the raw `text()` idiom):
```python
class RateStateRepo:
    def __init__(self, session: AsyncSession) -> None: ...
    async def ensure_row(self, token_hash, last_acquired_at): ...   # uses text("INSERT ... ON CONFLICT DO NOTHING")
    async def select_for_update(self, token_hash) -> datetime | None: ...   # raw text("SELECT ... FOR UPDATE")
    async def upsert(self, token_hash, last_acquired_at): ...   # raw text("INSERT ... ON CONFLICT (token_hash) DO UPDATE")
```

From `tests/conftest.py` (current `client` fixture truncate line — extend the table list):
```python
await s.execute(
    text("TRUNCATE TABLE transactions, accounts, mono_rate_state RESTART IDENTITY CASCADE")
)
```
After this plan: `TRUNCATE TABLE transactions, import_runs, accounts, scheduler_state, mono_rate_state RESTART IDENTITY CASCADE`. **Order matters:** child tables (`transactions`, `import_runs`) before parent (`accounts`); `RESTART IDENTITY CASCADE` keeps it safe regardless. Re-seed the `scheduler_state` row after truncate so the singleton invariant holds for tests that read it.

From `alembic/versions/0001_walking_skeleton.py` (header pattern to mirror in 0002):
```python
revision: str = "0001"
down_revision: str | None = None
# ...
def upgrade() -> None:
    op.create_table("accounts", ...)   # see file for the full DDL pattern
def downgrade() -> None:
    op.drop_table("mono_rate_state")
    op.drop_index("uq_transactions_account_source_tx", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("accounts")
```

From `02-RESEARCH.md` Code Examples §5 (lines 957-1032) — the *verbatim* migration body. Do not deviate.

From `02-PATTERNS.md` lines 681-727 — the verbatim ORM model additions. Do not deviate from `Mapped[...]` typing.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Wave 0 — install apscheduler, create fixtures, scaffold repo tests, extend conftest</name>
  <files>
    pyproject.toml,
    uv.lock,
    tests/conftest.py,
    tests/fixtures/client_info_multi_card.json,
    tests/fixtures/statement_with_hold.json,
    tests/fixtures/statement_cleared_followup.json,
    tests/fixtures/statement_empty.json,
    tests/test_import_run_repo.py,
    tests/test_scheduler_state_repo.py
  </files>
  <action>
**1) Install APScheduler 3.11.2 (per D-03 + RESEARCH.md Standard Stack):**
```bash
uv add apscheduler==3.11.2
```
Verify it pinned exactly (`grep '"apscheduler' pyproject.toml`) and that `uv.lock` updated. Do NOT add any other dep — RESEARCH.md confirms zero other new deps for the whole phase.

**2) Create test fixtures (Wave 0 for VALIDATION.md test files):**

`tests/fixtures/client_info_multi_card.json` — Mono `/personal/client-info` shape with FOUR cards covering the allowlist test surface (D-01, Pitfall 10). Use realistic shape based on `tests/fixtures/client_info_minimal.json` (Phase 1 already has this — read it first to copy structure exactly). Required cards in this order so `ORDER BY id ASC` exercises the round-robin (D-02):
  - card[0]: `type: "eAid"` (the empirical landmine — test asserts the scheduler skips it)
  - card[1]: `type: "black"`, currency 840 (USD)
  - card[2]: `type: "platinum"`, currency 980 (UAH)
  - card[3]: `type: "white"`, currency 980 (UAH)
Each card needs `id` (24-char hash-style string), `currencyCode`, `cashbackType`, `balance`, `creditLimit`, `maskedPan`, `iban`, `type`. NO `jars` array, NO `fop` accounts (Phase 1 empirics confirm Bohdan has neither).

`tests/fixtures/statement_with_hold.json` — Mono `/personal/statement/...` returning ONE item with `hold: true`. Mirror the row shape from `tests/fixtures/statement_two_items.json` (Phase 1 already has this; read it). The item needs: `id` (use `"HOLD-FIXTURE-ID-1"` literal — used as `source_tx_id` in 02-02's hold→cleared test), `time` (a Unix-second integer ~now-2h), `description`, `mcc`, `amount` (a negative integer, e.g. `-12345` minor units), `currencyCode: 980`, `commissionRate: 0`, `cashbackAmount: 0`, `balance: 100000`, `hold: true`.

`tests/fixtures/statement_cleared_followup.json` — SAME `id` as above (`"HOLD-FIXTURE-ID-1"`), `hold: false`, `amount: -12500` (different so 02-02 can assert the upsert mutates it), all other fields slightly different (different `balance`, etc.) so the raw_payload comparison can prove the cleared version overwrites the hold version (D-11). Same `time` is fine — the upsert must NOT mutate `time` (D-10 frozen field).

`tests/fixtures/statement_empty.json` — literally `[]`. Used by 02-03's `test_4xx_marks_error_not_skip` neighbour test for the "empty window" path (Pitfall 3 — empty `[]` is "no transactions in window", distinct from 4xx).

**3) Extend `tests/conftest.py` `client` fixture truncate:**

Locate the line in the `client` fixture:
```python
await s.execute(
    text("TRUNCATE TABLE transactions, accounts, mono_rate_state RESTART IDENTITY CASCADE")
)
```
Replace with:
```python
await s.execute(
    text(
        "TRUNCATE TABLE transactions, import_runs, accounts, "
        "scheduler_state, mono_rate_state RESTART IDENTITY CASCADE"
    )
)
# Re-seed scheduler_state singleton (migration 0002 seeds it but TRUNCATE wipes it).
await s.execute(text("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')"))
await s.commit()
```
Per D-15 + RESEARCH.md Pattern 5: scheduler_state must always have the id=1 row; tests must restore it after truncation.

**4) Scaffold repo unit tests (Wave 0 — failing tests are fine; this plan's Tasks 2-3 implement the repos):**

`tests/test_import_run_repo.py` — write the test functions (`async def test_...`) but leave their bodies as `pytest.fail("TODO Task 3")` for now. Required test names (each maps to a behavior in must_haves.truths):
  - `test_enqueue_backfill_creates_twelve_pending_rows`
  - `test_enqueue_live_creates_one_pending_row`
  - `test_claim_next_pending_returns_oldest_and_marks_in_flight`
  - `test_claim_next_pending_returns_none_when_empty`
  - `test_mark_done_records_counts_and_completed_at`
  - `test_mark_error_sets_status_and_last_error`
  - `test_recover_in_flight_resets_stale_rows`
  - `test_count_pending_or_in_flight_backfill_returns_count`

`tests/test_scheduler_state_repo.py`:
  - `test_read_returns_seeded_singleton`
  - `test_write_updates_state_and_last_error_and_since`
  - `test_write_does_not_create_second_row` (CHECK constraint enforces, but test the contract)

Each test uses `session_factory` fixture (Archetype B from PATTERNS.md). Imports: `import pytest, from sqlalchemy import text`.

The bodies will be filled in Task 3 once the repos exist. Wave 0 here just creates the file scaffolding so the executor in Task 3 has named slots to fill.
  </action>
  <verify>
    <automated>uv run python -c "import apscheduler; print(apscheduler.__version__)" | grep -q '3.11.2' &amp;&amp; ls tests/fixtures/client_info_multi_card.json tests/fixtures/statement_with_hold.json tests/fixtures/statement_cleared_followup.json tests/fixtures/statement_empty.json &amp;&amp; uv run python -c "import json; [json.load(open(f)) for f in ['tests/fixtures/client_info_multi_card.json','tests/fixtures/statement_with_hold.json','tests/fixtures/statement_cleared_followup.json','tests/fixtures/statement_empty.json']]" &amp;&amp; grep -q "import_runs" tests/conftest.py &amp;&amp; grep -q "scheduler_state" tests/conftest.py &amp;&amp; uv run python -m py_compile tests/test_import_run_repo.py tests/test_scheduler_state_repo.py &amp;&amp; uv run pytest tests/test_health.py tests/test_no_auth.py -x</automated>
  </verify>
  <done>apscheduler 3.11.2 is in pyproject.toml and uv.lock; all four fixtures exist and are valid JSON; client_info_multi_card.json contains exactly one card per allowlist type plus eAid; statement_with_hold/cleared share the same `id` value `"HOLD-FIXTURE-ID-1"`; conftest's TRUNCATE includes both new tables and re-seeds scheduler_state; the two new repo test files exist with the required test names (failing); all PRE-EXISTING Phase 1 tests still pass (sanity — conftest change must not break Phase 1).</done>
</task>

<task type="auto">
  <name>Task 2: Alembic 0002 + ORM models for ImportRun, SchedulerState, Account.mono_type</name>
  <files>
    alembic/versions/0002_phase2_sync.py,
    src/finance_bro/db/models.py,
    tests/test_migrations.py
  </files>
  <action>
**Reference:** RESEARCH.md Code Examples §5 (lines 957-1032) — verbatim migration body. Implement EXACTLY that. No extra columns, no extra constraints, no extra indexes beyond what's specified.

**1) Create `alembic/versions/0002_phase2_sync.py`:**

Header (mirror `0001_walking_skeleton.py` lines 1-16 — same imports, same module-level constants, with `revision = "0002"` and `down_revision = "0001"`).

`upgrade()` body — verbatim from RESEARCH.md Code Examples §5 lines 964-1024. The order is:
1. `op.add_column("accounts", sa.Column("mono_type", sa.Text, nullable=True))`
2. `op.execute("UPDATE accounts SET mono_type = raw_payload->>'type' WHERE source_kind = 'mono.card'")` — Pitfall 7 mitigation. **Do NOT skip this**: existing Phase 1 rows have `mono_type=NULL` until populated, and the scheduler's allowlist filter would silently exclude every card without it.
3. `op.create_table("scheduler_state", ...)` with:
   - `id INTEGER PK` (NOT BigInteger — singleton; mirrors `MonoRateState` pattern)
   - `state TEXT NOT NULL DEFAULT 'running'`
   - `last_error TEXT NULL`
   - `since TIMESTAMP(timezone=True) NOT NULL DEFAULT now()`
   - `CheckConstraint("id = 1", name="ck_scheduler_state_singleton")`
   - `CheckConstraint("state IN ('running','stopped','auth_failed')", name="ck_scheduler_state_state")`
4. `op.execute("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')")` — seed; idempotent because re-running migrations resets the schema.
5. `op.create_table("import_runs", ...)` with the columns from D-08 (id BigInteger PK autoincrement, account_id BigInteger FK accounts.id ON DELETE RESTRICT NOT NULL, run_kind Text NN, window_from/window_to TIMESTAMP(tz) NN, status Text NN default 'pending', last_error Text, attempts Integer NN default 0, statement_count Integer, inserted Integer, started_at TIMESTAMP(tz), completed_at TIMESTAMP(tz), created_at TIMESTAMP(tz) NN default now()) plus `CheckConstraint("run_kind IN ('backfill','live')", name="ck_import_runs_run_kind")` and `CheckConstraint("status IN ('pending','in_flight','done','error')", name="ck_import_runs_status")`.
6. `op.create_index("ix_import_runs_account_kind_completed", "import_runs", ["account_id","run_kind"], postgresql_using="btree")` — Pitfall 5 mitigation for the status-page join.
7. `op.create_index("ix_import_runs_status_created", "import_runs", ["status","created_at"], postgresql_using="btree")` — for `claim_next_pending`'s `WHERE status='pending' ORDER BY created_at ASC`.

`downgrade()` body — drop in reverse order: indexes, import_runs, scheduler_state, mono_type column. Mirror `0001_walking_skeleton.py` lines 87-91.

**Anti-pattern callout (RESEARCH.md):** Do NOT add a partial unique index, advisory lock, or SKIP LOCKED preamble. Single-consumer dequeue (max_instances=1) makes them unnecessary.

**2) Extend `src/finance_bro/db/models.py`:**

Verbatim from PATTERNS.md lines 681-727 (and RESEARCH.md Pattern 6 / Code Examples §5):

(a) `Account` — add a single line after `raw_payload` line 32:
```python
mono_type: Mapped[str | None] = mapped_column(Text, nullable=True)
```
Place BEFORE `created_at` so the column ordering matches the migration. Do NOT touch `__table_args__`.

(b) Append `class ImportRun(Base):` per PATTERNS.md lines 686-707. Required `__tablename__ = "import_runs"`. Use `BigInteger` for `id` and `account_id`, `Integer` for `attempts`/`statement_count`/`inserted`. **Do NOT** add `__table_args__` — the migration owns the FK, indexes, and CHECKs; the Phase 1 convention (per `Transaction`) is to declare indexes only when SQLAlchemy itself emits them, which is not the case here.

(c) Append `class SchedulerState(Base):` per PATTERNS.md lines 712-723. `id: Mapped[int] = mapped_column(Integer, primary_key=True)` — primary key without autoincrement (singleton seeded by migration).

**Imports to add at the top of `models.py`:** none new — `BigInteger`, `Integer`, `Text`, `TIMESTAMP`, `ForeignKey`, `text` are already imported.

**3) Extend `tests/test_migrations.py`:**

Read the existing `test_round_trip` test first to learn the pattern. Then add (or extend the existing test) so that AFTER `upgrade head`:
- Assert the existence of all three new schema objects via `pg_catalog`:
  - `SELECT 1 FROM information_schema.columns WHERE table_name='accounts' AND column_name='mono_type'` → 1 row
  - `SELECT 1 FROM information_schema.tables WHERE table_name='import_runs'` → 1 row
  - `SELECT 1 FROM information_schema.tables WHERE table_name='scheduler_state'` → 1 row
  - `SELECT state FROM scheduler_state WHERE id=1` → returns `"running"`
- Assert `downgrade base` then `upgrade head` (round-trip) leaves no orphans.

Use the same `asyncio.to_thread(run_alembic, cfg, "...")` pattern from `conftest.py` `pg_url` fixture.

**Caveat for round-trip test:** if `tests/test_migrations.py::test_round_trip` already runs `downgrade base → upgrade head`, simply add the new assertions after the upgrade pass; do not duplicate the migration drive.
  </action>
  <verify>
    <automated>uv run pytest tests/test_migrations.py -x &amp;&amp; uv run python -c "from finance_bro.db.models import ImportRun, SchedulerState, Account; assert hasattr(Account, 'mono_type'); print('models ok')" &amp;&amp; grep -c "import_runs" alembic/versions/0002_phase2_sync.py | grep -qE '[1-9]' &amp;&amp; grep -q "ck_scheduler_state_singleton" alembic/versions/0002_phase2_sync.py &amp;&amp; grep -q "ix_import_runs_status_created" alembic/versions/0002_phase2_sync.py</automated>
  </verify>
  <done>Migration 0002 round-trips cleanly (downgrade base + upgrade head); after upgrade, accounts has the mono_type column, scheduler_state and import_runs tables exist, scheduler_state has exactly one row with state='running'; ORM models import without error and `Account`/`ImportRun`/`SchedulerState` are all subclasses of `Base`.</done>
</task>

<task type="auto">
  <name>Task 3: ImportRunRepo + SchedulerStateRepo + AccountRepo.list_pollable_cards + fill repo tests</name>
  <files>
    src/finance_bro/db/import_run_repo.py,
    src/finance_bro/db/scheduler_state_repo.py,
    src/finance_bro/db/account_repo.py,
    tests/test_import_run_repo.py,
    tests/test_scheduler_state_repo.py
  </files>
  <action>
**1) Create `src/finance_bro/db/import_run_repo.py`** — RESEARCH.md Pattern 2 + PATTERNS.md lines 222-253 give the verbatim shape. Module docstring: "ImportRunRepo — claim/enqueue/audit for the scheduler. Single tick consumer per D-03 means no SKIP LOCKED needed (RESEARCH.md Pattern 2)."

Class shape (mirror TransactionRepo lines 19-21):
```python
class ImportRunRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
```

Methods (signatures and minimal SQL — RESEARCH.md Code Examples §3 + PATTERNS.md lines 244-251):

```python
async def claim_next_pending(self) -> ImportRun | None:
    # Verbatim from RESEARCH.md Pattern 2 (lines 379-398).
    # UPDATE import_runs SET status='in_flight', started_at=now(), attempts=attempts+1
    # WHERE id = (SELECT id FROM import_runs WHERE status='pending' ORDER BY created_at ASC LIMIT 1)
    # RETURNING *
    # Hydrate ImportRun from result.mappings().one_or_none()
```

```python
async def enqueue_backfill(self, account_id: int, chunks: list[tuple[datetime, datetime]]) -> list[int]:
    # Bulk insert N rows with run_kind='backfill', status='pending'.
    # Use SA insert(ImportRun).values([...]).returning(ImportRun.id) — same idiom as
    # TransactionRepo.insert_many lines 45-53 but DO NOTHING is irrelevant here (no conflict).
    # Return the inserted ids in the order they were created.
```

```python
async def enqueue_live(self, account_id: int, window_from: datetime, window_to: datetime) -> int:
    # Single insert with run_kind='live', status='pending'. Returns the new row id.
```

```python
async def mark_done(self, run_id: int, statement_count: int, inserted: int, updated: int) -> None:
    # UPDATE import_runs SET status='done', completed_at=now(),
    #     statement_count=:c, inserted=:i, last_error=NULL
    # WHERE id=:id
    # Note: `inserted` column stores both inserted+updated for status surface (RESEARCH.md
    # Code Examples §4 reads `ll.inserted AS last_poll_inserted`). The UPDATED count is logged
    # but not separately persisted — D-17 says no extra audit columns. Discretion: store
    # inserted as the user-meaningful number (`inserted+updated` = "rows touched"), or
    # store `inserted` only and ignore `updated` for status. **Choose: store `inserted` ONLY
    # in the column** (matches D-08 column name; the SchedulerRunner can pass either via the
    # arg). The `updated` arg is passed through and used by the runner's structlog line per
    # RESEARCH.md Code Examples §3 line 891 (`updated_in_place=updated`) but does not land
    # in the DB. Document this in the docstring.
```

```python
async def mark_error(self, run_id: int, error: str) -> None:
    # UPDATE import_runs SET status='error', completed_at=now(), last_error=:err WHERE id=:id
```

```python
async def recover_in_flight(self, threshold_seconds: int = 300) -> int:
    # RESEARCH.md Pattern 7 (lines 596-614) verbatim.
    # UPDATE import_runs SET status='pending', started_at=NULL
    # WHERE status='in_flight' AND started_at < now() - make_interval(secs => :s)
    # RETURNING id
    # Returns the count of rows reset.
```

```python
async def count_pending_or_in_flight_backfill(self, account_id: int) -> int:
    # SELECT count(*) FROM import_runs WHERE account_id=:id AND run_kind='backfill'
    #   AND status IN ('pending','in_flight')
    # For D-06 — the runner uses this to skip live polling for an account whose backfill
    # is in progress.
```

```python
async def last_live_per_account(self) -> dict[int, ImportRun]:
    # SELECT DISTINCT ON (account_id) ... FROM import_runs WHERE run_kind='live'
    #   ORDER BY account_id, completed_at DESC NULLS LAST
    # Returns {account_id: ImportRun}.
    # Used by the runner's pick_next_active_card (oldest last-poll wins) AND by
    # the status surface route (02-04). Single query for both paths.
```

**Imports for the file:** `from datetime import datetime`; `from sqlalchemy import insert as sa_insert, select, text`; `from sqlalchemy.dialects.postgresql import insert`; `from sqlalchemy.ext.asyncio import AsyncSession`; `from finance_bro.db.models import ImportRun`. (Phase 1's `transaction_repo.py` line 11-16 is the import template.)

**2) Create `src/finance_bro/db/scheduler_state_repo.py`** — verbatim from PATTERNS.md lines 257-302.

```python
"""SchedulerStateRepo — single owner of writes against the scheduler_state singleton.

The id=1 row is seeded by migration 0002 and protected by a CHECK constraint
(D-15 + RESEARCH.md Pattern 5). Reads come from process-cached state in the
runner; writes happen on 401-detection only."""

from datetime import datetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class SchedulerStateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def read(self) -> tuple[str, str | None, datetime] | None:
        row = (
            await self._s.execute(
                text("SELECT state, last_error, since FROM scheduler_state WHERE id = 1")
            )
        ).first()
        return (row[0], row[1], row[2]) if row else None

    async def write(self, state: str, last_error: str | None) -> None:
        await self._s.execute(
            text(
                "UPDATE scheduler_state "
                "SET state = :state, last_error = :err, since = now() "
                "WHERE id = 1"
            ),
            {"state": state, "err": last_error},
        )
```

No INSERT path — singleton is migration-seeded. The CHECK constraint already prevents id != 1 inserts.

**3) Extend `src/finance_bro/db/account_repo.py`:**

Add `list_pollable_cards()` method AFTER `get_first_card()` (do NOT delete `get_first_card` — it remains for Phase 1's `routes_transactions` `_pick_first_card_id` path; check whether that path still references it before assuming you can drop it. **Do not drop it in this plan.**).

```python
async def list_pollable_cards(self) -> list[Account]:
    """Active polling set per D-01 + D-02: mono.card with type ∈ {black, platinum, white},
    ordered by id ASC for deterministic round-robin (D-02). Fail-closed: any other
    mono_type (eAid, future iron/yellow/etc.) is excluded."""
    rows = (
        await self._s.execute(
            select(Account)
            .where(Account.source_kind == "mono.card")
            .where(Account.mono_type.in_(["black", "platinum", "white"]))
            .order_by(Account.id.asc())
        )
    ).scalars().all()
    return list(rows)
```

**3.5) Wire `mono_type` into `AccountRepo.upsert_many` (BLOCKER fix — bridge between 02-01 T2 schema and 02-03 T1 importer):**

- Update `AccountRepo.upsert_many`'s row-building dict to include `"mono_type": item.mono_type`. This is the wiring point between `CanonicalAccount.mono_type` (set by `MonobankImporter.discover_accounts` in plan 02-03) and the new `accounts.mono_type` column (added in T2 migration). Without this, freshly discovered accounts on first boot land with `mono_type=NULL`, silently fail the allowlist `WHERE mono_type IN ('black','platinum','white')`, and never enter the poll rotation — SC#1 breaks for fresh installs.

The dict construction in `upsert_many` becomes:
```python
rows = [
    {
        "source_kind": a.source_kind,
        "source_account_id": a.source_account_id,
        "currency": a.currency,
        "raw_payload": a.raw,
        "mono_type": a.mono_type,   # NEW — wired in 02-01 T3 to bridge 02-01 T2 schema + 02-03 T1 importer
    }
    for a in items
]
```

Note: `CanonicalAccount.mono_type` is added in plan 02-03 Task 1; until that lands, the attribute access raises AttributeError. **Per the wave structure (02-01 → 02-03), 02-01 must complete first.** That means 02-01 Task 3 lands the `"mono_type": a.mono_type` line BEFORE `CanonicalAccount.mono_type` exists. Mitigation: in 02-01 Task 3 use `getattr(a, "mono_type", None)` so the upsert continues to work pre-02-03 (every existing call site instantiates `CanonicalAccount` without the field; `None` is the safe value, the migration backfill in T2 already populated existing rows from `raw_payload->>'type'`). 02-03 Task 1 will then add the field, and the `getattr` continues to work — but if the executor wants the cleaner `a.mono_type` form, leave a follow-up note in 02-01 SUMMARY.md to swap once 02-03 lands. **Choose the `getattr` form for 02-01.**

**4) Fill the repo unit tests** scaffolded in Task 1.

`tests/test_import_run_repo.py` — for each test scaffolded:
- `test_enqueue_backfill_creates_twelve_pending_rows`: seed an account row via raw `text("INSERT INTO accounts...")` mirroring `tests/test_partial_unique_index.py` lines 22-40; call `repo.enqueue_backfill(account_id, [(datetime, datetime)] * 12)`; assert `SELECT count(*) FROM import_runs WHERE account_id=:id AND run_kind='backfill' AND status='pending'` returns 12.
- `test_enqueue_live_creates_one_pending_row`: similar; `repo.enqueue_live(...)` returns int; assert one row, run_kind='live'.
- `test_claim_next_pending_returns_oldest_and_marks_in_flight`: enqueue 3 pending rows; call claim once; assert returned `id` is the oldest by created_at; assert that row's status is now `in_flight` and `attempts = 1`.
- `test_claim_next_pending_returns_none_when_empty`: assert None.
- `test_mark_done_records_counts_and_completed_at`: enqueue+claim, then mark_done; assert status, statement_count, inserted, completed_at.
- `test_mark_error_sets_status_and_last_error`: similar with 'error'.
- `test_recover_in_flight_resets_stale_rows`: insert a row directly with `status='in_flight'` and `started_at = now() - interval '6 minutes'`; call `recover_in_flight(threshold_seconds=300)`; assert it's now `pending`, `started_at IS NULL`. Insert a fresh `in_flight` row with `started_at = now() - interval '1 minute'`; assert it stays `in_flight`.
- `test_count_pending_or_in_flight_backfill_returns_count`: mixed backfill+live, mixed statuses; assert exact count.

`tests/test_scheduler_state_repo.py`:
- `test_read_returns_seeded_singleton`: open session, instantiate repo, assert `(state, last_error, since)` returned with state='running' (seeded by migration). The conftest re-seed in Task 1 ensures this works after TRUNCATE too.
- `test_write_updates_state_and_last_error_and_since`: write `('auth_failed', 'token revoked (401)')`; read back; assert state, last_error, since-is-recent.
- `test_write_does_not_create_second_row`: confirm `SELECT count(*) FROM scheduler_state` is exactly 1 after several writes.

**Critical fixture detail:** every test that touches `scheduler_state` must commit/rollback within `session.begin()` blocks (mirror `services/import_service.py` pattern); each test uses its own session from `session_factory`. After test, the session_factory is torn down; per-test isolation is achieved by the conftest TRUNCATE+reseed in Task 1.
  </action>
  <verify>
    <automated>uv run pytest tests/test_import_run_repo.py tests/test_scheduler_state_repo.py -x &amp;&amp; uv run python -c "from finance_bro.db.import_run_repo import ImportRunRepo; from finance_bro.db.scheduler_state_repo import SchedulerStateRepo; from finance_bro.db.account_repo import AccountRepo; assert hasattr(AccountRepo, 'list_pollable_cards'); print('repos ok')" &amp;&amp; (grep -E '"mono_type":\s*(a\.mono_type|getattr\(a, *"mono_type"' src/finance_bro/db/account_repo.py || (echo "FAIL: AccountRepo.upsert_many missing mono_type wiring" &amp;&amp; exit 1)) &amp;&amp; uv run pytest tests/test_partial_unique_index.py tests/test_idempotency.py tests/test_rate_limit_gate.py tests/test_log_redaction.py -x</automated>
  </verify>
  <done>All scaffolded repo tests pass against testcontainers Postgres; `list_pollable_cards` honors the D-01 allowlist and D-02 ordering; AccountRepo.upsert_many writes mono_type from CanonicalAccount on insert (using `getattr(a, "mono_type", None)` to stay compatible with pre-02-03 callers); existing accounts retain backfilled value; SchedulerStateRepo cannot create a second row (CHECK enforced); Phase 1 invariants (partial unique index, idempotency, rate gate, log redaction) still pass — plan is non-regressive.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Network → app | Tailscale/LAN-only; no app-level auth (DEP-02 — Phase 1 invariant). No new boundary in this plan. |
| App → Postgres | Same connection pool as Phase 1; same env-only credentials. |
| Migration → existing data | New: 0002 mutates `accounts` rows in production. The UPDATE SET `mono_type = raw_payload->>'type'` reads attacker-controlled JSON (Mono response originally). |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-02-01 | Information Disclosure | structlog redaction (Phase 1 invariant) when 0002 logs migration progress | accept | Migration uses `op.execute()` which prints raw SQL via Alembic's logger (NOT structlog). The SQL contains no token, no amount values, only column/table identifiers. Verify by reading migration output in Task 2's verify step. |
| T-02-02 | Tampering | `accounts.mono_type` populated from `raw_payload->>'type'` | mitigate | The `raw_payload` JSONB came from Mono's TLS-authenticated response in Phase 1 — already trusted-input-shape; the new column inherits that trust level. The `text(...)` migration uses parameterized JSON access (`->>` operator), not string concatenation, so SQLi is structurally impossible. |
| T-02-03 | Denial of Service | unbounded `import_runs` growth | accept | Pitfall 5: index `(status, created_at)` is added so `claim_next_pending` stays O(log n). 90-day pruning is deferred to Phase 7. At ~360 rows/day this plan ships well within limits for the v1 horizon. |
| T-02-04 | Repudiation | scheduler_state singleton — auth_failed transition is the auditable event | mitigate | `since TIMESTAMPTZ` column captures the transition time; `last_error` carries the message. Written ONLY by the runner's 401 path (Plan 02-03 owns that write); this plan's repo just provides the seam. Singleton CHECK + UPDATE-only repo prevents history loss via accidental reseed. |
| T-02-05 | Elevation of Privilege | Migration grants no new roles | accept | 0002 creates tables under the same default-grant model as 0001 (single Postgres user owns everything; no GRANT statements). DEP-02 trust boundary unchanged. |
</threat_model>

<verification>
**Plan-level checks (run before commit/handoff):**

1. `uv run pytest -x` — full suite green. Phase 1 tests must not regress (CONFTEST changes are the only risk surface).
2. `uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head` (against the testcontainers Postgres via `tests/test_migrations.py`) — round-trip clean.
3. `grep -c "import_runs" alembic/versions/0002_phase2_sync.py` ≥ 1 — migration mentions the new table.
4. `grep -E "set_=" src/finance_bro/db/transaction_repo.py | wc -l` — should be `0`. This plan does NOT touch the upsert clause; that's 02-02. If grep returns nonzero, scope leaked.
5. `grep -E "(jsonbase|SKIP LOCKED|advisory_lock)" src/finance_bro/db/import_run_repo.py | wc -l` — should be `0` (anti-patterns called out by RESEARCH.md).
6. `uv run python -c "import apscheduler; from apscheduler.schedulers.asyncio import AsyncIOScheduler; from apscheduler.triggers.interval import IntervalTrigger"` — proves the dep is importable in the venv (necessary for Plan 02-03).

**Sanity grep:** `grep -RE "(test_scheduler_round_robin|test_backfill_enqueue|test_hold_cleared)" tests/ | wc -l` — should be `0`. Those tests are owned by 02-02/02-03; do not preempt.
</verification>

<success_criteria>
- All three Tasks' `<verify>` commands pass against testcontainers Postgres.
- `uv run pytest -x` is green (full suite, including Phase 1 invariants).
- Migration 0002 is committed and round-trips.
- `apscheduler==3.11.2` is in `pyproject.toml` (`grep '"apscheduler.*3.11.2"' pyproject.toml`) and in `uv.lock`.
- The four test fixtures exist and parse as valid JSON; `client_info_multi_card.json` contains exactly one card per type {black, platinum, white, eAid}.
- `tests/conftest.py` truncate covers `import_runs` and `scheduler_state` AND re-seeds the singleton.
- `must_haves.truths` verifiable: each truth has a passing test (mostly in `test_import_run_repo.py` / `test_scheduler_state_repo.py` / `test_migrations.py`).
- Reachability of every must-have artifact: a concrete file path; a unit test that imports it; a migration that creates the underlying schema.
</success_criteria>

<output>
After completion, create `.planning/phases/02-reliable-sync/02-01-SUMMARY.md` covering: schema delta (3 schema artifacts), repo seams added, fixture set, conftest extension, and any deviations from RESEARCH.md Code Examples §5 / PATTERNS.md lines 681-727. Note any empirical observations about Mono's `raw_payload->>'type'` shape (especially: any cards with NULL type after the UPDATE — that would indicate a Mono shape change since Phase 1).
</output>
