---
phase: 01-first-real-transaction
plan: 03
subsystem: api
tags: [fastapi, sqlalchemy, on-conflict-do-nothing, importer-orchestrator, pydantic-v2, respx, testcontainers, structlog-redaction]

requires:
  - phase: 01-first-real-transaction
    plan: 01
    provides: "Account/Transaction/MonoRateState models, partial unique idx uq_transactions_account_source_tx WHERE NOT is_deleted, set_engine() / get_session_factory(), Settings (mono_token, database_url, log_level), structlog redaction processor, testcontainers Postgres conftest with engine + session_factory + client fixtures, tests/fixtures/{client_info_minimal,statement_two_items}.json"
  - phase: 01-first-real-transaction
    plan: 02
    provides: "MonobankImporter(token, gate) with discover_accounts + fetch_statement (X-Token-only auth, numeric→alpha at boundary, int amount_minor, verbatim raw), RateLimitGate(session_factory) with persistent FOR UPDATE acquire, MONO_RATE_LIMIT_SECONDS=65, CanonicalAccount/CanonicalTransaction frozen dataclasses"
provides:
  - "AccountRepo (list_all / get_first_card / upsert_many) — idempotent INSERT ... ON CONFLICT DO NOTHING via uq_accounts_source with RETURNING for accurate insert count"
  - "TransactionRepo (insert_many / list_for_account) — INSERT ... ON CONFLICT (account_id, source_tx_id) WHERE NOT is_deleted DO NOTHING with RETURNING; list ordered by time DESC, is_deleted=false"
  - "ImportService.run_one_card(now=None) -> ImportResult — lazy discovery (D-03), persists every Mono account on first run (D-05), picks the first mono.card (D-04), 31-day statement window (Pitfall 5)"
  - "NoCardAccountFound error class — surfaces as HTTP 409 from /api/import when client-info returned no accounts or no mono.card exists"
  - "Pydantic response models: HealthOut, AccountOut, TransactionOut (amount_minor: int, raw_payload: dict — D-10 / FX-01 / threat T6), ImportResultOut (D-08)"
  - "FastAPI dependency providers (api/deps.py): get_session, get_rate_gate, get_importer, get_import_service — single shared RateLimitGate per process (Pitfall 9)"
  - "Four FastAPI routes mounted in main.py via include_router: GET /api/health (D-09), GET /api/accounts (D-09), GET /api/transactions (D-07/D-10), POST /api/import (D-08)"
  - "main.py lifespan now calls init_engine() in addition to logging.configure(); init_engine() is a no-op when set_engine() has been called (test-mode)"
  - "conftest `client` fixture truncates accounts/transactions/mono_rate_state before yielding so route tests that load the same Mono fixtures don't collide across the same pytest session"
affects: [01-04-compose-deploy, 02-back-fill-and-multi-account]

tech-stack:
  added: []
  patterns:
    - "Pattern 3 (RESEARCH.md): Synchronous Import Endpoint — POST /api/import is in-line, takes no body, calls ImportService.run_one_card, returns 200 + ImportResultOut. No background queue, no scheduler in Phase 1 (D-02)."
    - "ON CONFLICT DO NOTHING ... RETURNING id: SQLAlchemy postgresql.insert dialect chained .on_conflict_do_nothing(index_elements=[...], index_where=text('NOT is_deleted')).returning(Transaction.id). Counting len(result.scalars().all()) gives exact inserted count without `rowcount` (which is unknown-typed under basedpyright strict for INSERT...RETURNING)."
    - "Lazy discovery: ImportService.run_one_card reads accounts from DB first; only if empty does it call importer.discover_accounts() (D-03 — no rate budget burned on app startup; D-06 — discovery is one-shot)."
    - "`from_attributes=True` on Pydantic response models so AccountOut/TransactionOut.model_validate(row) works directly against SQLAlchemy ORM rows."
    - "FastAPI Annotated[T, Depends(...)] pattern (PEP 612 ergonomics, basedpyright-strict-friendly) instead of default-arg `param: T = Depends(...)` form."
    - "Per-test DB cleanup happens at the `client` fixture (truncates accounts/transactions/mono_rate_state) — not as an autouse fixture, so existing direct-DB tests that use unique source_account_id values keep their own isolation and don't pull testcontainers Postgres into tests that don't need it."

