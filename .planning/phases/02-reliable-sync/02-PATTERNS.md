# Phase 02: Reliable Sync — Pattern Map

**Mapped:** 2026-05-10
**Files analyzed:** 28 (8 new src + 6 modified src + 1 new migration + 9 new tests + 2 modified tests + scheduler package)
**Analogs found:** 27 / 28 (1 file — `scheduler/runner.py` — has no exact analog; composes existing patterns)

## File Classification

### New source files

| New file | Role | Data Flow | Closest Analog | Match Quality |
|----------|------|-----------|----------------|---------------|
| `src/finance_bro/db/import_run_repo.py` | repository | CRUD | `src/finance_bro/db/transaction_repo.py` | exact (repo, AsyncSession, returns ORM rows) |
| `src/finance_bro/db/scheduler_state_repo.py` | repository | CRUD (singleton) | `src/finance_bro/db/rate_state_repo.py` | exact (singleton-row repo, raw `text()` SQL) |
| `src/finance_bro/scheduler/__init__.py` | package marker | n/a | `src/finance_bro/importers/__init__.py` | exact (empty marker) |
| `src/finance_bro/scheduler/runner.py` | service / orchestrator | event-driven (tick) | **partial only** — composes `services/import_service.py` (orchestration shape) + `importers/rate_limit.py` (session_factory injection) + `services/import_service.py` (`async with self._session_factory() as s, s.begin()` pattern) | role-match composite |
| `src/finance_bro/scheduler/window.py` | utility | transform (pure function) | `src/finance_bro/importers/currency_map.py` | role-match (pure module-level mapping/iterator) |
| `src/finance_bro/scheduler/errors.py` | utility | n/a (exceptions) | `src/finance_bro/services/import_service.py` (`NoCardAccountFound` class) | role-match (typed exception class) |
| `src/finance_bro/api/routes_status.py` | route (controller) | request-response (read) | `src/finance_bro/api/routes_accounts.py` | exact (read-only GET, response_model, AsyncSession dep) |
| `src/finance_bro/api/routes_backfill.py` | route (controller) | request-response (write, 202) | `src/finance_bro/api/routes_import.py` | role-match (POST, service injection); 202-status is new shape |
| `alembic/versions/0002_phase2_sync.py` | migration | DDL | `alembic/versions/0001_walking_skeleton.py` | exact (Alembic upgrade/downgrade with `op.create_table`/`op.execute`) |

### Modified source files

| Modified file | Role | Data Flow | Phase 1 Pattern to Preserve |
|---------------|------|-----------|------------------------------|
| `src/finance_bro/db/models.py` | ORM models | n/a | DeclarativeBase + Mapped + `__table_args__` ConstraintList |
| `src/finance_bro/db/account_repo.py` | repository | CRUD | adds `list_pollable_cards()` — mirrors existing `get_first_card()` shape |
| `src/finance_bro/db/transaction_repo.py` | repository | CRUD | upsert clause swap; preserve `index_where=text("NOT is_deleted")` partial-index reference |
| `src/finance_bro/api/schemas.py` | DTO | n/a | `BaseModel` + `model_config = ConfigDict(from_attributes=True)`; integer minor units |
| `src/finance_bro/api/deps.py` | DI providers | n/a | `Annotated[T, Depends(...)]` chains; `get_session_factory()` reuse |
| `src/finance_bro/api/routes_import.py` | route | request-response (write) | reshape to 202; **breaks Phase 1 synchronous body contract** |
| `src/finance_bro/main.py` | bootstrap | event-driven (lifespan) | `@asynccontextmanager` lifespan; router mount-no-prefix |
| `src/finance_bro/services/import_service.py` | service | request-response | `run_one_card()` becomes `run_one(import_run_id)`; preserve `async with self._session_factory() as s, s.begin()` SQLA idiom |
| `src/finance_bro/importers/monobank.py` | adapter | streaming (HTTP → AsyncIterator) | gate-then-fetch order; raise typed exceptions instead of bare `httpx.HTTPStatusError` |

### Test files

| Test file | Role | Data Flow | Closest Analog | Match Quality |
|-----------|------|-----------|----------------|---------------|
| `tests/test_scheduler_round_robin.py` | test (unit) | n/a | `tests/test_importer_statement.py` (stub_gate fixture pattern) + `tests/test_idempotency.py` (DB-bound) | role-match composite |
| `tests/test_backfill_enqueue.py` | test (unit) | n/a | `tests/test_idempotency.py` | exact (DB-bound, respx-mocked importer) |
| `tests/test_backfill_resumability.py` | test (integration) | n/a | `tests/test_rate_limit_gate.py::test_persists_across_restart` | exact (cross-instance state via session_factory) |
| `tests/test_backfill_window_math.py` | test (unit) | n/a | `tests/test_importer_currency_map.py` (assumed; pure-function unit test) | role-match (pure-function unit test) |
| `tests/test_hold_cleared_upsert.py` | test (DB) | n/a | `tests/test_partial_unique_index.py` | exact (raw `text()` SQL + `session_factory` fixture) |
| `tests/test_import_status_shape.py` | test (HTTP route) | n/a | `tests/test_transactions_route.py` | exact (`client` fixture + `respx` seed + JSON shape assert) |
| `tests/test_401_stops_scheduler.py` | test (integration) | n/a | `tests/test_import_route.py` (respx error fixture) + `tests/test_rate_limit_gate.py` (cross-instance) | role-match composite |
| `tests/test_429_does_not_stop.py` | test (integration) | n/a | `tests/test_import_route.py` | role-match (respx 429 response) |
| `tests/test_force_poll_endpoint.py` | test (HTTP route) | n/a | `tests/test_import_route.py` | exact (POST + assert response shape, then verify side effects via DB) |
| `tests/test_import_route.py` (modified) | test (HTTP route) | n/a | self — replace assertions on `inserted/skipped_duplicates` body with 202 + `enqueued` shape |
| `tests/test_transactions_route.py` (modified) | test (HTTP route) | n/a | self — add `hold` field assertion to `test_response_shape` |

---

## Pattern Assignments

### `src/finance_bro/db/transaction_repo.py` (MODIFIED — upsert clause swap)

**Current shape (Phase 1) — `src/finance_bro/db/transaction_repo.py` lines 1-57:**

