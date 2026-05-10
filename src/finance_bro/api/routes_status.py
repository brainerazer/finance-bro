"""Status endpoint — scheduler + per-account + backfill state (D-14).

Single read-only join over accounts × import_runs × scheduler_state.
Cheap to compute; no caching needed in v1 (Pitfall 5 index makes the
join O(log n)).

Pitfall 10: ALL mono.card accounts appear in the response, including
allowlist-filtered cards (eAid). The user can see why a card isn't being
polled because mono_type is surfaced verbatim.
"""

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import (
    AccountStatusOut,
    BackfillStatusOut,
    ImportStatusOut,
    SchedulerStatusOut,
)
from finance_bro.db.scheduler_state_repo import SchedulerStateRepo

router = APIRouter()
_log = structlog.get_logger()


# RESEARCH.md Code Examples §4 — verbatim CTE.
#
# `last_poll_updated` is surfaced as a constant 0 in v1 because the underlying
# DB stores inserted+updated together in import_runs.inserted (D-14). v1.5 may
# add a separate `updated_in_place` column to import_runs.
STATUS_QUERY = text(
    """
    WITH last_live AS (
        -- WR-05: restrict to terminal states so `last_polled_at` and
        -- `last_status` describe the most recent COMPLETED poll. Without
        -- this filter, a card whose only live row is pending/in_flight
        -- (e.g. right after a force-poll, before the tick fires) showed
        -- last_status='pending' / last_polled_at=null — making the
        -- semantics of "last poll" inconsistent across cards. v1.5 may add
        -- a separate `live_queued` indicator if the UI wants to surface
        -- "next poll is enqueued".
        SELECT DISTINCT ON (account_id)
               account_id,
               completed_at,
               status,
               last_error,
               inserted,
               statement_count
          FROM import_runs
         WHERE run_kind = 'live'
           AND status IN ('done', 'error')
         ORDER BY account_id, completed_at DESC NULLS LAST
    ),
    backfill_pending AS (
        SELECT account_id, count(*) AS remaining
          FROM import_runs
         WHERE run_kind = 'backfill' AND status IN ('pending','in_flight')
         GROUP BY account_id
    ),
    backfill_total AS (
        SELECT account_id, count(*) AS total
          FROM import_runs
         WHERE run_kind = 'backfill'
         GROUP BY account_id
    )
    SELECT a.id            AS account_id,
           a.source_account_id,
           a.mono_type,
           ll.completed_at  AS last_polled_at,
           ll.inserted      AS last_poll_inserted,
           0                AS last_poll_updated,
           ll.statement_count AS last_poll_statement_count,
           ll.status        AS last_status,
           ll.last_error,
           coalesce(bp.remaining, 0) AS backfill_remaining,
           coalesce(bt.total, 0)     AS backfill_total
      FROM accounts a
      LEFT JOIN last_live        ll ON ll.account_id = a.id
      LEFT JOIN backfill_pending bp ON bp.account_id = a.id
      LEFT JOIN backfill_total   bt ON bt.account_id = a.id
     WHERE a.source_kind = 'mono.card'
     ORDER BY a.id ASC
    """
)


@router.get("/api/import/status", response_model=ImportStatusOut)
async def import_status(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ImportStatusOut:
    # Scheduler state singleton.
    sched_row = await SchedulerStateRepo(session).read()
    if sched_row is None:
        # Defensive: should never happen post-migration; treat as 'running'.
        sched = SchedulerStatusOut(
            state="running", since=datetime.now(UTC), last_error=None
        )
    else:
        state, last_err, since = sched_row
        sched = SchedulerStatusOut(state=state, since=since, last_error=last_err)

    # Per-account snapshot.
    rows = (await session.execute(STATUS_QUERY)).mappings().all()
    accounts: list[AccountStatusOut] = []
    backfill_remaining_total = 0
    backfill_total_total = 0
    for r in rows:
        accounts.append(
            AccountStatusOut(
                account_id=r["account_id"],
                source_account_id=r["source_account_id"],
                mono_type=r["mono_type"],
                last_polled_at=r["last_polled_at"],
                last_poll_inserted=r["last_poll_inserted"],
                last_poll_updated=r["last_poll_updated"],
                last_poll_statement_count=r["last_poll_statement_count"],
                last_status=r["last_status"],
                last_error=r["last_error"],
                backfill_remaining=r["backfill_remaining"],
                backfill_total=r["backfill_total"],
            )
        )
        backfill_remaining_total += r["backfill_remaining"]
        backfill_total_total += r["backfill_total"]

    backfill = BackfillStatusOut(
        state="running" if backfill_remaining_total > 0 else "idle",
        runs_remaining=backfill_remaining_total,
        runs_total=backfill_total_total,
        # v1.5: estimate from rate-limit budget × remaining.
        eta_seconds=None,
    )

    _log.info(
        "import.status.read",
        scheduler_state=sched.state,
        active_accounts=len(accounts),
        backfill_remaining=backfill_remaining_total,
    )
    return ImportStatusOut(scheduler=sched, accounts=accounts, backfill=backfill)
