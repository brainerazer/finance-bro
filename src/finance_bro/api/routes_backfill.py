"""Backfill endpoint — debug/operator (D-07).

POST /api/backfill enqueues 12 backfill chunks per active card by default.
Returns 202 Accepted with {run_ids: [...]} immediately; the actual fetches
happen on subsequent scheduler ticks (no HTTP socket held). Bohdan can
trigger a fresh 12-month backfill from the UI without ever holding open a
long request.

`account_id` defaults to None (all active cards); `months` defaults to 12
and is bounded 1..36 by Pydantic.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status

from finance_bro.api.deps import get_scheduler_runner
from finance_bro.api.schemas import BackfillEnqueueIn, BackfillEnqueueOut
from finance_bro.scheduler.runner import SchedulerRunner

router = APIRouter()
_log = structlog.get_logger()


@router.post(
    "/api/backfill",
    response_model=BackfillEnqueueOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_backfill(
    body: BackfillEnqueueIn,
    runner: Annotated[SchedulerRunner, Depends(get_scheduler_runner)],
) -> BackfillEnqueueOut:
    _log.info(
        "backfill.enqueue.start", account_id=body.account_id, months=body.months
    )
    run_ids = await runner.enqueue_backfill(
        account_id=body.account_id, months=body.months
    )
    _log.info("backfill.enqueue.done", run_count=len(run_ids))
    return BackfillEnqueueOut(run_ids=run_ids)
