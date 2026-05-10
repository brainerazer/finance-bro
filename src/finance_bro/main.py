"""FastAPI application factory for finance-bro.

The lifespan boots structlog redaction (default-on at INFO+ — OPS-04) and
initializes the async DB engine from settings. Tests rewire the engine via
`finance_bro.db.engine.set_engine` BEFORE the lifespan runs, so
`init_engine()` becomes a no-op in test mode (the engine slot is already
populated). The four Phase-1 routers (health, accounts, transactions,
import) are mounted directly at /api/* with no prefix nor middleware
(DEP-02 — Tailscale/LAN is the trust boundary in v1).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from finance_bro.api import (
    routes_accounts,
    routes_health,
    routes_import,
    routes_transactions,
)
from finance_bro.core import logging as logging_cfg
from finance_bro.core.settings import get_settings
from finance_bro.db.engine import init_engine


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
