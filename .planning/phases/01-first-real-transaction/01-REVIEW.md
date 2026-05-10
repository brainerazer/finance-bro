---
phase: 01-first-real-transaction
reviewed: 2026-05-10T00:00:00Z
depth: standard
files_reviewed: 39
files_reviewed_list:
  - .env.example
  - Dockerfile
  - alembic/env.py
  - alembic/script.py.mako
  - alembic/versions/0001_walking_skeleton.py
  - compose.yml
  - src/finance_bro/__init__.py
  - src/finance_bro/api/__init__.py
  - src/finance_bro/api/deps.py
  - src/finance_bro/api/routes_accounts.py
  - src/finance_bro/api/routes_health.py
  - src/finance_bro/api/routes_import.py
  - src/finance_bro/api/routes_transactions.py
  - src/finance_bro/api/schemas.py
  - src/finance_bro/core/__init__.py
  - src/finance_bro/core/logging.py
  - src/finance_bro/core/settings.py
  - src/finance_bro/db/__init__.py
  - src/finance_bro/db/account_repo.py
  - src/finance_bro/db/engine.py
  - src/finance_bro/db/models.py
  - src/finance_bro/db/rate_state_repo.py
  - src/finance_bro/db/transaction_repo.py
  - src/finance_bro/importers/__init__.py
  - src/finance_bro/importers/base.py
  - src/finance_bro/importers/currency_map.py
  - src/finance_bro/importers/monobank.py
  - src/finance_bro/importers/rate_limit.py
  - src/finance_bro/main.py
  - src/finance_bro/services/__init__.py
  - src/finance_bro/services/import_service.py
  - tests/conftest.py
  - tests/test_health.py
  - tests/test_idempotency.py
  - tests/test_import_route.py
  - tests/test_log_redaction.py
  - tests/test_no_auth.py
  - tests/test_rate_limit_gate.py
  - tests/test_settings.py
findings:
  critical: 1
  warning: 9
  info: 7
  total: 17
status: issues_found
---

# Phase 1: Code Review Report

**Reviewed:** 2026-05-10T00:00:00Z
**Depth:** standard
**Files Reviewed:** 39
**Status:** issues_found

## Summary

Phase 1 implements the walking skeleton: schema migrations, Mono importer with persistent rate-limit gate, idempotent inserts, and Phase-1 API surface. The hard constraints from CLAUDE.md are honored well in code: token rides only in `X-Token` header (verified by test), amount stays as `int` end-to-end, currency is `CHAR(3)` ISO-alpha at every seam, all SQL goes through SQLAlchemy bound parameters (no injection surface), the partial unique index on `(account_id, source_tx_id) WHERE NOT is_deleted` is enforced at the DB layer, and the Postgres `SELECT ... FOR UPDATE` rate-gate pattern is correctly implemented including the sentinel-row bootstrap for first-time concurrent acquirers.

The most serious defect is a **resource leak**: the `MonobankImporter` instantiated by the FastAPI dependency `get_importer` constructs a fresh `httpx.AsyncClient` on every `/api/import` request and never closes it (`aclose()` is defined but never called). Each request leaks a connection pool — over time this exhausts file descriptors on a long-running NAS deployment. The dependency must be a `yield` dependency that calls `aclose()` in `finally`.

The remaining findings cluster around (a) test fixture hygiene — `test_log_redaction.py` mutates root-logger handlers and the `lru_cache` on `get_settings` without restoring them, allowing state to leak across tests in the same session; (b) the structlog redaction processor only inspects top-level keys, so any future code that logs `raw_payload` or any nested dict containing a `token`/`amount` key will leak — defense-in-depth is shallow by design but undocumented; (c) the deps.py docstring claims a "shared persistent RateLimitGate instance per process" but the gate is reconstructed on every request (correctness still holds because state lives in Postgres, but the docstring lies about the implementation); (d) the `Protocol` declaration of `ImporterProtocol.fetch_statement` is `def`, while the implementation is `async def` (async generator) — pyright should but may not flag the structural mismatch. Several unused dependencies (`tenacity`, `iso4217`) are listed in `pyproject.toml`.

