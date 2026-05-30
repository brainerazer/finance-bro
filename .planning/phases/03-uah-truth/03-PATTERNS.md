# Phase 3: UAH Truth - Pattern Map

**Mapped:** 2026-05-30
**Files analyzed:** 18 (8 new, 10 modified) + 8 new test files
**Analogs found:** 17 / 18 (1 partial; no exact analog for the LATERAL-join read path)

This phase is backend-only Python. Every new file has a strong sibling in the
existing `src/finance_bro/` tree — the importer/adapter seam, the repository
pattern, the Alembic migration shape, and the testcontainers+respx harness are
all established. Copy from the analogs verbatim; the deltas are small and
documented per-file below.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| NEW `src/finance_bro/importers/nbu.py` | importer/adapter | request-response (outbound HTTP) | `src/finance_bro/importers/monobank.py` | exact (role + flow) |
| MOD `src/finance_bro/importers/base.py` | port/protocol | n/a (dataclass + Protocol) | self (`ImporterProtocol` + `CanonicalTransaction` in same file) | exact |
| MOD `src/finance_bro/importers/currency_map.py` | utility | transform | self (extend `_NUM_TO_ALPHA`) | exact |
| MOD `src/finance_bro/importers/monobank.py` | importer/adapter | request-response | self (extend `fetch_statement` yield) | exact |
| NEW `src/finance_bro/db/fx_rate_repo.py` | repository | CRUD (upsert + count) | `src/finance_bro/db/account_repo.py` (`upsert_many` ON CONFLICT DO NOTHING) | exact (role + flow) |
| NEW `src/finance_bro/db/tracked_fx_currency_repo.py` | repository | CRUD (iterate/upsert/update) | `src/finance_bro/db/scheduler_state_repo.py` + `account_repo.py` | role-match |
| MOD `src/finance_bro/db/transaction_repo.py` | repository | CRUD + read-join | self (`insert_many` frozen-by-omission + `list_for_account`) | exact |
| MOD `src/finance_bro/db/models.py` | model | n/a (ORM tables) | self (`ImportRun`, `Account`, `SchedulerState`) | exact |
| NEW `src/finance_bro/services/fx_rollup.py` | service/utility | transform (Decimal math) | (no exact analog — see No Analog) | partial |
| NEW `src/finance_bro/services/fx_bootstrap.py` | service | event-driven (idempotent orchestration) | `src/finance_bro/services/import_service.py` | role-match |
| MOD `src/finance_bro/scheduler/runner.py` | service (scheduler job) | event-driven (cron tick) | self (`tick`) | exact |
| MOD `src/finance_bro/main.py` | config (lifespan wiring) | event-driven | self (lifespan `add_job` + scheduler.start) | exact |
| MOD `src/finance_bro/api/schemas.py` | model (Pydantic DTO) | response-shaping | self (`TransactionOut`) | exact |
| MOD `src/finance_bro/api/routes_transactions.py` | route | request-response | self | exact |
| NEW `alembic/versions/0003_fx_truth.py` | migration | batch (DDL + backfill) | `alembic/versions/0002_phase2_sync.py` | exact |
| MOD `src/finance_bro/core/settings.py` | config | n/a | self (add optional `nbu_base`) | exact (optional, planner's call) |
| NEW `tests/fixtures/nbu_*.json` | test fixture | n/a | `tests/fixtures/statement_two_items.json` | exact |
| NEW `tests/test_fx_*.py` (8 files) | test | varies | `tests/test_importer_statement.py`, `test_migrations.py`, `test_schema_invariants.py` | exact |

## Pattern Assignments

### `src/finance_bro/importers/nbu.py` (NEW — importer/adapter, request-response)

**Analog:** `src/finance_bro/importers/monobank.py`

The whole file mirrors `MonobankImporter`: module docstring → constants →
`httpx.AsyncClient` constructed in `__init__` → `aclose()` → one fetch method.
The DELTAS vs Mono: no `RateLimitGate` (NBU has no rate limit), no token header,
`tenacity` retry decorator, and `json.loads(..., parse_float=Decimal)` to keep
floats off the rate.

**Imports + module constant pattern** (monobank.py lines 26-41):
```python
from collections.abc import AsyncIterator
from datetime import UTC, datetime
import httpx
from .base import CanonicalAccount, CanonicalTransaction
from .currency_map import numeric_to_alpha
from .rate_limit import RateLimitGate

MONO_BASE = "https://api.monobank.ua"
```
For NBU: drop `rate_limit`, add `tenacity` imports and `import json`; the
verified base is `NBU_BASE = "https://bank.gov.ua/NBU_Exchange/exchange_site"`
(RESEARCH.md "Range endpoint", confirmed live 2026-05-30).

**Client construction pattern** (monobank.py lines 57-67) — copy the
`httpx.AsyncClient(base_url=..., timeout=httpx.Timeout(30.0, connect=10.0))`
shape and the `aclose()` method verbatim; OMIT the `X-Token` header:
```python
self._client = httpx.AsyncClient(
    base_url=MONO_BASE,
    timeout=httpx.Timeout(30.0, connect=10.0),
    headers={"X-Token": token},   # <-- NBU: delete this line
)

async def aclose(self) -> None:
    await self._client.aclose()
```
The `aclose()` is load-bearing: `pyproject.toml` sets `filterwarnings=["error"]`,
so an unclosed `AsyncClient` escalates to a hard test failure (RESEARCH.md
"Note on filterwarnings"). The bootstrap/lifespan MUST `await client.aclose()`.

**Fetch + parse pattern** — use RESEARCH.md "Code Examples → NBU range fetch"
(the verified `exchange_site` shape) as the body. Critical excerpt (do NOT
deviate — Pitfall 1):
```python
raw = json.loads(resp.text, parse_float=Decimal)   # never float the rate
return [
    FxRateRow(
        rate_date=datetime.strptime(r["exchangedate"], "%d.%m.%Y").date(),
        currency=r["cc"],
        rate=Decimal(str(r["rate"])),
    )
    for r in raw          # raw == [] on weekend-only/unknown ccy → empty list (D-16)
]
```
Params are `{"start": YYYYMMDD, "end": YYYYMMDD, "valcode": currency, "json": ""}`.
Use `rate_per_unit` defensively if a currency reports `units != 1` (Assumptions
Log A3) — equals `rate` for USD/EUR/PLN/GBP/CHF.

**Typed-exception note:** Mono wraps `HTTPStatusError` into `MonoAuthError/...`
(monobank.py lines 73-80). NBU has NO auth and no 429 — do NOT copy that
branch. Let `tenacity` retry transient 5xx/network (3 attempts, exp backoff),
then `raise_for_status()`; a non-200 OR empty array means "no rates" (D-16) and
the caller leaves `bootstrap_done=false`.

---

### `src/finance_bro/importers/base.py` (MOD — port/protocol)

**Analog:** self — add `FxRatesPort` next to `ImporterProtocol`, and `FxRateRow`
next to `CanonicalTransaction`.

**Dataclass pattern** (base.py lines 15-42) — frozen dataclass with typed
fields:
```python
@dataclass(frozen=True)
class CanonicalTransaction:
    source_tx_id: str
    ...
    hold: bool = False
```
Add the new `FxRateRow` in the same style (D-02):
```python
@dataclass(frozen=True)
class FxRateRow:
    rate_date: date          # add `from datetime import date` to imports
    currency: str
    rate: Decimal            # add `from decimal import Decimal`
```

**Protocol pattern** (base.py lines 44-54):
```python
class ImporterProtocol(Protocol):
    source_kind: str
    async def discover_accounts(self) -> list[CanonicalAccount]: ...
    def fetch_statement(self, ...) -> AsyncIterator[CanonicalTransaction]: ...
```
Add the sibling (D-02), single method:
```python
class FxRatesPort(Protocol):
    async def fetch_range(
        self, currency: str, start: date, end: date
    ) -> list[FxRateRow]: ...
```

---

### `src/finance_bro/importers/currency_map.py` (MOD — utility, transform)

**Analog:** self. Extend the dict literal (currency_map.py lines 8-12) with the
long tail verified in RESEARCH.md ("Minor-currency availability"):
```python
_NUM_TO_ALPHA: dict[int, str] = {
    980: "UAH", 840: "USD", 978: "EUR",
    985: "PLN", 826: "GBP", 756: "CHF",   # add long tail (Discretion + D-15)
}
```
Per Discretion: if a numeric code isn't in the map for the `fx_source`
detection path (D-11), the planner may add a defensive fallback (return the
numeric code as string + log warning) rather than raising `ValueError` —
note that the current `numeric_to_alpha` (lines 15-19) RAISES on unknown
codes, which the Mono insert path relies on. Keep the raising behavior for the
account/transaction insert path; the fx_source op-currency lookup needs the
softer fallback. Planner decides whether to add a separate helper.

---

### `src/finance_bro/importers/monobank.py` (MOD — importer/adapter)

**Analog:** self — extend the `CanonicalTransaction` yield in `fetch_statement`
(monobank.py lines 127-137) with `attributed_day` (D-09). Use RESEARCH.md "Code
Examples → attributed_day on the importer boundary":
```python
from zoneinfo import ZoneInfo
KYIV = ZoneInfo("Europe/Kyiv")
# inside the yield (add to the existing CanonicalTransaction(...) call):
attributed_day=datetime.fromtimestamp(item["time"], tz=UTC).astimezone(KYIV).date(),
```
The existing yield already constructs `occurred_at` the same way (line 130) —
derive `attributed_day` from the same source. `raw_payload` already carries
`currencyCode` (fixture confirmed: `statement_two_items.json` has
`"currencyCode": 980`), so no new field is needed for the `mono_card` detection
— the rollup reads it out of `raw_payload` at read time (D-11).

---

### `src/finance_bro/db/fx_rate_repo.py` (NEW — repository, CRUD)

**Analog:** `src/finance_bro/db/account_repo.py` (the `upsert_many` ON CONFLICT
DO NOTHING path is the exact shape needed for D-03's idempotent rate upsert).

**Repo class + constructor pattern** (account_repo.py lines 17-19):
```python
class AccountRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
```

**ON CONFLICT DO NOTHING upsert pattern** (account_repo.py lines 49-75) — copy
verbatim, swap the table/columns and the conflict target to
`(rate_date, currency)` per D-03/D-04:
```python
rows = [{...} for a in items]
stmt = (
    insert(Account)
    .values(rows)
    .on_conflict_do_nothing(constraint="uq_accounts_source")  # -> index_elements=["rate_date","currency"]
    .returning(Account.id)
)
result = await self._s.execute(stmt)
return len(result.scalars().all())
```
Add a `count_in_window(currency, since_date) -> int` method for D-03's
"~250 rows in last 365 days" freshness threshold. Use the `text()` count
pattern from `import_run_repo.py` lines 160-174 (`SELECT count(*) ... WHERE ...`,
`return int(row[0]) if row else 0`).

---

### `src/finance_bro/db/tracked_fx_currency_repo.py` (NEW — repository, CRUD)

**Analog:** `src/finance_bro/db/scheduler_state_repo.py` (small singleton-ish
write helper) + `account_repo.py` (iterate + upsert). This repo needs:
iterate (`SELECT currency FROM tracked_fx_currencies ORDER BY currency` — D-17),
upsert-on-first-seen (ON CONFLICT DO NOTHING — D-15), and field updates
(`bootstrap_done`, `last_attempted_at`, `last_error` — D-08/D-16).

**Update pattern** (scheduler_state_repo.py lines 26-34) — copy this `text()`
UPDATE shape for setting bootstrap/error fields:
```python
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
For tracked currencies, the equivalents:
`set_bootstrap_done(currency)`, `mark_attempted(currency, last_error)` (sets
`last_attempted_at=now()`, `last_error=:err`).

**Iterate pattern** (account_repo.py lines 21-23) — `select(...).order_by(...)`
+ `.scalars().all()`.

---

### `src/finance_bro/db/transaction_repo.py` (MOD — repository)

**Analog:** self. Two edits, both with self-analogs in the same file.

**(1) `attributed_day` frozen-by-omission in `insert_many`** (transaction_repo.py
lines 46-72): add `"attributed_day": t.attributed_day` to the `rows` dict
(alongside `description`/`mcc`/`hold` at lines 54-60) and LEAVE IT OUT of the
`set_={...}` (lines 67-71). This is identical to how `description`/`mcc` are
already frozen-by-omission (D-09 / D-10 invariant — the docstring at lines 32-39
already names `attributed_day` in the frozen list). The set clause stays exactly
three columns:
```python
set_={
    "hold": stmt.excluded.hold,
    "amount_minor": stmt.excluded.amount_minor,
    "raw_payload": stmt.excluded.raw_payload,
},   # attributed_day NOT added here — frozen on first write
```

**(2) Replace `list_for_account` with the LATERAL join** (transaction_repo.py
lines 83-96). RESEARCH.md "Pattern 1 → Approach A" is the locked choice — use
`text()` matching D-14's literal SQL (the repo already uses `text()` for the
`NOT is_deleted` index_where, so raw SQL is established here). RESEARCH.md
provides the verbatim `ROLLUP_SQL`:
```python
ROLLUP_SQL = text("""
    SELECT t.id, t.account_id, t.source_tx_id, t.amount_minor, t.currency,
           t.time, t.hold, t.raw_payload, t.attributed_day,
           fx.rate AS fx_rate, fx.rate_date AS fx_rate_date
    FROM transactions t
    LEFT JOIN LATERAL (
        SELECT rate, rate_date FROM fx_rates
        WHERE currency = t.currency AND rate_date <= t.attributed_day
        ORDER BY rate_date DESC LIMIT 1
    ) fx ON true
    WHERE t.account_id = :account_id AND NOT t.is_deleted
    ORDER BY t.time DESC
""")
rows = (await session.execute(ROLLUP_SQL, {"account_id": account_id})).mappings().all()
```
Return type changes from `list[Transaction]` (ORM) to a list of mappings/rows
that carry the joined `fx_rate`/`fx_rate_date` — the route (below) feeds these
through `fx_rollup` then into `TransactionOut`. The `.mappings()` pattern is
already used in `import_run_repo.py` line 50 (`.mappings().one_or_none()`).

---

### `src/finance_bro/db/models.py` (MOD — model)

**Analog:** self (`ImportRun` lines 90-117, `SchedulerState` lines 120-130 for
table shape; `Transaction` lines 43-80 for column types).

Add two ORM models using the exact column idioms already in the file:
- `CHAR(3)` for currency (models.py line 31, 54).
- `Date` for `rate_date` (models.py line 67 — `attributed_day` already uses it).
- `TIMESTAMP(timezone=True), server_default=text("now()")` for `fetched_at`/
  `first_seen_at`/`last_attempted_at` (models.py lines 34-36).
- `Boolean, server_default=text("false")` for `bootstrap_done` (models.py
  lines 57, 62-64).
- `NUMERIC(18,8)` for `rate` — NOT yet imported; add `Numeric` to the
  `sqlalchemy` import block (models.py lines 4-16) and map as
  `Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)`.

Index pattern (models.py lines 72-80, the `Index(...)` in `__table_args__`) —
add `Index("ix_fx_rates_currency_rate_date", "currency", "rate_date")` (D-04
covering index for the LATERAL lookup). PK is `(rate_date, currency)` via
`PrimaryKeyConstraint` (see migration analog).

Change `attributed_day` (models.py line 67) from `nullable=True` to
`nullable=False` (D-09).

---

### `src/finance_bro/services/fx_rollup.py` (NEW — service/utility, transform)

**Analog:** No exact analog (see No Analog Found). Closest structural sibling is
`services/import_service.py` for the `@dataclass(frozen=True)` result + module
docstring style, but the Decimal math is new to the codebase.

Use RESEARCH.md "Code Examples → UAH rollup math" verbatim — it encodes D-11
(fx_source three-way), D-12 (no-rate → all-null + fx_stale), D-13 (stale iff
`fx_rate_date < attributed_day`), and D-14 (banker's rounding). The
load-bearing math (must be copied exactly — Pitfall 1 + Pitfall 2):
```python
from decimal import Decimal, ROUND_HALF_EVEN
major = (Decimal(amount_minor) / 100) * fx_rate
uah_minor = int(major.quantize(Decimal("0.01"), ROUND_HALF_EVEN) * 100)
```
fx_source detection (D-11, audit-only label — math is identical for mono_card
and nbu):
```python
source = "mono_card" if (op_currency_alpha and op_currency_alpha != currency) else "nbu"
```
The `op_currency_alpha` comes from `numeric_to_alpha(raw_payload["currencyCode"])`
at the call site (route). Project-wide `getcontext().prec = 28` is assumed
already set (CLAUDE.md §Money) — planner should verify it's set at app import
(check `core/` or `domain/money.py`); if not, set it in this module.

---

### `src/finance_bro/services/fx_bootstrap.py` (NEW — service, event-driven)

**Analog:** `src/finance_bro/services/import_service.py`

**Service class + session-factory constructor pattern** (import_service.py
lines 43-50):
```python
class ImportService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        importer: MonobankImporter,
    ) -> None:
        self._session_factory = session_factory
        self._importer = importer