key-files:
  created:
    - "src/finance_bro/db/account_repo.py"
    - "src/finance_bro/db/transaction_repo.py"
    - "src/finance_bro/services/__init__.py"
    - "src/finance_bro/services/import_service.py"
    - "src/finance_bro/api/__init__.py"
    - "src/finance_bro/api/schemas.py"
    - "src/finance_bro/api/deps.py"
    - "src/finance_bro/api/routes_health.py"
    - "src/finance_bro/api/routes_accounts.py"
    - "src/finance_bro/api/routes_transactions.py"
    - "src/finance_bro/api/routes_import.py"
    - "tests/test_health.py"
    - "tests/test_import_route.py"
    - "tests/test_idempotency.py"
    - "tests/test_transactions_route.py"
  modified:
    - "src/finance_bro/main.py"
    - "tests/conftest.py"

key-decisions:
  - "AccountRepo.upsert_many uses RETURNING + len(scalars().all()) instead of result.rowcount. basedpyright strict flags `result.rowcount` on a CursorResult as `reportUnknownMemberType` / `reportAttributeAccessIssue` (the Result protocol doesn't expose `rowcount` as part of its strict-typed surface for INSERT statements). Switching to RETURNING gives the exact same answer with strict-clean typing and matches TransactionRepo.insert_many's pattern (consistency)."
  - "Per-test cleanup is wired into the `client` fixture, not as an autouse fixture in conftest.py. An autouse cleanup that depends on `session_factory` would force every test in the project to spin up testcontainers Postgres — including unit-only tests like `test_no_auth.py::test_no_auth_middleware` — which is a regression risk. Putting cleanup inside `client` keeps the contract tight: route tests get isolation; non-route tests are unchanged."
  - "Annotated[T, Depends(...)] in deps.py and route handlers, not the legacy `param: T = Depends(...)` default-arg form. Plan code used the legacy form; the Annotated form is equivalent at runtime, idiomatic in modern FastAPI, and avoids ruff B008 (function-call-in-default-argument)."
  - "Module docstrings in route files dropped the literal '/api/{path}' substring after Task 2 verification — plan acceptance criterion required `grep -c '/api/{path}' file.py == 1`. Module docstring + decorator gave 2 matches; rephrasing docstrings to 'Health endpoint' / 'Import endpoint' / etc. keeps the unique literal grep gate as the contract evidence (matches the docstring hygiene fix Plan 02 already applied for the X-Token literal)."
  - "main.py lifespan calls init_engine() in addition to logging.configure(). init_engine() is idempotent: if set_engine() has already been called (test mode via conftest), the engine slot is non-None and init_engine() is a no-op. In production, init_engine() lazily creates the async engine from settings on first /api/* request."

patterns-established:
  - "Repos own all SQL for their table — no SQL leaks to ImportService or to route handlers. Route handlers depend on AsyncSession; services depend on session_factory; repos take an AsyncSession and execute the actual statements."
  - "`session.begin()` block per write operation in services — explicit transaction boundary ensures the partial-unique-index check happens inside a single tx (no read-then-insert race window for the import path)."
  - "Empty-result handling: GET /api/transactions returns `[]` when no card exists yet (instead of 404). Frontend renders an empty dashboard before the first /api/import — better UX than a hard error on cold start."
  - "Idempotency surface area: `inserted` is exactly count(rows-with-RETURNING-row); `skipped_duplicates = statement_count - inserted`. The /api/import response shape lets the user see at a glance whether a poll yielded new data or was a no-op (D-08 / SC#3)."
  - "Log redaction is layered defense: route handler explicitly logs ONLY structural counts in `import.done`; the structlog processor (Plan 01) redacts any token-shaped substring or token/amount-named keys at INFO+ as a backstop. test_no_token_in_info_logs_full_cycle exercises both layers across a full POST /api/import + GET /api/transactions cycle (SC#5 partial)."

decisions:
  - "Per-test cleanup wired into `client` fixture (not autouse) to avoid forcing testcontainers Postgres on unit-only tests"
  - "AccountRepo.upsert_many uses RETURNING for inserted count (basedpyright strict flags result.rowcount as unknown-typed)"
  - "Annotated[T, Depends(...)] dependency injection form over legacy default-arg form (ruff B008-clean)"
  - "main.py lifespan calls init_engine() — idempotent; respects set_engine() already called by tests"

metrics:
  duration_seconds: 463
  duration_human: "~7 min"
  task_count: 2
  files_created: 15
  files_modified: 2
  commits: 3