No SQL-injection vector, no token-leakage path, no money-precision regression, no auth bypass (auth is intentionally absent — DEP-02), and no broken idempotency contract was found.

## Critical Issues

### CR-01: `MonobankImporter` httpx client leaks on every `/api/import` request

**File:** `src/finance_bro/api/deps.py:33-37`
**Issue:** `get_importer` is a plain `def` dependency that constructs a new `MonobankImporter(settings.mono_token, gate)` per request. The importer's `__init__` opens an `httpx.AsyncClient` (line 32 of `monobank.py`) which owns a connection pool, file descriptors, and background tasks. The class defines `aclose()` (line 38-39 of `monobank.py`) but no production code path ever calls it — the dependency does not `yield`, and the route handler in `routes_import.py` does nothing on exit. Each `/api/import` POST therefore allocates a connection pool that is never released. On a homelab/NAS where the process runs for weeks between restarts, this leaks fds and TCP sockets monotonically. The class docstring in `deps.py` further claims the `RateLimitGate` is "one persistent instance per process," but `get_rate_gate()` is also a non-cached factory that constructs a new gate per request — the rate-limit contract still holds because gate state lives in Postgres, not in the gate object, but the docstring is materially wrong about the design.
**Fix:**
```python
# src/finance_bro/api/deps.py
from collections.abc import AsyncIterator

async def get_importer(
    settings: Annotated[Settings, Depends(get_settings)],
    gate: Annotated[RateLimitGate, Depends(get_rate_gate)],
) -> AsyncIterator[MonobankImporter]:
    importer = MonobankImporter(settings.mono_token, gate)
    try:
        yield importer
    finally:
        await importer.aclose()
```
And update the docstring to describe the actual design ("RateLimitGate is stateless in Python; serialization is via Postgres `SELECT ... FOR UPDATE` on `mono_rate_state`").

## Warnings

### WR-01: `db.engine.get_session_factory` uses `assert` for production-path null check

**File:** `src/finance_bro/db/engine.py:34-38`
**Issue:** The function asserts `_factory is not None` after `init_engine()`. With Python's `-O` flag (or `PYTHONOPTIMIZE=1`), `assert` statements are stripped — `_factory` could then be `None` and the function would return it, causing every subsequent `factory()` call to fail with `TypeError: 'NoneType' object is not callable` and a confusing stack trace. This is a fragile contract for code in the request hot-path.
**Fix:**
```python
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _factory is None:
        init_engine()
    if _factory is None:
        raise RuntimeError("DB engine not initialized")
    return _factory
```

### WR-02: FastAPI lifespan never disposes the SQLAlchemy engine

**File:** `src/finance_bro/main.py:28-33`
**Issue:** The `lifespan` async context manager initializes the engine but yields with no cleanup. On graceful shutdown (`docker compose down`, SIGTERM), the connection pool is not closed; pending checkouts may be dropped mid-transaction and Postgres has to garbage-collect the orphan sessions. For a single-container homelab this is mostly cosmetic, but it is the source of the `ResourceWarning: unclosed event loop` and "asyncpg connection garbage collected" warnings that obscure real problems in tests/operations.
**Fix:**
```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    engine, _ = init_engine()
    try:
        yield
    finally:
        await engine.dispose()
```

### WR-03: Structlog redaction processor is non-recursive — nested `token`/`amount` keys leak

**File:** `src/finance_bro/core/logging.py:13-27`
**Issue:** `_redact` only iterates over the top-level keys of `event_dict`. If any code path ever logs a nested dict containing a sensitive key — e.g. `log.info("imported", raw_payload={"amount": 8500, "token": "..."})` — the redactor walks past it. Phase 1 routes do not log `raw_payload` today, but the importer keeps `raw_payload` on every `CanonicalTransaction`, so future debugging temptations are one keystroke away from a leak. This makes the OPS-04 defense-in-depth claim weaker than it appears, and the test `test_no_token_in_info_logs_full_cycle` only proves the current routes behave — it does not prove the redaction processor itself defends nested structures.
**Fix:** Either recurse on dict/list values:
```python
def _redact_value(v):
    if isinstance(v, dict):
        return {k: _REDACTED if re.search(r"token|amount", k, re.IGNORECASE) else _redact_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    return v
```
Or document the invariant ("never log nested dicts at INFO+") and add a lint/test that fails on any `_log.info(...)` with a `dict` argument outside an allowlist.

