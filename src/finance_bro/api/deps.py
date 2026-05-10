"""FastAPI dependency providers for the Phase 1 API surface.

`get_session` yields a per-request AsyncSession bound to the process-wide
session factory (test or prod). `get_rate_gate` / `get_importer` /
`get_import_service` compose the importer + service over the same factory,
sharing one persistent RateLimitGate instance per process so the 1-req/60s
contract is honored even across concurrent /api/import calls (Pitfall 9).
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.core.settings import Settings, get_settings
from finance_bro.db.engine import get_session_factory
from finance_bro.importers.monobank import MonobankImporter
from finance_bro.importers.rate_limit import RateLimitGate
from finance_bro.scheduler.runner import SchedulerRunner
from finance_bro.services.import_service import ImportService


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_rate_gate() -> RateLimitGate:
    return RateLimitGate(get_session_factory())


def get_importer(
    settings: Annotated[Settings, Depends(get_settings)],
    gate: Annotated[RateLimitGate, Depends(get_rate_gate)],
) -> MonobankImporter:
    return MonobankImporter(settings.mono_token, gate)


def get_import_service(
    importer: Annotated[MonobankImporter, Depends(get_importer)],
) -> ImportService:
    return ImportService(get_session_factory(), importer)


def get_scheduler_runner(request: Request) -> SchedulerRunner:
    """Return the process-scoped SchedulerRunner attached at lifespan startup
    (RESEARCH.md Pattern 1 / Code Examples §2). Used by Plan 02-04's
    POST /api/import (D-16 reshape) and POST /api/backfill (D-07).

    No `get_scheduler` provider is exposed because the APScheduler instance
    itself is an implementation detail (PATTERNS.md anti-pattern callout)."""
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise RuntimeError(
            "SchedulerRunner missing from app.state — did lifespan fire?"
        )
    return runner