```
For `fx_bootstrap`, inject the `NbuFxImporter` (`FxRatesPort`) + session factory.

**Session-per-unit-of-work pattern** (import_service.py lines 57-66 + runner.py
lines 67-71) — `async with self._session_factory() as session, session.begin():`
for each DB write block; do HTTP fetch OUTSIDE the session block (import_service
does exactly this: discovery HTTP at line 62, then a fresh session at line 65
for the write). `maybe_bootstrap_fx(currency)` (D-03):
1. read count via `FxRateRepo.count_in_window` (session block)
2. if below threshold → `await importer.fetch_range(...)` (no session)
3. `FxRateRepo` upsert + `TrackedFxCurrencyRepo.set_bootstrap_done` (session block)
4. on empty/failure → `mark_attempted(currency, "no rates published")` (D-16)

`maybe_bootstrap_fx_all_tracked()` iterates `TrackedFxCurrencyRepo` and calls
`maybe_bootstrap_fx` sequentially (D-07/D-17 — sequential, not gathered).

---

### `src/finance_bro/scheduler/runner.py` (MOD — scheduler job, event-driven)

**Analog:** self. Add an `fx_tick` coroutine method (or module-level function)
alongside `tick` (runner.py lines 235-350). The `fx_tick` body (D-17): iterate
`tracked_fx_currencies ORDER BY currency`, fetch today's rate per currency,
upsert, `mark_attempted`; re-run 12-month range if `bootstrap_done=false`.
Reuse `fx_bootstrap.maybe_bootstrap_fx` for the bootstrap-incomplete branch.

**Error-isolation-per-tick pattern** (runner.py lines 255-260) — wrap the work
so one currency's failure doesn't abort the others; log + continue:
```python
try:
    await self.recover_in_flight()