```python
"""Transaction repository — single owner of writes/reads against `transactions`.

`insert_many` uses `INSERT ... ON CONFLICT (account_id, source_tx_id) WHERE NOT
is_deleted DO NOTHING` against the partial unique index `uq_transactions_account_source_tx`
declared in migration 0001 (ING-04). The `RETURNING id` clause lets us count
exactly how many rows were inserted (rows skipped by the conflict are not
returned), which the import service surfaces as `skipped_duplicates =
statement_count - inserted` (SC#3).
"""

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.db.models import Transaction
from finance_bro.importers.base import CanonicalTransaction


class TransactionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_many(
        self,
        account_id: int,
        items: list[CanonicalTransaction],
    ) -> int:
        if not items:
            return 0
        rows = [
            {
                "account_id": account_id,
                "source_tx_id": t.source_tx_id,
                "amount_minor": t.amount_minor,
                "currency": t.currency,
                "time": t.occurred_at,
                "raw_payload": t.raw,
            }
            for t in items
        ]
        stmt = (
            insert(Transaction)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["account_id", "source_tx_id"],
                index_where=text("NOT is_deleted"),
            )
            .returning(Transaction.id)
        )
        result = await self._s.execute(stmt)
        returned = result.scalars().all()
        return len(returned)
```

**Transformation (per RESEARCH.md Pattern 3 + D-10):**

1. Return type changes from `int` to `tuple[int, int]` (`inserted`, `updated_in_place`).
2. `rows` dict gains three new keys (allowed only on first INSERT — the SET clause omits them, so they freeze on conflict): `"description": getattr(t, "description", None)`, `"mcc": getattr(t, "mcc", None)`, `"hold": getattr(t, "hold", False)`.
3. `on_conflict_do_nothing(...)` becomes `on_conflict_do_update(set_={...})` with EXACTLY THREE EXCLUDED fields: `hold`, `amount_minor`, `raw_payload`. **Anti-pattern (RESEARCH.md):** any other field in the SET clause is a bug.
4. `.returning(Transaction.id)` becomes `.returning(Transaction.id, literal_column("(xmax = 0)").label("inserted"))`.
5. Iterate `result.all()` (not `result.scalars().all()` — we need both columns); count `inserted = sum(1 for r in rows_back if r.inserted)`; `updated = len(rows_back) - inserted`.
6. New import: `from sqlalchemy import literal_column` (already imports `text`, `select`).
7. **CanonicalTransaction must gain `hold`, `description`, `mcc` fields** in `src/finance_bro/importers/base.py` so `getattr(t, ...)` returns honest values instead of `None`/`False` defaults.

**Frozen-by-omission anti-pattern callout (D-10):** The SET clause MUST contain only `hold`, `amount_minor`, `raw_payload`. Phase 1's Pitfall-10 promise (importer never overwrites manual edits) depends on `is_user_locked`, `category_*`, `is_deleted`, `description`, `mcc`, `attributed_day`, `currency`, `time`, `created_at`, `account_id`, `source_tx_id` being absent from `set_={...}`.

---

### `src/finance_bro/importers/monobank.py` (MODIFIED — typed exceptions + mono_type extraction)

**Current shape (Phase 1) — `src/finance_bro/importers/monobank.py` full file (87 lines):**

Imports (lines 14-21):
```python
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from .base import CanonicalAccount, CanonicalTransaction
from .currency_map import numeric_to_alpha
from .rate_limit import RateLimitGate
```

discover_accounts (lines 41-66):
```python
async def discover_accounts(self) -> list[CanonicalAccount]:
    await self._gate.acquire(self._token)
    resp = await self._client.get("/personal/client-info")
    resp.raise_for_status()
    data = resp.json()
    out: list[CanonicalAccount] = []
    for acc in data.get("accounts", []):
        kind = "mono.fop" if acc.get("type") == "fop" else "mono.card"
        out.append(
            CanonicalAccount(
                source_account_id=acc["id"],
                source_kind=kind,
                currency=numeric_to_alpha(acc["currencyCode"]),
                raw=acc,
            )
        )
    for jar in data.get("jars", []):
        out.append(
            CanonicalAccount(
                source_account_id=jar["id"],
                source_kind="mono.jar",
                currency=numeric_to_alpha(jar["currencyCode"]),
                raw=jar,
            )
        )
    return out
```

fetch_statement (lines 68-87) — note the **`AsyncIterator` shape** (yields per item, not bulk return; `await self._gate.acquire` happens BEFORE the HTTP call):
```python
async def fetch_statement(
    self,
    source_account_id: str,
    since: datetime,
    until: datetime,
) -> AsyncIterator[CanonicalTransaction]:
    await self._gate.acquire(self._token)
    from_ts = int(since.timestamp())
    to_ts = int(until.timestamp())
    resp = await self._client.get(f"/personal/statement/{source_account_id}/{from_ts}/{to_ts}")
    resp.raise_for_status()
    for item in resp.json():
        yield CanonicalTransaction(
            source_tx_id=item["id"],
            source_account_id=source_account_id,
            occurred_at=datetime.fromtimestamp(item["time"], tz=UTC),
            amount_minor=int(item["amount"]),
            currency=numeric_to_alpha(item["currencyCode"]),
            raw=item,
        )
```

**Transformation (per RESEARCH.md Pattern 4 + Discretion bullet 2):**

1. **`mono_type` extraction (Discretion bullet 2):** Inside the `for acc in data.get("accounts", [])` loop, before the `out.append(...)`, capture `mono_type = acc.get("type")` and pass it to `CanonicalAccount(... mono_type=mono_type ...)`. **Requires `CanonicalAccount` dataclass to gain a `mono_type: str | None = None` field** in `src/finance_bro/importers/base.py`. Jars don't have `type`; FOPs use `mono.fop` source_kind; for both, leave `mono_type=None` (Discretion bullet 2).
2. **CanonicalTransaction extension:** Same file, add `hold: bool = False`, `description: str | None = None`, `mcc: int | None = None` fields. In `fetch_statement`'s `yield CanonicalTransaction(...)`, populate them from `item.get("hold", False)`, `item.get("description")`, `item.get("mcc")`. Preserves Phase 1's "amount_minor is int not float" invariant (already guaranteed by `int(item["amount"])`); the new fields use `.get(..., default)` because Mono may omit them on hold rows.
3. **Typed exceptions (RESEARCH.md Pattern 4 — Code Examples §4):** Both `discover_accounts` and `fetch_statement` currently call `resp.raise_for_status()` bare. Wrap each in:
   ```python
   try:
       resp.raise_for_status()
   except httpx.HTTPStatusError as e:
       if e.response.status_code == 401:
           raise MonoAuthError("Mono token rejected (401)") from e
       if e.response.status_code == 429:
           retry = e.response.headers.get("Retry-After")
           retry_seconds = int(retry) if retry and retry.isdigit() else None
           raise MonoRateLimitError(retry_seconds) from e
       raise MonoTransientError(f"Mono {e.response.status_code}") from e
   ```