### WR-04: `tests/test_log_redaction.py` mutates global stdlib-logging state without restore

**File:** `tests/test_log_redaction.py:11-18`
**Issue:** `_capture()` does `root.handlers = [handler]` and `root.setLevel(logging.DEBUG)` and never restores them. After this module runs, every subsequent test in the same pytest session sees: (a) the StringIO buffer attached as the only root handler, (b) root level forced to DEBUG, (c) `httpx`/`httpcore` loggers stuck at WARNING because `configure()` was called. This can mask or trigger failures in `test_import_route.py::test_no_token_in_info_logs_full_cycle` (which uses `caplog`) and in any test that asserts on log output. It can also corrupt `test_debug_bypasses_redaction` ordering — running it before the others changes structlog's filtering bound-logger to DEBUG, causing INFO-redaction tests to behave differently.
**Fix:** Wrap in a fixture with teardown:
```python
@pytest.fixture
def capture_logs():
    saved_handlers = logging.getLogger().handlers[:]
    saved_level = logging.getLogger().level
    buf = io.StringIO()
    logging.getLogger().handlers = [logging.StreamHandler(buf)]
    logging.getLogger().setLevel(logging.DEBUG)
    yield buf
    logging.getLogger().handlers = saved_handlers
    logging.getLogger().setLevel(saved_level)
```

### WR-05: `tests/test_no_auth.py::_env` clears `get_settings` cache without restoring

**File:** `tests/test_no_auth.py:6-13`
**Issue:** The autouse fixture monkeypatches `MONO_TOKEN`/`DATABASE_URL` and calls `s.get_settings.cache_clear()` *before* the test runs, but does not clear the cache *after* the test. When monkeypatch reverts the env vars, the cache still holds the stub values from the test, so any subsequent test that calls `get_settings()` in the same session will receive the leftover stub `Settings` (`mono_token = "stub-token-..."`, `database_url = "postgresql+psycopg://x:y@localhost:5432/x"`). In Phase 1 this is masked because `engine.set_engine` is wired by the `session_factory` fixture before any other test reaches `init_engine`, but the cross-test bleed is real and will surface the moment a test uses `get_settings` directly without first calling `cache_clear`.
**Fix:**
```python
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MONO_TOKEN", "stub-token-...")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost:5432/x")
    from finance_bro.core import settings as s
    s.get_settings.cache_clear()
    yield
    s.get_settings.cache_clear()
```

### WR-06: `import_service.run_one_card` discovery race triggers redundant Mono calls

**File:** `src/finance_bro/services/import_service.py:48-62`
**Issue:** Two concurrent `/api/import` requests both observe an empty `accounts` list in step 1 (under different sessions, before either commits the upsert), then both proceed to call `self._importer.discover_accounts()`. The rate-gate prevents 429 (the second `client-info` call sleeps 65s), but the work is wasted: 2 client-info round-trips + 2 statement round-trips spread across ~3:15 to do what should be one import. Worse, on Phase-2 wiring (scheduler + manual button), this becomes a real footgun — an impatient operator clicking "Import" twice burns the next slot.
**Fix:** Guard discovery with a transactional advisory lock or with `INSERT ... ON CONFLICT DO NOTHING` on a `discovery_state` row, e.g.:
```python
async with self._session_factory() as session, session.begin():
    # Use a row-lock pattern similar to RateLimitGate to serialize discovery.
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": hash("mono.discover")})
    if not (await AccountRepo(session).list_all()):
        discovered = await self._importer.discover_accounts()
        ...
```
Or accept the redundancy and document it (the rate-gate makes it safe, only inefficient).

### WR-07: `Dockerfile` runs `alembic upgrade head` and `uvicorn` in one CMD without an init binary

