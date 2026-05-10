"""Accounts endpoint — discovered Monobank accounts (D-09)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import AccountOut
from finance_bro.db.account_repo import AccountRepo

router = APIRouter()


@router.get("/api/accounts", response_model=list[AccountOut])
async def list_accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AccountOut]:
    rows = await AccountRepo(session).list_all()
    return [AccountOut.model_validate(r) for r in rows]