4. **New import:** `from finance_bro.scheduler.errors import MonoAuthError, MonoRateLimitError, MonoTransientError`.
5. **Preserve unchanged:** `await self._gate.acquire(self._token)` MUST remain the first line of both methods — RESEARCH.md "Anti-Patterns" calls out "Adding a second Mono caller seam" as forbidden. The token still rides only in the `X-Token` header set in `__init__` (Phase 1's Pitfall-7 invariant — verified by `tests/test_importer_no_token_in_url.py`).

---

### `src/finance_bro/db/import_run_repo.py` (NEW)

**Analog:** `src/finance_bro/db/transaction_repo.py` (repo shape) + `src/finance_bro/db/rate_state_repo.py` (raw-`text()` SQL idiom)

**Imports pattern** (copy from `transaction_repo.py` lines 11-16):
```python
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.db.models import ImportRun  # NEW model
```

**Class shape** (copy from `transaction_repo.py` lines 19-21):
```python
class ImportRunRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
```

**Claim-and-execute pattern (RESEARCH.md Pattern 2)** — closest analog is `RateStateRepo.select_for_update` (`src/finance_bro/db/rate_state_repo.py` lines 35-44) for the raw-text style; `claim_next_pending` is a bigger UPDATE-with-subselect. RESEARCH.md provides the verbatim SQL. Repo methods to expose:

- `claim_next_pending() -> ImportRun | None` — RESEARCH.md Pattern 2 verbatim
- `enqueue_backfill(account_id, chunks: list[tuple[datetime, datetime]]) -> list[int]` — bulk insert; reuse `insert(ImportRun).values(rows).returning(ImportRun.id)` shape from `TransactionRepo.insert_many`
- `enqueue_live(account_id, window_from, window_to) -> int` — single insert
- `mark_done(run_id, statement_count, inserted, updated) -> None` — UPDATE with `text("UPDATE import_runs SET status='done', completed_at=now(), statement_count=:c, inserted=:i WHERE id=:id")` shape (mirror `RateStateRepo.upsert`)
- `mark_error(run_id, error: str) -> None` — UPDATE
- `recover_in_flight(threshold: timedelta) -> int` — RESEARCH.md Pattern 7 verbatim
- `count_pending_or_in_flight_backfill(account_id) -> int` — for D-06 gate (skip live polls when backfill active)
- `last_live_per_account() -> dict[int, ImportRun]` — for status query (RESEARCH.md Code Examples §4 STATUS_QUERY CTE shape)

**Why no `SKIP LOCKED`:** RESEARCH.md Pattern 2 + Anti-Patterns. Single tick consumer (`max_instances=1`) makes it unnecessary.

---

### `src/finance_bro/db/scheduler_state_repo.py` (NEW)

**Analog (exact):** `src/finance_bro/db/rate_state_repo.py` — both are singleton-style state tables managed via raw `text()` SQL.

**Imports pattern** (copy verbatim from `rate_state_repo.py` lines 8-11):
```python
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
```

**Class shape** (copy from `rate_state_repo.py` lines 14-18):
```python
class SchedulerStateRepo:
    """Single owner of writes to scheduler_state. SchedulerRunner uses this."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session
```

**Read pattern** (mirror `RateStateRepo.select_for_update` lines 35-44 — but no `FOR UPDATE` because the row is a simple flag, no contention):
```python
async def read(self) -> tuple[str, str | None, datetime] | None:
    """Returns (state, last_error, since) or None if migration didn't seed."""
    row = (
        await self._s.execute(
            text("SELECT state, last_error, since FROM scheduler_state WHERE id = 1")
        )
    ).first()
    return (row[0], row[1], row[2]) if row else None
```

**Write pattern** (mirror `RateStateRepo.upsert` lines 46-55 — but UPDATE-only, never INSERT because the migration seeds the singleton row):
```python
async def write(self, state: str, last_error: str | None) -> None:
    """Persist a state transition. The id=1 row is seeded by migration 0002."""
    await self._s.execute(
        text(
            "UPDATE scheduler_state "
            "SET state = :state, last_error = :err, since = now() "
            "WHERE id = 1"
        ),
        {"state": state, "err": last_error},
    )
```

---

### `src/finance_bro/scheduler/runner.py` (NEW — composite analog)

**No exact analog.** Composes:
- **Service shape** from `src/finance_bro/services/import_service.py` (`__init__(session_factory, importer)`, `async with self._session_factory() as session, session.begin():`).
- **Session-factory injection** from `src/finance_bro/importers/rate_limit.py` (`RateLimitGate.__init__(self, session_factory: async_sessionmaker[AsyncSession])` — line 32).
- **Tick orchestration** verbatim from RESEARCH.md Code Examples §3 (lines 854-909).
- **Recovery sweep** verbatim from RESEARCH.md Pattern 7 (lines 596-615).

**Constructor pattern** (copy from `services/import_service.py` lines 39-46):
```python
class SchedulerRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        importer: MonobankImporter,
    ) -> None:
        self._session_factory = session_factory
        self._importer = importer
        self._cached_state: tuple[str, str | None] = ("running", None)
```

**Tick body** (copy verbatim from RESEARCH.md Code Examples §3 lines 864-908 — the dequeue/fetch/upsert/mark-done pipeline). The session-acquire idiom inside tick reuses ImportService's `async with self._session_factory() as session, session.begin():` (lines 53, 61, 83 of `services/import_service.py`).

**Recovery sweep** (copy verbatim from RESEARCH.md Pattern 7 lines 596-614). Calls `ImportRunRepo.recover_in_flight()` inside `async with self._session_factory() as session, session.begin():`.

**Pick-next-card logic (D-01 + D-02 + D-06):**
- Filter: `Account.source_kind == "mono.card" AND Account.mono_type IN ("black","platinum","white")` — implement as `AccountRepo.list_pollable_cards()` mirroring `get_first_card()` (`src/finance_bro/db/account_repo.py` lines 25-33).
- Skip if any backfill row pending/in_flight for that account (D-06) — `ImportRunRepo.count_pending_or_in_flight_backfill(account_id) > 0`.
- Pick by oldest `last_live` `completed_at` (Discretion bullet 5 step 4) — joins via `ImportRunRepo.last_live_per_account()`.

---

### `src/finance_bro/scheduler/window.py` (NEW)

**Analog:** `src/finance_bro/importers/currency_map.py` (assumed pure-data utility module — same structural role: module-level constants + pure function).

**Verbatim from RESEARCH.md Pattern 6 (lines 562-582):**
```python
from datetime import datetime, timedelta, UTC
from collections.abc import Iterator

MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000  # 31d + 1h — Mono cap (Pitfall 5)
MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30        # Operating chunk size (1h+ headroom)

def backfill_chunks(now: datetime, months: int = 12) -> Iterator[tuple[datetime, datetime]]:
    """Yield (window_from, window_to) tuples in newest-first order."""
    for n in range(months):
        window_to = now - timedelta(days=n * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        window_from = now - timedelta(days=(n + 1) * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        yield window_from, window_to
```

**No new imports beyond stdlib.** Keep this module dependency-free so unit tests don't need a DB or HTTP fixture.

---

### `src/finance_bro/scheduler/errors.py` (NEW)

**Analog:** `src/finance_bro/services/import_service.py` `NoCardAccountFound` exception class (lines 34-36).

**Pattern** (verbatim from RESEARCH.md Pattern 4, lines 494-507):
```python
class MonoAuthError(Exception):
    """Raised when Mono returns 401. Sticky — sets scheduler_state='auth_failed'."""

class MonoRateLimitError(Exception):
    """Raised when Mono returns 429. Transient — surfaced per-call only."""
    def __init__(self, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Mono 429 (Retry-After={retry_after_seconds})")

class MonoTransientError(Exception):
    """Raised on 5xx / connect-timeout / read-timeout. Per-call import_runs.error;
    the next tick tries the next pending row."""
```

**Docstring style** (mirror `NoCardAccountFound` in `services/import_service.py` lines 34-36 — short, references the relevant decision ID).

---

### `src/finance_bro/api/routes_status.py` (NEW)

**Analog (exact):** `src/finance_bro/api/routes_accounts.py` (full 21 lines).

**Imports pattern** (copy verbatim from `routes_accounts.py` lines 1-10):
```python
"""Status endpoint — scheduler + per-account + backfill state (D-14)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import ImportStatusOut
```

**Route shape** (mirror `routes_accounts.py` lines 12-20):
```python
router = APIRouter()


@router.get("/api/import/status", response_model=ImportStatusOut)
async def import_status(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ImportStatusOut:
    # Body: execute the STATUS_QUERY CTE from RESEARCH.md Code Examples §4
    # via session.execute(STATUS_QUERY); compose ImportStatusOut from the row set
    # plus a single SchedulerStateRepo(session).read() call.
    ...
```

**SQL** — copy verbatim from RESEARCH.md Code Examples §4 (lines 911-952). Wire it via `session.execute(text(...))` like `routes_health.py` line 21 does for `SELECT 1`.

---

### `src/finance_bro/api/routes_backfill.py` (NEW)

**Analog (role-match):** `src/finance_bro/api/routes_import.py` (POST + service injection); shape diverges because Phase 2 returns 202 not 200.

**Imports pattern** (copy from `routes_import.py` lines 1-17):
```python
"""Backfill endpoint — debug/operator. POST enqueues 12 chunks per active card.

Returns 202 Accepted with {run_ids: [...]} immediately; the actual fetches
happen on subsequent scheduler ticks (D-07)."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status

from finance_bro.api.deps import get_scheduler_runner  # NEW dep
from finance_bro.api.schemas import BackfillEnqueueIn, BackfillEnqueueOut
from finance_bro.scheduler.runner import SchedulerRunner

router = APIRouter()
_log = structlog.get_logger()
```

**Route shape** (compare against `routes_import.py` lines 23-44 — but use `status_code=status.HTTP_202_ACCEPTED`):
```python
@router.post("/api/backfill", response_model=BackfillEnqueueOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_backfill(
    body: BackfillEnqueueIn,
    runner: Annotated[SchedulerRunner, Depends(get_scheduler_runner)],
) -> BackfillEnqueueOut:
    _log.info("backfill.enqueue", account_id=body.account_id, months=body.months)
    run_ids = await runner.enqueue_backfill(account_id=body.account_id, months=body.months or 12)
    return BackfillEnqueueOut(run_ids=run_ids)
```

**Body schema** (Discretion bullet 7): `BackfillEnqueueIn` has optional `account_id: int | None = None` and optional `months: int = 12`. Default = backfill all active cards 12 months.

---

### `src/finance_bro/api/routes_import.py` (MODIFIED — Phase 1 → 202 enqueue, D-16)

**Current Phase 1 shape:** see above (`routes_import.py` lines 23-44 — synchronous body returning `ImportResultOut`).

**Transformation:**

1. **Status code:** `@router.post("/api/import", response_model=..., status_code=status.HTTP_202_ACCEPTED)`.
2. **Response model:** `ImportEnqueuedOut` (new) instead of `ImportResultOut`. Shape: `{enqueued: list[ImportEnqueueRowOut]}` where `ImportEnqueueRowOut(account_id: int, run_id: int)`.
3. **Body change:** instead of `await svc.run_one_card()`, call `await runner.enqueue_live_for_all_active_cards()` and return the list of `(account_id, run_id)` tuples. The actual fetch happens on the next scheduler tick (≤10s) per D-16.
4. **Remove** `NoCardAccountFound` HTTPException 409 — Phase 2 enqueues even if zero cards exist (returns `{enqueued: []}`); discovery is owned by the runner.
5. **Preserve:** structlog `info("import.start")` / `info("import.done", ...)` logging idiom from `routes_import.py` lines 27, 32-38 (just change the keys to match the new shape: `enqueued_count` instead of `inserted`/`skipped_duplicates`).

**Anti-pattern from RESEARCH.md:** "Letting any HTTP route synchronously await Mono. Phase 1's `POST /api/import` did this; Phase 2 reshapes to 202. Tests must NOT regress to assert synchronous behavior."

---

### `src/finance_bro/api/schemas.py` (MODIFIED — add hold + new schemas)

**Current shape (Phase 1) — `src/finance_bro/api/schemas.py` lines 29-44:**
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


class ImportResultOut(BaseModel):
    polled_account_id: str
    statement_count: int
    inserted: int
    skipped_duplicates: int
```

**Transformation:**

1. **`TransactionOut`:** add `hold: bool` (D-12). The `model_config = ConfigDict(from_attributes=True)` already in place handles ORM hydration from `Transaction.hold` (now actively populated by the upsert).
2. **`AccountOut`:** add `mono_type: str | None = None` so `GET /api/accounts` exposes the new column.
3. **Keep `ImportResultOut`** for backward compatibility OR delete it (Phase 1 was the only consumer). **Recommendation:** delete and replace with `ImportEnqueuedOut` + `ImportEnqueueRowOut` (D-16).
4. **New schemas** for D-14 (`GET /api/import/status`):
   - `SchedulerStatusOut(state: str, since: datetime, last_error: str | None)`
   - `AccountStatusOut(account_id: int, source_account_id: str, mono_type: str | None, last_polled_at: datetime | None, last_poll_inserted: int | None, last_poll_updated: int | None, last_status: str | None, last_error: str | None)`
   - `BackfillStatusOut(state: str, runs_remaining: int, runs_total: int, eta_seconds: int | None)`
   - `ImportStatusOut(scheduler: SchedulerStatusOut, accounts: list[AccountStatusOut], backfill: BackfillStatusOut)`
5. **New schemas** for D-07 (`POST /api/backfill`):
   - `BackfillEnqueueIn(account_id: int | None = None, months: int = 12)`
   - `BackfillEnqueueOut(run_ids: list[int])`

**Style guardrail (from `schemas.py` module docstring lines 1-8):** money on JSON boundary stays integer minor units typed as `int` — never str/float/Decimal. ISO-4217 alpha currency. Mirror this for any new money-shaped fields (Phase 2 has none, but the ImportRun status counts are integer counts, no money).

---

### `src/finance_bro/api/deps.py` (MODIFIED — add new providers)

**Current shape — `src/finance_bro/api/deps.py` full 43 lines** establishes the `Annotated[T, Depends(...)]` chain:
- `get_session()` — yields `AsyncSession`
- `get_rate_gate()` — instantiates `RateLimitGate(get_session_factory())`
- `get_importer(settings, gate)` — instantiates `MonobankImporter`
- `get_import_service(importer)` — instantiates `ImportService`

**Transformation (additive):**

1. **`get_scheduler_runner(request: Request) -> SchedulerRunner`** — read `request.app.state.runner` (set in lifespan, RESEARCH.md Pattern 1 line 346-347). This bypasses the per-request DI chain because the runner is process-scoped. Pattern is FastAPI-canonical; mirrors how `app.state.scheduler` is also stored.
2. **`get_import_run_repo(session) -> ImportRunRepo`** — mirrors `get_rate_gate()` shape (no Depends, plain factory) but takes session. Could also be inlined at call sites — Phase 1 inlines `TransactionRepo(session)` directly in `routes_transactions.py` line 29 rather than going through deps. Recommendation: inline at call sites; do not add a `get_*_repo()` provider.
3. **`get_scheduler_state_repo(session) -> SchedulerStateRepo`** — same as #2, inline.

**Anti-pattern callout:** Do NOT add a `get_scheduler()` provider returning the APScheduler — it's a singleton on `app.state.scheduler` and only lifespan should touch it. Routes never need it.

---

### `src/finance_bro/main.py` (MODIFIED — lifespan extends, mount new routers)

**Current shape — `src/finance_bro/main.py` full 41 lines:**

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    init_engine()
    yield


app = FastAPI(title="finance-bro", lifespan=lifespan)
app.include_router(routes_health.router)
app.include_router(routes_accounts.router)
app.include_router(routes_transactions.router)
app.include_router(routes_import.router)
```

**Transformation (verbatim from RESEARCH.md Code Examples §2 lines 819-851 + Pattern 1 lines 322-354):**

1. **Lifespan body** — extend after `init_engine()`:
   ```python
   runner = SchedulerRunner(
       session_factory=get_session_factory(),
       importer=MonobankImporter(settings.mono_token, RateLimitGate(get_session_factory())),
   )
   await runner.recover_in_flight()
   state, last_err, _since = await runner.read_state()  # SchedulerStateRepo lookup
   scheduler = AsyncIOScheduler()
   if state == "running":
       scheduler.add_job(
           runner.tick,
           IntervalTrigger(seconds=10),
           id="finance-bro-tick",
           max_instances=1,
           coalesce=True,
           misfire_grace_time=30,
       )
       scheduler.start()
   app.state.scheduler = scheduler
   app.state.runner = runner
   try:
       yield
   finally:
       if scheduler.running:
           scheduler.shutdown(wait=False)  # Pitfall 8: wait=False, not True
       await runner.aclose()
   ```
2. **Mount new routers** — copy the `app.include_router(...)` pattern from lines 37-40:
   ```python
   app.include_router(routes_status.router)
   app.include_router(routes_backfill.router)
   ```
3. **Imports** add: `from apscheduler.schedulers.asyncio import AsyncIOScheduler`, `from apscheduler.triggers.interval import IntervalTrigger`, `from finance_bro.scheduler.runner import SchedulerRunner`, `from finance_bro.importers.monobank import MonobankImporter`, `from finance_bro.importers.rate_limit import RateLimitGate`, `from finance_bro.db.engine import get_session_factory` (currently only `init_engine` is imported).

**Critical ordering (RESEARCH.md Pattern 1 + Pitfall 8):**
- `init_engine()` BEFORE creating runner (engine must exist before SchedulerRunner gets the session_factory).
- `runner.recover_in_flight()` BEFORE `scheduler.start()` (no concurrent tick at sweep time).
- `scheduler.shutdown(wait=False)` BEFORE engine teardown in the `finally` (Pitfall 8: `wait=True` blocks lifespan exit by 65s).

---

### `src/finance_bro/services/import_service.py` (MODIFIED — replaced/extended)

**Current Phase 1 shape — `src/finance_bro/services/import_service.py` full 92 lines** orchestrates: lazy discovery → first-card pick → fetch → insert. The four-step structure (lines 48-91) is the analog template.

**Transformation:**

The Phase 2 scheduler runner replaces `run_one_card()`'s "polling decision logic" but the **session/transaction idiom is preserved verbatim**:

```python
async with self._session_factory() as session, session.begin():
    # repo work
```

This appears 3 times in Phase 1 ImportService (lines 53, 61, 83). Phase 2's SchedulerRunner.tick uses the same idiom for: claim_next_pending, fetch+upsert, mark_done/error. RESEARCH.md Pitfall 6 explicitly preserves this: "`insert_many` is called inside `session.begin()` blocks (existing Phase 1 pattern in `ImportService.run_one_card`). The caller commits before reading the counts."

**New `run_one(import_run_id: int)` method** (Discretion bullet 5 + RESEARCH.md Code Examples §3):
- Takes a claimed `import_run` row.
- Looks up the `Account` (existing `AccountRepo` — no new method needed; could use `select(Account).where(Account.id == account_id)` inline).
- Calls `importer.fetch_statement(account.source_account_id, run.window_from, run.window_to)` and materializes via list comprehension (existing pattern at `import_service.py` lines 73-80).
- Calls the new tuple-returning `TransactionRepo.insert_many(...)` and surfaces `(inserted, updated)` to the caller.
- Catches `MonoAuthError`/`MonoRateLimitError`/`MonoTransientError` at this layer OR lets the runner catch (RESEARCH.md Code Examples §3 catches at runner — recommended).

**Preserve Phase 1's lazy discovery** for the cold-boot case (Phase 1 D-03 + D-06 — discovery on first import). The runner's first tick after a fresh boot must trigger discovery if `accounts` table is empty. Reuse the `if not have_accounts: discovered = await self._importer.discover_accounts(); ...upsert_many(...)` block from `import_service.py` lines 52-62 verbatim.

---

### `alembic/versions/0002_phase2_sync.py` (NEW)

**Analog (exact):** `alembic/versions/0001_walking_skeleton.py` (full 92 lines).

**Header pattern** (copy from `0001_walking_skeleton.py` lines 1-16):
```python
"""phase 2 sync

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None
```

**Body** — verbatim from RESEARCH.md Code Examples §5 (lines 957-1032). Key pattern parallels with `0001_walking_skeleton.py`:
- `op.create_table(..., sa.Column(...), sa.UniqueConstraint(...))` — mirror lines 20-36.
- `sa.Column("...", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()"))` — mirror line 27-32.
- `sa.Column("...", postgresql.JSONB, ...)` — mirror line 26 (Phase 2 doesn't use JSONB but the import is already conventional).
- `sa.ForeignKey("accounts.id", ondelete="RESTRICT")` — mirror line 43.
- `op.create_index(..., postgresql_where=...)` partial-index idiom — mirror lines 72-78 (Phase 2 doesn't add a partial index but the import paths are established).

**Order of operations (RESEARCH.md Code Examples §5):**
1. `op.add_column("accounts", sa.Column("mono_type", sa.Text, nullable=True))`
2. `op.execute("UPDATE accounts SET mono_type = raw_payload->>'type' WHERE source_kind = 'mono.card'")` — Pitfall 7 mitigation (existing rows backfilled in same revision)
3. `op.create_table("scheduler_state", ...)` with both CheckConstraints + PrimaryKey
4. `op.execute("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')")` — seed
5. `op.create_table("import_runs", ...)` with FK + 2 CheckConstraints (`run_kind`, `status`)
6. `op.create_index("ix_import_runs_account_kind_completed", ...)` — Pitfall 5 mitigation
7. `op.create_index("ix_import_runs_status_created", ...)` — for `claim_next_pending` ordering

**Downgrade** — RESEARCH.md Code Examples §5 lines 1026-1032 (drop in reverse order, same as Phase 1 Pattern at lines 87-91).

**Test analog** — `tests/test_migrations.py::test_round_trip` (lines 10-33) does `downgrade base → upgrade head` and asserts tables exist. Phase 2 should add an analogous test or extend this test to assert `import_runs` and `scheduler_state` exist post-upgrade.

---

### `src/finance_bro/db/models.py` (MODIFIED — add Account.mono_type, ImportRun, SchedulerState)

**Current shape — `src/finance_bro/db/models.py` full 87 lines** establishes:
- `class Base(DeclarativeBase)` (line 21-22)
- `Mapped[...] = mapped_column(...)` typing (lines 28-35, 45-69)
- `__table_args__ = (UniqueConstraint(...), Index(...))` (lines 37-39, 71-79)
- `JSONB` import for raw_payload (line 17)
- `server_default=text("now()")` for created_at columns (lines 33-35)

**Transformation:**

1. **`Account.mono_type`** — add `mono_type: Mapped[str | None] = mapped_column(Text, nullable=True)` after line 32. Mirror the nullable `category_id` pattern in `Transaction` lines 59-60 for nullable mapped columns.

2. **`ImportRun` class (new)** — mirror `Transaction` declaration shape (lines 42-79):
   ```python
   class ImportRun(Base):
       __tablename__ = "import_runs"

       id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
       account_id: Mapped[int] = mapped_column(
           BigInteger,
           ForeignKey("accounts.id", ondelete="RESTRICT"),
           nullable=False,
       )
       run_kind: Mapped[str] = mapped_column(Text, nullable=False)
       window_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
       window_to: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
       status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
       last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
       attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
       statement_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
       inserted: Mapped[int | None] = mapped_column(Integer, nullable=True)
       started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
       completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
       created_at: Mapped[datetime] = mapped_column(
           TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
       )
   ```
   The CheckConstraints from migration 0002 are enforced at the DB level; whether to also declare them in `__table_args__` is optional — Phase 1 did not (no CHECKs in migration 0001), so omit for consistency.

3. **`SchedulerState` class (new)** — mirror `MonoRateState` shape (lines 82-86):
   ```python
   class SchedulerState(Base):
       __tablename__ = "scheduler_state"

       id: Mapped[int] = mapped_column(Integer, primary_key=True)
       state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
       last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
       since: Mapped[datetime] = mapped_column(
           TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
       )
   ```
   `MonoRateState` is the closest singleton-ish analog (single primary key without autoincrement).

4. **Note about `Phase 1 Transaction.hold`:** the column already exists in models.py line 58 (`hold: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))`) — Phase 2 does NOT need to alter the model, only start writing it via the upsert.

---

## Test Pattern Assignments

Phase 1 tests establish 3 fixture archetypes that Phase 2 replicates:

### Archetype A: stub-gate unit test (no DB)
**Source:** `tests/test_importer_statement.py` lines 13-18 + 21-34
**Use for:** `test_backfill_window_math.py` (pure function), `test_backfill_enqueue.py` (with DB extension)
```python
@pytest.fixture
def stub_gate():
    g = AsyncMock()
    g.acquire = AsyncMock(return_value=None)
    return g
```

### Archetype B: testcontainers + raw SQL (DB invariants)
**Source:** `tests/test_partial_unique_index.py` lines 22-52
**Use for:** `test_hold_cleared_upsert.py` (the central correctness test — D-10).
```python
@pytest.mark.asyncio
async def test_active_duplicate_rejected(session_factory):
    async with session_factory() as s:
        await s.execute(text("INSERT INTO accounts ..."))
        # ... raw SQL setup ...
        await s.commit()
    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            await s.execute(text("INSERT INTO transactions ..."))
            await s.commit()
```

### Archetype C: respx-mocked HTTP route test
**Source:** `tests/test_import_route.py` lines 22-44 (full pattern)
**Use for:** `test_import_status_shape.py`, `test_force_poll_endpoint.py`, modified `test_import_route.py`, modified `test_transactions_route.py`, `test_401_stops_scheduler.py` (with 401 fixture), `test_429_does_not_stop.py` (with 429 fixture).
```python
@pytest.mark.asyncio
async def test_first_import_discovers_and_inserts(client):
    with (
        respx.mock(base_url="https://api.monobank.ua") as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(
            return_value=httpx.Response(200, json=_client_info())
        )
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=_statement())
        )
        r = await client.post("/api/import")
    assert r.status_code == 200, r.text
    body = r.json()
    # ... assertions
```

### Archetype D: cross-instance state persistence
**Source:** `tests/test_rate_limit_gate.py::test_persists_across_restart` lines 22-40
**Use for:** `test_backfill_resumability.py` (RESEARCH.md Pattern 7) and `test_401_stops_scheduler.py` (D-15 sticky bit survives "restart").
```python
@pytest.mark.asyncio
async def test_persists_across_restart(session_factory):
    gate_a = RateLimitGate(session_factory)
    with patch("finance_bro.importers.rate_limit.asyncio.sleep", new_callable=AsyncMock):
        await gate_a.acquire("tok-restart")
    del gate_a
    gate_b = RateLimitGate(session_factory)
    # second instance reads first instance's persisted state
```

### Per-test pattern assignments

| Phase 2 test | Archetype | Specific Phase 1 analog |
|--------------|-----------|--------------------------|
| `test_scheduler_round_robin.py` | A + B | `test_importer_statement.py` (stub_gate) + `test_idempotency.py` (DB seeding via session_factory) |
| `test_backfill_enqueue.py` | A + B | seed accounts via `session_factory`, call `runner.enqueue_backfill(...)`, assert `import_runs` rows via raw SQL |
| `test_backfill_resumability.py` | D | mirror `test_persists_across_restart` — instantiate `SchedulerRunner` A, partially execute backfill, mark a row in_flight stale, instantiate runner B, call `recover_in_flight`, assert row is back to `pending` |
| `test_backfill_window_math.py` | (pure) | no DB, no HTTP; assert `list(backfill_chunks(now=datetime(2026,5,10,tzinfo=UTC), months=12))` has 12 tuples in newest-first order with 30-day spacing |
| `test_hold_cleared_upsert.py` | B | mirror `test_partial_unique_index.py::test_active_duplicate_rejected` shape — seed an `(account, transaction)` row with `hold=true, amount_minor=A`, call `TransactionRepo.insert_many(...)` again with `hold=false, amount_minor=B`, assert single row, mutated fields, frozen `is_user_locked`/`category_id`/etc. |
| `test_import_status_shape.py` | C | mirror `test_transactions_route.py::test_response_shape` — seed via `_seed(client)` helper, GET `/api/import/status`, assert all four states render |
| `test_401_stops_scheduler.py` | C + D | use `respx.mock` to return `httpx.Response(401, json={"errorDescription":"Unknown 'X-Token'"})`, call `runner.tick()`, assert `scheduler_state.state == 'auth_failed'`. Then create runner B, call `runner.read_state()`, assert sticky |
| `test_429_does_not_stop.py` | C | `respx` returns `httpx.Response(429, headers={"Retry-After": "60"}, json={...})`, call `runner.tick()`, assert state stays `running`, assert `import_runs.last_error` contains "429" |
| `test_force_poll_endpoint.py` | C | `await client.post("/api/import")` against fresh DB; assert 202 status; assert response body has `enqueued: [{account_id, run_id}, ...]`; query `import_runs` directly to verify `pending` rows exist |
| `test_import_route.py` (modified) | C | replace `assert body["statement_count"] == 2 / inserted == 2` with `assert r.status_code == 202` + `assert "enqueued" in body`. Optionally drive a tick afterward to verify the rows actually flow through |
| `test_transactions_route.py` (modified) | C | extend `test_response_shape`: `assert "hold" in row` and `assert isinstance(row["hold"], bool)` |

### Test fixtures to extend

**Existing:** `tests/fixtures/client_info_minimal.json` and `tests/fixtures/statement_two_items.json`.

**New fixtures Phase 2 will need (Plan-stage decision; the planner can spec these per test):**
- `client_info_multi_card.json` — 4 cards with `type` ∈ {`black`, `platinum`, `white`, `eAid`} for `test_scheduler_round_robin.py` (the eAid one verifies the allowlist)
- `statement_hold_then_cleared.json` — same `id`, two payloads (one `hold:true`, one `hold:false` with different `amount`) for `test_hold_cleared_upsert.py`
- `statement_empty.json` — `[]` (for backfill chunks past Mono retention horizon — Pitfall 3)
- `mono_401_response.json` and `mono_429_response.json` — for typed-exception tests

**Statement fixture format (existing analog):** `tests/fixtures/statement_two_items.json` already has a `hold: false` field on each item (lines 8, 23) — Mono returns it; Phase 1 ignored it; Phase 2 uses it. The `hold: true` variant for tests is just changing the boolean.

---

## Shared Patterns

### Pattern S1: Repository constructor + AsyncSession single-arg

**Source:** every existing repo
- `src/finance_bro/db/transaction_repo.py` lines 19-21
- `src/finance_bro/db/account_repo.py` lines 17-19
- `src/finance_bro/db/rate_state_repo.py` lines 14-18

**Apply to:** `ImportRunRepo`, `SchedulerStateRepo`

```python
class XxxRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
```

The session is owned by the caller via `async with session_factory() as session, session.begin():`. The repo never opens a session.

### Pattern S2: Session factory injection + transaction boundary

**Source:** `src/finance_bro/services/import_service.py` lines 39-62 (constructor + every transaction block) and `src/finance_bro/importers/rate_limit.py` lines 32-46.

**Apply to:** `SchedulerRunner.__init__(self, session_factory: async_sessionmaker[AsyncSession], importer)` and every method on `SchedulerRunner` that touches the DB.

```python
async with self._session_factory() as session, session.begin():
    repo = SomeRepo(session)
    await repo.do_work()
# commits on context exit; rolls back on exception
```

### Pattern S3: structlog logging at INFO with structured keys

**Source:** `src/finance_bro/api/routes_import.py` lines 27, 32-38; `src/finance_bro/core/logging.py` lines 13-27.

**Apply to:** all new routes (`routes_status.py`, `routes_backfill.py`) and the runner tick (`runner.py`).

```python
import structlog
_log = structlog.get_logger()
_log.info("scheduler.tick.run.start", import_run_id=run.id, account_id=run.account_id, run_kind=run.run_kind)
```

**Redaction guarantee (CLAUDE.md + `core/logging.py` lines 13-27):** any key containing `token` or `amount` (case-insensitive) is masked at INFO+. Phase 2 code MUST NOT log `amount_minor` values directly at INFO; logging `inserted` and `updated_in_place` (counts) is safe; logging the full `raw_payload` is forbidden (would expose amounts).

### Pattern S4: Router declaration + mount

**Source:** every `src/finance_bro/api/routes_*.py` (consistent across `routes_health.py`, `routes_accounts.py`, `routes_transactions.py`, `routes_import.py`).

**Apply to:** `routes_status.py`, `routes_backfill.py`.

```python
router = APIRouter()


@router.get("/api/...", response_model=...)
async def name(...):
    ...
```

Then in `main.py`:
```python
app.include_router(routes_xxx.router)
```

**No prefix, no middleware** — DEP-02 (Tailscale/LAN trust boundary). Phase 2 does NOT change this; Phase 2 routers mount the same way.

### Pattern S5: respx + `patch("...rate_limit.asyncio.sleep", new_callable=AsyncMock)` for HTTP route tests

**Source:** `tests/test_import_route.py` lines 22-44 (full pattern).

**Apply to:** every Phase 2 test that goes through the HTTP boundary or instantiates the importer.

The `patch` of `asyncio.sleep` is mandatory — without it tests sleep for ~60 seconds inside the gate. The pattern is established and **must be preserved** across all 9 new tests + 2 modified tests that exercise the gate path.

### Pattern S6: ON CONFLICT idempotency contract (the invariant Phase 2 must not break)

**Source:** Phase 1 invariants captured in `src/finance_bro/db/models.py` lines 71-79 (partial unique index) and `src/finance_bro/db/transaction_repo.py` lines 45-53 (current DO NOTHING).

**Phase 2 invariants to PRESERVE while changing the SET clause:**
- `index_elements=["account_id", "source_tx_id"]` — composite key (no change)
- `index_where=text("NOT is_deleted")` — partial-index predicate (no change; both DO NOTHING and DO UPDATE need it for Postgres to use the index)
- `(account_id, source_tx_id) WHERE NOT is_deleted` partial unique index from migration 0001 (no change)
- Importer never overwrites `is_user_locked`, `category_*`, `is_deleted`, `description`, `mcc`, `attributed_day` (Pitfall-10) — enforced by ABSENCE from the SET clause

**The change is exactly:** `on_conflict_do_nothing(...)` → `on_conflict_do_update(..., set_={"hold": ..., "amount_minor": ..., "raw_payload": ...})`. **Anything else added to `set_={...}` is a bug.**

### Pattern S7: `httpx.AsyncClient` configuration with header-only token (Pitfall 7 invariant)

**Source:** `src/finance_bro/importers/monobank.py` lines 32-36.

**Apply to:** any new HTTP usage Phase 2 introduces (recommendation: none — reuse `MonobankImporter._client`).

```python
self._client = httpx.AsyncClient(
    base_url=MONO_BASE,
    timeout=httpx.Timeout(30.0, connect=10.0),
    headers={"X-Token": token},
)
```

Token in the header only; URL paths NEVER carry the token. `tests/test_importer_no_token_in_url.py` enforces this. Phase 2 must keep the test green.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `src/finance_bro/scheduler/runner.py` | service / orchestrator | event-driven (tick) | No existing in-process job-tick orchestrator in Phase 1. Closest composite analog is `services/import_service.py` (orchestration shape) + `importers/rate_limit.py` (session_factory injection). RESEARCH.md Code Examples §3 is the canonical reference for the tick body. Planner should treat this as new ground but bounded by the established patterns S1, S2, S3, S6 above. |

---

## Metadata

**Analog search scope:**
- `src/finance_bro/api/` — all 6 files read
- `src/finance_bro/db/` — all 5 files read (models.py, account_repo.py, transaction_repo.py, rate_state_repo.py, engine.py)
- `src/finance_bro/services/` — all 1 file read (import_service.py)
- `src/finance_bro/importers/` — 4 files read (monobank.py, rate_limit.py, base.py, currency_map.py via Glob)
- `src/finance_bro/main.py` — read
- `src/finance_bro/core/` — settings.py + logging.py read
- `tests/` — 8 of 16 test files read in full (representative archetypes)
- `alembic/versions/0001_walking_skeleton.py` — read in full
- `tests/conftest.py` + `tests/fixtures/` — read in full

**Files scanned:** 28 source + test + migration + fixture files
**Pattern extraction date:** 2026-05-10