except Exception:  # noqa: BLE001
    _log.exception("scheduler.tick.recover.failed")
```
**structlog logging pattern** (runner.py lines 45, 322-329) — `_log =
structlog.get_logger()`; structured events like
`_log.info("fx.tick.currency.done", currency=ccy, rows=n)`. Do NOT touch
`scheduler_state` (D-08) — that's Mono-auth-only (runner.py `_set_state_auth_failed`
lines 358-361 is the anti-pattern to avoid here).

---

### `src/finance_bro/main.py` (MOD — config/lifespan wiring)

**Analog:** self (lifespan `add_job` + `scheduler.start()` at main.py lines
80-88). Two additions per D-06/D-07, both AFTER `scheduler.start()`.

**add_job pattern** (main.py lines 80-87) — copy and swap the trigger to
`CronTrigger` (RESEARCH.md Pattern 2):
```python
scheduler.add_job(
    runner.tick,
    IntervalTrigger(seconds=10),       # -> CronTrigger(hour=16, minute=0,
    id="finance-bro-tick",             #      timezone=ZoneInfo("Europe/Kyiv"))
    max_instances=1, coalesce=True,    # id="fx_tick"
    misfire_grace_time=30,             # misfire_grace_time=3600 (D-06)
)
```
Add `from apscheduler.triggers.cron import CronTrigger` and
`from zoneinfo import ZoneInfo` to imports (main.py lines 32-33 currently import
only `IntervalTrigger`).

**Fire-and-forget bootstrap** (D-07) — add after `scheduler.start()`:
`asyncio.create_task(fx_bootstrap_service.maybe_bootstrap_fx_all_tracked())`.
Construct the `NbuFxImporter` next to `MonobankImporter` (main.py line 63) and
ensure its `aclose()` is called in the `finally` block alongside
`runner.aclose()` (main.py line 97) — CR-01 invariant: any HTTP-client owner
must be closed in the `finally` that owns it, else `filterwarnings=["error"]`
escalates the unclosed-client warning.

Respect `APP_DISABLE_SCHEDULER` (main.py line 78) — the fx_tick registration
must sit inside the same `if state == "running" and not disable_scheduler:`
guard so test mode (conftest.py line 43 sets it) doesn't fire the cron.

---

### `src/finance_bro/api/schemas.py` (MOD — Pydantic DTO)

**Analog:** self (`TransactionOut` lines 34-45). Add five fields per D-10 inside
the existing model (keep `model_config = ConfigDict(from_attributes=True)`):
```python
from datetime import date          # add to imports (currently only datetime)
from typing import Literal          # add to imports