**File:** `Dockerfile:26`
**Issue:** `CMD ["sh", "-c", "alembic upgrade head && uvicorn ..."]` runs uvicorn as PID 1 of a `sh -c` subshell. `sh` does not reap zombies and does not forward SIGTERM cleanly to uvicorn, so `docker compose down` may force a 10s SIGKILL instead of a graceful shutdown — connection pool not flushed, no chance for the FastAPI lifespan-exit hook (which WR-02 also concerns) to dispose cleanly. The bigger concern: if `alembic upgrade head` fails partway, `set -e` is not active, so `&&` may not fire, but the failure mode of a partial migration on subsequent restarts is unclean.
**Fix:** Use a tiny init (e.g., `tini` already in the slim image's apt repo, or `python -m` runner), and add `set -e`:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends curl tini && rm -rf /var/lib/apt/lists/* && useradd -u 1000 -m app
...
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "set -e; alembic upgrade head; exec uvicorn finance_bro.main:app --host 0.0.0.0 --port 8000 --workers 1"]
```

### WR-08: `routes_health.py` returns HTTP 200 even when the DB probe fails

**File:** `src/finance_bro/api/routes_health.py:16-24`
**Issue:** The handler catches `Exception` from `SELECT 1` and returns `{"status": "ok", "db": "error"}` with 200. The Docker compose healthcheck (`curl -fs http://localhost:8000/api/health`) only fails on non-2xx status, so a complete DB outage will not change the container's health state — operator gets no automated signal. Either the healthcheck should examine the body, or the handler should return 503 when the DB is unreachable.
**Fix:**
```python
@router.get("/api/health", response_model=HealthOut)
async def health(...) -> HealthOut:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail={"status": "degraded", "db": "error"})
    return HealthOut(status="ok", db="ok")
```

### WR-09: `ImporterProtocol.fetch_statement` declared `def`, implemented `async def` async-generator

**File:** `src/finance_bro/importers/base.py:38-43` vs `src/finance_bro/importers/monobank.py:68-87`
**Issue:** The Protocol declares `def fetch_statement(...) -> AsyncIterator[CanonicalTransaction]: ...` (synchronous function returning an async iterator), but the concrete `MonobankImporter.fetch_statement` is `async def` containing `yield` — i.e. a Python async-generator function, whose call site receives an `AsyncIterator` directly. Strict structural typing on Protocols may flag this mismatch (basedpyright in strict mode usually does), and any future second importer that copies the Protocol shape literally (`def ...` returning a coroutine of an iterator) would not match the Mono implementation. The Plan-2 contract was for an async-generator; the Protocol should match.
**Fix:** Change the Protocol declaration to match an async-generator:
```python
class ImporterProtocol(Protocol):
    source_kind: str
    async def discover_accounts(self) -> list[CanonicalAccount]: ...
    def fetch_statement(
        self,
        source_account_id: str,
        since: datetime,
        until: datetime,
    ) -> AsyncIterator[CanonicalTransaction]:
        # Implementations are async-generator functions; calling returns an AsyncIterator.
        ...
```
The current declaration is consistent with what async-generator callers see (calling the function returns an `AsyncIterator`), but document the contract explicitly so future implementers don't write `async def` returning an awaitable instead.

## Info

### IN-01: `_configured` global in `core/logging.py` is set but never read

**File:** `src/finance_bro/core/logging.py:10, 50`
**Issue:** Module-level `_configured = False` is mutated to `True` at the end of `configure()` but never consulted. Either remove the global or use it to guard re-entry: `if _configured: return`. Current docstring claims idempotency but the function happily reconfigures structlog on every call.
**Fix:** Delete the dead variable, or:
```python
def configure(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    ...
    _configured = True
```

### IN-02: `pyproject.toml` lists unused dependencies

**File:** `pyproject.toml:9-22`
**Issue:** `tenacity==9.1.4` and `iso4217==1.16.20260101` are declared but no source file imports them (`currency_map.py` is hand-rolled, no retries are wired into Mono fetches). Dead deps inflate the image, are an attack-surface for supply-chain incidents, and signal that the dependency list is aspirational rather than reflective. Either wire them in (importer fetches via `tenacity.AsyncRetrying`) or remove them until needed.
**Fix:** Run `uv remove tenacity iso4217` until they are actually used; reintroduce in the plan that wires retries / extends the currency map.