requirements-completed: [ING-01, ING-03, ING-04, ING-07, FX-01, OPS-01, OPS-04, DEP-02]

duration: ~7 min
completed: 2026-05-10
---

# Phase 1 Plan 3: Importer Service + Read Endpoints Summary

**Walking-skeleton vertical slice complete: POST /api/import drives lazy Mono discovery → first-card pick → 31-day statement fetch → ON CONFLICT DO NOTHING insert; GET /api/{health,accounts,transactions} read it back; second POST is a no-op (SC#3); INFO logs across the full cycle leak zero token / X-Token / amount substrings (SC#5 partial). 42/42 tests green (33 prior + 9 new), ruff + basedpyright (src/) clean.**

## Performance

- **Duration:** ~7 min
- **Started:** 2026-05-10T12:55:29Z
- **Completed:** 2026-05-10T13:03:12Z
- **Tasks:** 2 (both `tdd="true"`)
- **Files created:** 15
- **Files modified:** 2 (`src/finance_bro/main.py`, `tests/conftest.py`)
- **Commits:** 3 (1 feat for Task 1; 1 RED test + 1 GREEN feat for Task 2)

## Accomplishments

- **End-to-end SC#1 / SC#2 / SC#3 walking skeleton verified.** POST /api/import (no body) returns ImportResultOut JSON with `polled_account_id="card-id-1"`, `statement_count=2`, `inserted=2`, `skipped_duplicates=0` against respx-mocked Mono. GET /api/transactions returns 2 rows ordered by `time DESC` with int `amount_minor`, length-3 `currency`, dict `raw_payload` (verbatim Mono `statementItem`). GET /api/accounts returns both the card and the jar (D-05 — every Mono account persisted on first import).
- **Idempotency demonstrated end-to-end (SC#3).** `tests/test_idempotency.py::test_second_import_is_noop` runs two POSTs back-to-back: first returns `inserted=2, skipped_duplicates=0`; second returns `inserted=0, skipped_duplicates=2`. GET /api/transactions still returns exactly 2 rows. The partial unique index from migration 0001 (`uq_transactions_account_source_tx WHERE NOT is_deleted`) is the on-disk invariant; `TransactionRepo.insert_many` uses `postgresql.insert(...).on_conflict_do_nothing(index_elements=["account_id","source_tx_id"], index_where=text("NOT is_deleted")).returning(Transaction.id)` to count exactly what landed.
- **Full-cycle log redaction proved (SC#5 partial).** `test_no_token_in_info_logs_full_cycle` runs POST /api/import + GET /api/transactions through the same `client` and asserts `MONO_TOKEN`, the literal `X-Token` substring, and the three statement amounts (`-8500`, `8500`, `5000000`) are absent from `caplog.text` at INFO. The route handler logs only structural counters; the structlog redaction processor (Plan 01) is the defense-in-depth backstop. Plan 04 will close the full SC#5 against `docker logs` on a live container.
- **Lazy discovery (D-03 + D-06) implemented.** `ImportService.run_one_card` reads accounts from the DB first; only if empty does it call `importer.discover_accounts()`. App startup is silent — no /personal/client-info call, no rate budget burned. Subsequent imports skip discovery entirely. The 31-day statement window (Pitfall 5) is computed inline (`now - timedelta(days=31)`).
- **Pydantic response shape locked (FX-01 / D-10 / threat T6).** `TransactionOut.amount_minor: int` (basedpyright strict). `test_response_shape` asserts `isinstance(row["amount_minor"], int)` and `not isinstance(..., bool)` for every row. JSON over-the-wire stays integer minor units — no float drift, no string-encoded numbers, no Decimal. `raw_payload: dict[str, Any]` round-trips the Mono `statementItem` verbatim (`test_raw_payload_verbatim` compares element-by-element against the fixture).
- **No regressions in Plans 01-01 / 01-02.** Full project suite is 42 green (33 prior + 9 new). Ruff + basedpyright (src/) clean.

## Task Commits

1. **Task 1: Repos + ImportService + Pydantic schemas** — `11c923b` (feat)
2. **Task 2 RED: failing tests for /api/{health,accounts,transactions,import}** — `ad43815` (test)
3. **Task 2 GREEN: wire FastAPI routes** — `d4e94ce` (feat)

## Files Created/Modified

### Created (15)

- `src/finance_bro/db/account_repo.py` — `AccountRepo(session)` with `list_all`, `get_first_card`, `upsert_many` (RETURNING for accurate insert count)
- `src/finance_bro/db/transaction_repo.py` — `TransactionRepo(session)` with `insert_many` (ON CONFLICT DO NOTHING + RETURNING) and `list_for_account` (DESC by time, is_deleted=false)
- `src/finance_bro/services/__init__.py` — package marker
- `src/finance_bro/services/import_service.py` — `ImportResult` dataclass, `NoCardAccountFound` exception, `ImportService(session_factory, importer).run_one_card(now=None) -> ImportResult`
- `src/finance_bro/api/__init__.py` — package marker
- `src/finance_bro/api/schemas.py` — `HealthOut`, `AccountOut`, `TransactionOut` (`amount_minor: int`, `raw_payload: dict[str, Any]`), `ImportResultOut`
- `src/finance_bro/api/deps.py` — `get_session`, `get_rate_gate`, `get_importer`, `get_import_service` (Annotated[T, Depends(...)] form)
- `src/finance_bro/api/routes_health.py` — `GET /api/health` returns `{status: ok, db: ok}` (or `db: error` on SELECT 1 failure)
- `src/finance_bro/api/routes_accounts.py` — `GET /api/accounts` returns `list[AccountOut]`
- `src/finance_bro/api/routes_transactions.py` — `GET /api/transactions` scoped to first card, ordered DESC; returns `[]` when no card exists
- `src/finance_bro/api/routes_import.py` — `POST /api/import` calls `ImportService.run_one_card`; raises HTTP 409 on `NoCardAccountFound`; logs only structural counters
- `tests/test_health.py` — 2 tests (db ok, no auth)
- `tests/test_import_route.py` — 4 tests (first import, all accounts persisted, raw_payload verbatim, no token/amount in INFO logs across full cycle)
- `tests/test_idempotency.py` — 1 test (SC#3 — second POST is no-op)
- `tests/test_transactions_route.py` — 2 tests (response shape, time DESC ordering)

### Modified (2)

- `src/finance_bro/main.py` — lifespan now calls `init_engine()` in addition to `logging.configure()`; mounts the four routers via `include_router`
- `tests/conftest.py` — `client` fixture truncates `transactions, accounts, mono_rate_state RESTART IDENTITY CASCADE` before yielding so route tests don't collide on shared fixture IDs (`card-id-1`, `jar-id-1`) across the same pytest session

## Decisions Made

- **`AccountRepo.upsert_many` uses RETURNING + `len(result.scalars().all())` instead of `result.rowcount`.** basedpyright strict flags `CursorResult.rowcount` as `reportUnknownMemberType` / `reportAttributeAccessIssue`. Switching to RETURNING gives the exact same count with strict-clean typing and matches `TransactionRepo.insert_many`'s pattern (consistency).
- **Per-test cleanup wired into `client` fixture, NOT as an autouse fixture in conftest.** An autouse fixture that depends on `session_factory` would force every test in the project to spin up testcontainers Postgres — including pure-unit tests like `test_no_auth.py::test_no_auth_middleware` that don't touch the DB at all. Putting cleanup inside `client` keeps the cleanup scoped to tests that already need the HTTP layer.
- **`Annotated[T, Depends(...)]` over the legacy `param: T = Depends(...)` form.** The plan code used the default-arg form; switching to Annotated is functionally equivalent at runtime, modern FastAPI idiom, and avoids ruff B008 (function-call-in-default-argument). Type-checker sees the same dependency graph.
- **Route module docstrings dropped the `/api/{path}` literal.** Plan acceptance gate `grep -c '/api/{path}' file.py == 1` required exactly one match — the route decorator. Module docstrings that included the literal path produced 2 matches. Rephrased docstrings (`Health endpoint`, `Import endpoint`, etc.) keep the unique grep gate as the contract evidence (same docstring-hygiene fix Plan 02 applied for the `X-Token` literal).
- **`main.py` lifespan calls `init_engine()` in addition to `logging.configure()`.** `init_engine()` is idempotent: if `set_engine()` has already been called by the conftest's `session_factory` fixture (test mode), the engine slot is non-None and `init_engine()` is a no-op. In production, `init_engine()` lazily creates the async engine from settings on first request without surprising the test harness.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Plan's `result.rowcount` would fail basedpyright strict on `AccountRepo.upsert_many`**
- **Found during:** Task 1 strict type-check.
- **Issue:** `result.rowcount` on a `CursorResult` is `reportUnknownMemberType` and `reportAttributeAccessIssue` under basedpyright strict mode. The plan's verbatim snippet (`return result.rowcount or 0`) would have produced 3 strict errors.
- **Fix:** Chained `.returning(Account.id)` onto the insert and returned `len(result.scalars().all())`. Same numeric answer, strict-clean typing, matches `TransactionRepo.insert_many`'s pattern.
- **Files modified:** `src/finance_bro/db/account_repo.py`
- **Verification:** `uv run basedpyright src/` → `0 errors, 0 warnings, 0 notes`.
- **Committed in:** `11c923b` (Task 1)

**2. [Rule 1 - Bug] Plan acceptance grep `amount_minor: int == 1` would fail with the schemas module docstring**
- **Found during:** Task 1 acceptance gate (`grep -c 'amount_minor: int' src/finance_bro/api/schemas.py | grep -q '^1$'`).
- **Issue:** Initial draft included the literal `amount_minor: int` substring in the module docstring AND in the Pydantic field. Two matches. Plan required exactly one.
- **Fix:** Rephrased docstring to "integer minor units typed as Python `int`". The unique `amount_minor: int` literal is now only on the Pydantic field declaration, which is the contract point.
- **Files modified:** `src/finance_bro/api/schemas.py`
- **Verification:** `grep -c 'amount_minor: int' src/finance_bro/api/schemas.py` returns `1`.
- **Committed in:** `11c923b` (Task 1)

**3. [Rule 3 - Blocking] No per-test DB cleanup; route tests would collide on `card-id-1` across the same pytest session**
- **Found during:** Sketching the test plan — `test_first_import_discovers_and_inserts` inserts `card-id-1`, then `test_all_accounts_persisted` would re-import it and either see `inserted=0, skipped_duplicates=2` (breaking the assertion) or duplicate-key on accounts.upsert_many (skipped by ON CONFLICT, so the actual failure mode is the `inserted == 2` assertion). The Postgres testcontainer is session-scoped — state persists across tests.
- **Fix:** Modified the existing `client` fixture in `tests/conftest.py` to TRUNCATE `transactions, accounts, mono_rate_state RESTART IDENTITY CASCADE` before yielding. Scoped to the `client` fixture (not autouse) so non-route tests aren't forced to depend on testcontainers Postgres.
- **Files modified:** `tests/conftest.py`
- **Verification:** All 42 tests pass (9 new + 33 prior). The new route tests run independently in any order; existing direct-DB tests that use unique source_account_id values are unaffected.
- **Committed in:** `ad43815` (Task 2 RED — landed alongside the failing tests since they require this fixture)

**4. [Rule 1 - Bug] Plan acceptance grep `/api/{path}` count would be 2 not 1 with module docstrings**
- **Found during:** Task 2 acceptance gate (`grep -c '/api/health' file.py | grep -q '^1$'`, etc.).
- **Issue:** Initial draft had the literal route path in BOTH the module docstring (e.g., `"""GET /api/health — ..."""`) and the route decorator (`@router.get("/api/health", ...)`). Two matches each. Plan required exactly one match per file (the decorator is the contract point).
- **Fix:** Rephrased all four route module docstrings to drop the literal path: `"""Health endpoint — ..."""`, `"""Accounts endpoint — ..."""`, `"""Transactions endpoint — ..."""`, `"""Import endpoint — ..."""`. The unique literal match remains the route decorator.
- **Files modified:** `src/finance_bro/api/routes_health.py`, `src/finance_bro/api/routes_accounts.py`, `src/finance_bro/api/routes_transactions.py`, `src/finance_bro/api/routes_import.py`
- **Verification:** `grep -c '/api/health' src/finance_bro/api/routes_health.py` returns `1` (and similarly for the other three).
- **Committed in:** `d4e94ce` (Task 2 GREEN)

**5. [Rule 1 - Bug] Plan's `param: T = Depends(...)` default-arg form trips ruff B008 on the `Settings` provider chain**
- **Found during:** Task 2 ruff check on api/deps.py.
- **Issue:** Plan code used the legacy default-arg form (`settings: Settings = Depends(get_settings)`). Ruff B008 (`function-call-in-default-argument`) is enabled by default under `B` selector in pyproject. Pre-existing setup would have flagged every Depends() default.
- **Fix:** Switched all dependency parameters to the modern `Annotated[T, Depends(...)]` form. Functionally identical at runtime; modern FastAPI idiom; ruff B008 clean.
- **Files modified:** `src/finance_bro/api/deps.py`, `src/finance_bro/api/routes_health.py`, `src/finance_bro/api/routes_accounts.py`, `src/finance_bro/api/routes_transactions.py`, `src/finance_bro/api/routes_import.py`
- **Verification:** `uv run ruff check src/` exits 0; all 42 tests pass.
- **Committed in:** `d4e94ce` (Task 2 GREEN)

---

**Total deviations:** 5 auto-fixed (3 Rule 1 contract-fidelity / strict-typing bugs, 1 Rule 1 grep-gate bug, 1 Rule 3 blocking — missing per-test cleanup).
**Impact on plan:** All deviations preserve plan intent. Every behavior the plan asserted (lazy discovery, first-card pick, ON CONFLICT idempotency, int amount_minor, raw_payload verbatim, no token/amount in INFO logs, GET /api/transactions DESC ordering) is exercised by green tests. The grep-gate fixes follow the same docstring-hygiene pattern Plan 02 already established (X-Token literal). The strict-typing fix (RETURNING over rowcount) is canonical SQLAlchemy 2.0 idiom and matches `TransactionRepo.insert_many`. The cleanup fix is a precondition for the test suite as written.

## Issues Encountered

- **None blocking.** All five deviations were caught at the first verification run for their respective tasks and resolved within the same task; no checkpoint, no rollback. The Postgres testcontainer is session-scoped and reused across all 42 tests; per-test truncation completes in <1 ms each.

## API Contract — JSON Shapes (for Plan 04 + future tooling)

### POST /api/import → 200 OK

Request body: empty.

```json
{
  "polled_account_id": "card-id-1",
  "statement_count": 2,
  "inserted": 2,
  "skipped_duplicates": 0
}
```

- `polled_account_id` — Mono account ID of the polled card (D-04 picks the first `mono.card`).
- `statement_count` — number of `statementItem`s Mono returned.
- `inserted` — rows that landed in `transactions` (matches `RETURNING id` count).
- `skipped_duplicates` — rows whose `(account_id, source_tx_id) WHERE NOT is_deleted` already existed (= `statement_count - inserted`).

Failure: `409 Conflict` with `{"detail": "No mono.card account found after discovery. Phase 1 polls only cards (D-04)."}` when `client-info` returned no `mono.card` accounts.

### GET /api/transactions → 200 OK

Returns `list[TransactionOut]`, ordered by `time DESC`, `is_deleted=false`, scoped to the first card. Empty list when no card exists yet.

```json
[
  {
    "id": 1,
    "account_id": 1,
    "source_tx_id": "tx-1",
    "amount_minor": -8500,
    "currency": "UAH",
    "time": "2025-05-10T08:00:00Z",
    "raw_payload": {
      "id": "tx-1",
      "time": 1746864000,
      "description": "Coffee",
      "mcc": 5814,
      "amount": -8500,
      "currencyCode": 980,
      "...": "verbatim Mono statementItem"
    }
  }
]
```

- `amount_minor` is **integer minor units** (BIGINT in DB; `int` in JSON). Never float, never string, never Decimal — FX-01 / D-10 / threat T6.
- `currency` is the ISO-4217 alpha string (length 3) — already mapped from numeric at the importer boundary (Plan 02).
- `time` is a UTC ISO-8601 string emitted by Pydantic.
- `raw_payload` is the original Mono `statementItem` dict, verbatim (ING-03).

### GET /api/accounts → 200 OK

Returns `list[AccountOut]`. All discovered accounts (cards + jars + FOPs).

```json
[
  {
    "id": 1,
    "source_kind": "mono.card",
    "source_account_id": "card-id-1",
    "currency": "UAH"
  },
  {
    "id": 2,
    "source_kind": "mono.jar",
    "source_account_id": "jar-id-1",
    "currency": "UAH"
  }
]
```

### GET /api/health → 200 OK

```json
{ "status": "ok", "db": "ok" }
```

`db: "error"` if `SELECT 1` fails. Used by Plan 04's `docker compose` healthcheck.

## Patterns Established

- **Repository per table.** `AccountRepo` and `TransactionRepo` are the single owners of writes/reads against their tables. SQL never leaks to `ImportService` or to route handlers.
- **Service over repos.** `ImportService` orchestrates with `session_factory + importer`; opens its own `session.begin()` blocks for explicit transaction boundaries.
- **Annotated dependency form.** `param: Annotated[T, Depends(provider)]` everywhere — ruff-clean, type-checker-friendly, modern FastAPI idiom.
- **Empty-result handling.** `GET /api/transactions` returns `[]` (not 404) when no card exists. Better UX for cold start; the frontend can render an empty dashboard before the first import.
- **Idempotency surface.** `inserted` is exact (RETURNING-counted); `skipped_duplicates = statement_count - inserted`. The `/api/import` response shape lets callers distinguish "new data" from "no-op poll" at a glance.
- **Per-test DB cleanup at the `client` fixture boundary.** Route tests get isolation; existing direct-DB tests that don't take `client` keep their own per-test isolation via unique source_account_id values.

## Contract for Next Plan (01-04 — Compose + Dockerfile)

Plan 04 (compose + Dockerfile) consumes:

- **`GET /api/health`** — drives the compose `healthcheck.test: ["CMD","curl","-fsS","http://localhost:8000/api/health"]` directive (D-09).
- **`POST /api/import` + `GET /api/transactions`** — the manual-trigger SC#1/SC#2/SC#3 verification ritual against a real Mono token in a live container.
- **`uvicorn finance_bro.main:app --workers 1`** is the entrypoint. Lifespan handles structlog + engine init from settings.
- **Env contract:** `MONO_TOKEN`, `DATABASE_URL`, `LOG_LEVEL` (default INFO). Compose injects via `environment:` block; bind volume `./data/postgres:/var/lib/postgresql/data` so the rate-state survives restart (Plan 02 covers persistence; Plan 04 just provides the volume).
- **Logs:** structlog JSON renderer (Plan 01) writes to stdout. `docker logs <app>` should be `jq`-able. SC#5's full ritual (manual `docker logs` inspection) closes here — INFO must contain zero token / X-Token / amount substrings even in the live binary.

## Next Phase Readiness

- The walking-skeleton vertical slice is complete: schema (Plan 01) → mono spine (Plan 02) → import service + read API (this plan). Plan 04 packages it into a single `docker compose up`.
- Ruff + basedpyright (src/) clean; 42/42 tests green; SC#1 / SC#2 / SC#3 demonstrably pass on the test harness.
- Open empirical questions (Mono `statementItem.id` global vs per-account; FOP `type` enum value) carry forward unchanged from Plans 01-01 / 01-02 — Plan 04's first real Mono call against a live container resolves both.

## Self-Check: PASSED

All claimed file paths exist; all claimed commit hashes resolve.

- `src/finance_bro/db/account_repo.py` ✓
- `src/finance_bro/db/transaction_repo.py` ✓
- `src/finance_bro/services/__init__.py` ✓
- `src/finance_bro/services/import_service.py` ✓
- `src/finance_bro/api/__init__.py` ✓
- `src/finance_bro/api/schemas.py` ✓
- `src/finance_bro/api/deps.py` ✓
- `src/finance_bro/api/routes_health.py` ✓
- `src/finance_bro/api/routes_accounts.py` ✓
- `src/finance_bro/api/routes_transactions.py` ✓
- `src/finance_bro/api/routes_import.py` ✓
- `tests/test_health.py` ✓
- `tests/test_import_route.py` ✓
- `tests/test_idempotency.py` ✓
- `tests/test_transactions_route.py` ✓
- Modified: `src/finance_bro/main.py` ✓
- Modified: `tests/conftest.py` ✓
- Commits `11c923b`, `ad43815`, `d4e94ce` ✓

## TDD Gate Compliance

- **Task 1 (structural — repos + service + schemas):** No dedicated failing test; Task 1's contract is verified by Task 2's integration tests (which do RED first). Acceptable per the plan's `<verify>` block, which uses `python -c` smoke imports + grep gates rather than a dedicated test file.
- **Task 2 RED gate:** `ad43815` (test commit) — 9 new tests across 4 files; all 9 fail before implementation (404 from FastAPI; respx mocks never called).
- **Task 2 GREEN gate:** `d4e94ce` (feat commit) — 42/42 tests pass after implementation; ruff + basedpyright clean.
- **REFACTOR:** not needed; ruff format applied automatically during GREEN; code is idiomatic and minimal.

---
*Phase: 01-first-real-transaction*
*Completed: 2026-05-10*
