"""Force-poll endpoint — D-16 reshape.

POST /api/import enqueues a live-poll import_runs row for every active card
(D-01 allowlist) and returns 202 Accepted. The scheduler tick (≤10s away)
picks up the rows and routes them through the rate-limit gate (≤65s further
if the bucket is held).

Phase 1's synchronous body shape (statement_count / inserted /
skipped_duplicates) is GONE — the manual button is now an async hint, not a
synchronous fetch. The 409 path (no-card-account) is also gone: with zero
allowlisted cards the route returns 202 + {enqueued: []} (steady-state truth
is more useful than a misleading conflict).
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status

from finance_bro.api.deps import get_scheduler_runner
from finance_bro.api.schemas import ImportEnqueuedOut, ImportEnqueueRowOut
from finance_bro.scheduler.runner import SchedulerRunner

router = APIRouter()
_log = structlog.get_logger()


@router.post(
    "/api/import",
    response_model=ImportEnqueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_import(
    runner: Annotated[SchedulerRunner, Depends(get_scheduler_runner)],
) -> ImportEnqueuedOut:
    _log.info("import.start")
    pairs = await runner.enqueue_live_for_all_active_cards()
    enqueued = [
        ImportEnqueueRowOut(account_id=aid, run_id=rid) for (aid, rid) in pairs
    ]
    _log.info("import.done", enqueued_count=len(enqueued))
    return ImportEnqueuedOut(enqueued=enqueued)
