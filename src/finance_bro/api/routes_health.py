"""Health endpoint — compose-friendly liveness + DB probe (D-09)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import HealthOut

router = APIRouter()


@router.get("/api/health", response_model=HealthOut)
async def health(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthOut:
    db_status = "ok"
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"
    return HealthOut(status="ok", db=db_status)
