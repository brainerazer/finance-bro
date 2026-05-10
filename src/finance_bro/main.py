"""FastAPI application factory for finance-bro.

The lifespan boots structlog redaction (default-on at INFO+ — OPS-04),
initializes the async DB engine from settings, instantiates the
SchedulerRunner (with a single shared RateLimitGate + MonobankImporter), runs
the recover_in_flight sweep + reads the scheduler_state singleton, then
adds-and-starts the in-process AsyncIOScheduler at a 10s interval (D-03 +
D-04). Tests rewire the engine via `finance_bro.db.engine.set_engine` BEFORE
the lifespan runs so `init_engine()` becomes a no-op in test mode; tests
also set `APP_DISABLE_SCHEDULER=1` so the runner is still constructed (and
mounted on `app.state.runner` for routes/integration tests) but the
APScheduler job is not started — keeps Phase 1 route tests deterministic.

Lifespan ordering (RESEARCH.md Pattern 1 + Pitfall 8):
  init_engine -> SchedulerRunner() -> recover_in_flight -> read_state ->
  if state == 'running' and not disabled: scheduler.add_job + scheduler.start ->
  yield -> finally: shut down the scheduler without waiting -> runner.aclose().

The shutdown call uses wait=False (Pitfall 8) — wait=True blocks lifespan
teardown and the FastAPI lifespan never gets to close the httpx client
cleanly.

The Phase-1 routers (health, accounts, transactions, import) and the
Phase-2 status + backfill routers all mount at /api/* with no prefix nor
middleware (DEP-02 — Tailscale/LAN is the trust boundary in v1).
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from finance_bro.api import (
    routes_accounts,
    routes_backfill,
    routes_health,
    routes_import,
    routes_status,
    routes_transactions,
)
from finance_bro.core import logging as logging_cfg
from finance_bro.core.settings import get_settings
from finance_bro.db.engine import get_session_factory, init_engine
from finance_bro.importers.monobank import MonobankImporter
from finance_bro.importers.rate_limit import RateLimitGate
from finance_bro.scheduler.runner import SchedulerRunner


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    init_engine()

    # Build the runner regardless of scheduler enable so app.state.runner is
    # always available for routes (Plan 02-04 force-poll + backfill endpoints
    # depend on this).
    session_factory = get_session_factory()
    gate = RateLimitGate(session_factory)
    importer = MonobankImporter(settings.mono_token, gate)
    scheduler = AsyncIOScheduler()
    runner = SchedulerRunner(session_factory=session_factory, importer=importer)

    # CR-01: any DB-touching call (recover_in_flight, read_state) MUST run
    # inside the try/finally that owns the httpx.AsyncClient (held by
    # MonobankImporter). If it ran above, a startup DB blip would raise
    # before `try:` and `runner.aclose()` would never run — leaking the
    # client. Under filterwarnings=["error"] (pyproject.toml) the resulting
    # unclosed-client warning escalates to a hard exception that masks the
    # original cause.
    try:
        await runner.recover_in_flight()
        state, _last_err = await runner.read_state()

        disable_scheduler = os.environ.get("APP_DISABLE_SCHEDULER") == "1"
        if state == "running" and not disable_scheduler:
            scheduler.add_job(
                runner.tick,
                IntervalTrigger(seconds=10),
                id="finance-bro-tick",
                max_instances=1,  # D-03
                coalesce=True,  # D-03
                misfire_grace_time=30,
            )
            scheduler.start()

        app.state.scheduler = scheduler
        app.state.runner = runner

        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)  # Pitfall 8
        await runner.aclose()


app = FastAPI(title="finance-bro", lifespan=lifespan)
app.include_router(routes_health.router)
app.include_router(routes_accounts.router)
app.include_router(routes_transactions.router)
app.include_router(routes_import.router)
app.include_router(routes_status.router)
app.include_router(routes_backfill.router)