class TransactionOut(BaseModel):
    ...
    uah_amount_minor: int | None = None
    fx_rate: str | None = None                 # Decimal-as-string, never float (CLAUDE.md §Money)
    fx_rate_date: date | None = None
    fx_source: Literal["native_uah", "mono_card", "nbu"]
    fx_stale: bool
```
Note the file docstring (lines 1-8) asserts "all money on the JSON boundary is
integer minor units ... never str, never float, never Decimal" — `fx_rate` is
the deliberate D-10 exception (string transport for the rate); update the
docstring to record it.

---

### `src/finance_bro/api/routes_transactions.py` (MOD — route)

**Analog:** self (routes_transactions.py lines 22-30). Since `list_for_account`
now returns joined rows (not ORM `Transaction`), the route maps each row
through `fx_rollup.rollup(...)` and merges the result into `TransactionOut`.
Keep the "no card → empty list" guard (lines 26-28). The op-currency for the
rollup comes from `numeric_to_alpha(row["raw_payload"]["currencyCode"])` (with
the defensive fallback per D-11). Planner decides whether to build the
`TransactionOut` directly or via `model_validate` over a merged dict.

---

### `alembic/versions/0003_fx_truth.py` (NEW — migration, batch)

**Analog:** `alembic/versions/0002_phase2_sync.py`

**Revision header pattern** (0002 lines 1-15):
```python
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None
```
0003: `revision="0003"`, `down_revision="0002"`.

**create_table + seed pattern** (0002 lines 28-48) — `op.create_table(...)` with
`sa.Column`, `sa.CheckConstraint`, `sa.PrimaryKeyConstraint`, then
`op.execute("INSERT ...")` to seed. Use this for both `fx_rates` (D-04) and
`tracked_fx_currencies` (D-05). Seed USD+EUR exactly as 0002 seeds the
scheduler_state singleton (line 48):
```python
op.execute(
    "INSERT INTO tracked_fx_currencies (currency, bootstrap_done) "
    "VALUES ('USD', false), ('EUR', false)"
)
```

**create_index pattern** (0002 lines 95-100):
```python
op.create_index(
    "ix_fx_rates_currency_rate_date", "fx_rates",
    ["currency", "rate_date"], postgresql_using="btree",
)
```
(D-04 wants `rate_date DESC`; for a btree the planner can scan backward, so a
plain index suffices — or use `postgresql_ops` / `text("rate_date DESC")` if
strict DESC ordering is desired. Planner's call.)

**backfill + SET NOT NULL ordering** (D-09 + Pitfall 3 — UPDATE BEFORE the
ALTER, same transaction; Postgres DDL is transactional):
```python
op.execute(
    "UPDATE transactions SET attributed_day = "
    "(time AT TIME ZONE 'Europe/Kyiv')::date WHERE attributed_day IS NULL"
)
op.alter_column("transactions", "attributed_day", nullable=False)
```
The `op.execute("UPDATE ...")` data-migration pattern is exactly 0002 lines
21-25 (the `mono_type` backfill). No rate rows seeded — lifespan bootstrap fills
`fx_rates` (D-04 bullet).

**downgrade pattern** (0002 lines 110-115) — drop in reverse order; re-add the
nullable on `attributed_day`, drop the index, drop both tables.

---

### `src/finance_bro/core/settings.py` (MOD — optional, planner's call)

**Analog:** self. If adding `nbu_base` for symmetry (RESEARCH.md Runtime State
Inventory), add a defaulted field to `Settings` (settings.py lines 12-15):
```python
nbu_base: str = "https://bank.gov.ua/NBU_Exchange/exchange_site"
```
`extra="ignore"` (line 21) already tolerates unknown env. A hardcoded constant
in `nbu.py` (mirroring `MONO_BASE`) is equally acceptable per Discretion.

## Shared Patterns

### Repository constructor + session ownership
**Source:** every file in `src/finance_bro/db/` (e.g. `account_repo.py` lines
17-19, `transaction_repo.py` lines 21-23)
**Apply to:** `fx_rate_repo.py`, `tracked_fx_currency_repo.py`
```python
def __init__(self, session: AsyncSession) -> None:
    self._s = session
