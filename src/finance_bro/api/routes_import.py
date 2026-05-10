"""Import endpoint — synchronous, no body, returns ImportResultOut (D-08).

The handler logs only the structural outcome (polled_account_id,
statement_count, inserted, skipped_duplicates). No token, no amount values,
no Mono response body — the structlog redaction processor (configured in
core/logging.py) masks any token-shaped substring or `amount*`/`token*` key
at INFO+ as a defense-in-depth guard for OPS-04.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException

from finance_bro.api.deps import get_import_service
from finance_bro.api.schemas import ImportResultOut
from finance_bro.services.import_service import ImportService, NoCardAccountFound

router = APIRouter()
_log = structlog.get_logger()


@router.post("/api/import", response_model=ImportResultOut)
async def trigger_import(
    svc: Annotated[ImportService, Depends(get_import_service)],
) -> ImportResultOut:
    _log.info("import.start")
    try:
        result = await svc.run_one_card()
    except NoCardAccountFound as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    _log.info(
        "import.done",
        polled_account_id=result.polled_account_id,
        statement_count=result.statement_count,
        inserted=result.inserted,
        skipped_duplicates=result.skipped_duplicates,
    )
    return ImportResultOut(
        polled_account_id=result.polled_account_id,
        statement_count=result.statement_count,
        inserted=result.inserted,
        skipped_duplicates=result.skipped_duplicates,
    )