### IN-03: Naming inconsistency between canonical `occurred_at` and DB column `time`

**File:** `src/finance_bro/db/models.py:54`, `src/finance_bro/importers/base.py:27`, `src/finance_bro/db/transaction_repo.py:40`
**Issue:** `CanonicalTransaction.occurred_at` maps to `transactions.time` via a string-keyed dict in the repo. Two pitfalls: (a) `time` shadows the stdlib module and reads as a generic noun in queries (`Transaction.time.desc()`), and (b) a refactor that touches one name will not surface the other through type checking — pyright cannot prove the mapping is bijective. Phase-2 reconciliation logic will deal with multiple time concepts (`time`, `attributed_day`, `created_at`); resolving this now keeps that work tractable.
**Fix:** Rename DB column to `occurred_at` in a follow-up migration, or rename the canonical to `time` to match. Either is fine; consistency matters more than the specific choice.

### IN-04: `MonobankImporter.fetch_statement` does not validate window or paginate

**File:** `src/finance_bro/importers/monobank.py:68-87`
**Issue:** Mono caps statement windows at 31 days + 1 hour and may truncate large responses (the public API has historically capped at 500 items per call). The importer makes a single GET and yields whatever Mono returns. For Phase-1 single-card imports this is acceptable, but a card with >500 transactions in 31 days will silently drop entries. Phase-2 should add `?limit=...` cursor pagination or split the window.
**Fix:** Out of scope for Phase 1; document as a known limitation in the Phase 1 SUMMARY and create a follow-up task.

### IN-05: `routes_import.py` has no failure-path log

**File:** `src/finance_bro/api/routes_import.py:26-44`
**Issue:** The handler logs `import.start` and `import.done` on success, but on `NoCardAccountFound` (or any unhandled exception) only the start is logged. Diagnosing a failed import requires fishing in uvicorn's stderr. Add `_log.warning("import.failed", reason=str(e))` in the except block.
**Fix:**
```python
try:
    result = await svc.run_one_card()
except NoCardAccountFound as e:
    _log.warning("import.no_card", detail=str(e))
    raise HTTPException(status_code=409, detail=str(e)) from e
except Exception:
    _log.exception("import.failed")
    raise
```

### IN-06: `MonobankImporter.fetch_statement` interpolates `source_account_id` into URL path without validation

**File:** `src/finance_bro/importers/monobank.py:77`
**Issue:** `f"/personal/statement/{source_account_id}/{from_ts}/{to_ts}"` — `source_account_id` originates from Mono's `client-info` response and is stored verbatim. If Mono ever returned an ID containing `/` or `..`, the URL would point elsewhere on the Mono API (path traversal scoped to Mono's own domain, scoped further to the token's permissions). Risk is low since the source is trusted, but a one-line guard hardens the boundary.
**Fix:**
```python
import urllib.parse
safe_id = urllib.parse.quote(source_account_id, safe="")
resp = await self._client.get(f"/personal/statement/{safe_id}/{from_ts}/{to_ts}")
```

### IN-07: `tests/conftest.py` uses `os.environ.setdefault` — host-shell `MONO_TOKEN` leaks into test process

**File:** `tests/conftest.py:36`
**Issue:** `os.environ.setdefault("MONO_TOKEN", "test-token-aaaa...")` — if the developer has `MONO_TOKEN` exported in their shell (likely, since `.env` exists locally), the real token is what `os.environ["MONO_TOKEN"]` returns inside `test_no_token_in_info_logs_full_cycle`. The assertion `len(token) >= 30` will pass for a real Mono token, and the test will then assert the *real* token does not appear in logs — which is fine for the assertion, but means a real production token is sitting in the test process's environment, accessible to any test or test plugin that reads `os.environ`. Use a strict override:
**Fix:**
```python
os.environ["MONO_TOKEN"] = "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
```
(Drop the `setdefault` — tests must run with the test token, not whatever is in the shell.)

---

_Reviewed: 2026-05-10T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