```
No SQLAlchemy leaks outside `db/` — repos take an `AsyncSession`, callers own
the `session.begin()` transaction boundary (runner.py line 67, import_service.py
line 57).

### Idempotent upsert via ON CONFLICT
**Source:** `account_repo.py` lines 67-75 (DO NOTHING), `transaction_repo.py`
lines 64-76 (DO UPDATE, frozen-by-omission)
**Apply to:** `fx_rate_repo` (DO NOTHING on `(rate_date, currency)` — D-03),
`tracked_fx_currency_repo` (DO NOTHING on `currency` PK — D-15)
```python
from sqlalchemy.dialects.postgresql import insert
stmt = insert(Model).values(rows).on_conflict_do_nothing(index_elements=[...])
```

### No-float-for-money / string rate transport
**Source:** CLAUDE.md §"Money / Decimal Handling"; RESEARCH.md Pitfall 1
**Apply to:** `nbu.py` (parse), `fx_rollup.py` (math), `schemas.py` (transport),
`models.py` (`NUMERIC(18,8)`)
- Input: `json.loads(resp.text, parse_float=Decimal)` — never `resp.json()` on a
  rate.
- Math: `Decimal` + `ROUND_HALF_EVEN` + `.quantize(Decimal("0.01"))`.
- Transport: `fx_rate` is `str` (`f"{rate:.8f}"`), never float in JSON.

### httpx.AsyncClient lifecycle + filterwarnings trap
**Source:** `monobank.py` lines 60-67; `main.py` lines 67-97 (CR-01); RESEARCH.md
"Note on filterwarnings"
**Apply to:** `nbu.py` (build client in `__init__`, expose `aclose()`),
`main.py` (close NBU client in the lifespan `finally` block)
Any unclosed `AsyncClient` under `filterwarnings=["error"]` (pyproject.toml) is a
hard test failure. The DB-touching startup calls must run INSIDE the try/finally
that owns the client.

### structlog structured logging (logs-only failure surface)
**Source:** `runner.py` line 45 + lines 322-329; D-08
**Apply to:** `fx_bootstrap.py`, `fx_tick` in `runner.py`
```python
_log = structlog.get_logger()
_log.info("fx.bootstrap.done", currency=ccy, rows=n)
```
FX failures go to logs + `tracked_fx_currencies.last_error` ONLY — do NOT touch
`scheduler_state`, do NOT extend `/api/import/status` (D-08).

### Session-per-unit-of-work + HTTP-outside-session
**Source:** `import_service.py` lines 57-66; `runner.py` lines 67, 297-321
**Apply to:** `fx_bootstrap.py`, `fx_tick`
Open a session block per write; do the `await importer.fetch_range(...)` HTTP
call OUTSIDE any open session so a slow NBU response doesn't hold a DB
transaction open.

## Test Patterns

### respx HTTP mock (NBU adapter tests)
**Source:** `tests/test_importer_statement.py` lines 1-34, 100-119
**Apply to:** `test_fx_importer_nbu.py`
```python
import respx, httpx
with respx.mock(base_url="https://bank.gov.ua") as mock:
    mock.get(url__regex=r"/NBU_Exchange/exchange_site.*").mock(
        return_value=httpx.Response(200, json=fixture_payload)
    )
    imp = NbuFxImporter()
    rows = await imp.fetch_range("USD", start, end)
    await imp.aclose()      # MANDATORY — filterwarnings=["error"]
```
Empty-array case: `httpx.Response(200, json=[])` → assert `rows == []` (D-16).
Build fixtures `tests/fixtures/nbu_usd_range.json` + `nbu_empty.json` mirroring
the real `exchange_site` shape (incl. `exchangedate` `dd.mm.yyyy`, `calcdate`,
`cc`, `rate`) — analog fixture: `tests/fixtures/statement_two_items.json`.

### testcontainers Postgres + session_factory (LATERAL/migration tests)
**Source:** `tests/conftest.py` lines 25-71; `tests/test_schema_invariants.py`
**Apply to:** `test_fx_rollup_join.py`, `test_fx_on_card.py`,
`test_fx_stale_fallback.py`, `test_fx_bootstrap_lazy.py`
Use the `session_factory` fixture; INSERT raw rows via `text()` (schema_invariants
lines 6-33 is the template), then call the repo. For the Sunday→Friday test
(D-14), seed `fx_rates` with ONLY the Friday row (RESEARCH.md: NBU carries
forward, so the sparse table is what exercises the LATERAL fallback).

### Alembic up/down round-trip + column assertion (migration tests)
**Source:** `tests/test_migrations.py` lines 1-53
**Apply to:** `test_attributed_day_migration.py`
Use `run_alembic` (conftest.py lines 15-22) via `asyncio.to_thread`. For the
backfill test: downgrade to `0002`, INSERT a transaction with
`attributed_day=NULL`, upgrade to `0003` (or `head`), assert the row's
`attributed_day` is Kyiv-correct (Pitfall 3). Assert `fx_rates` +
`tracked_fx_currencies` tables exist post-upgrade (migrations test lines 30-33
pattern) and the USD/EUR seed rows are present.

### freezegun cron-fire-time (DST test)
**Source:** none existing — `freezegun==1.5.5` is installed (RESEARCH.md). New
pattern.
**Apply to:** `test_fx_cron_dst.py`
Assert the `CronTrigger(hour=16, timezone=ZoneInfo("Europe/Kyiv"))` next-fire
time around the last-Sunday-of-October boundary (RESEARCH.md Pattern 2 — assert
the computed next fire, no real hour needs to pass).

### property/unit test (Decimal rounding)
**Source:** `tests/test_money_invariants.py` (existing money-invariant style)
**Apply to:** `test_fx_rollup_math.py`
Pure-function test of `fx_rollup.rollup(...)` — assert mono_card and nbu rows
with identical account-amount + day produce IDENTICAL uah_amount_minor (FX-04
no-double-conversion property, RESEARCH.md Pitfall 2).

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `src/finance_bro/services/fx_rollup.py` | service/utility | transform (Decimal FX math) | No existing module does multi-currency Decimal rollup math. The `@dataclass`/docstring shell copies from `import_service.py`, but the `Decimal * rate → quantize → minor-units` body has no codebase precedent — use RESEARCH.md "Code Examples → UAH rollup math" verbatim (it encodes D-11..D-14). The banker's-rounding + `parse_float=Decimal` discipline comes from CLAUDE.md §Money + Pitfall 1, not from a sibling file. |
| `test_fx_cron_dst.py` (freezegun) | test | n/a | No existing test uses `freezegun` or asserts a `CronTrigger` fire time; this is a net-new test pattern (lib is installed). Pattern source is RESEARCH.md Pattern 2, not a sibling test. |

## Metadata

**Analog search scope:** `src/finance_bro/importers/`, `src/finance_bro/db/`,
`src/finance_bro/services/`, `src/finance_bro/scheduler/`, `src/finance_bro/api/`,
`src/finance_bro/core/`, `alembic/versions/`, `tests/`, `tests/fixtures/`
**Files scanned:** 16 source files + 6 test files + 2 migrations + 1 fixture read in full
**Pattern extraction date:** 2026-05-30
