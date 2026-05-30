"""Transactions endpoint — flat list, ordered by time DESC (D-07, D-10).

Phase 1 ships a single read endpoint with no pagination, no filtering. Scoped
to the first card (the only account being polled in Phase 1 — D-04). When
no card exists yet (no import has run), returns an empty list rather than
404 so the frontend can render an empty dashboard before the first import.

Phase 3 (FX-03/FX-04): every row carries a UAH rollup computed ON READ. The
LATERAL join + per-row `fx_rollup.rollup(...)` happens in
`TransactionRepo.list_for_account` (the repo is the single owner of the read
SQL, and the locked FX read tests exercise it directly), so the route simply
validates the enriched mapping rows — including the five FX fields — into
`TransactionOut`. The rollup is never denormalized onto `transactions` (FX-03).
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import TransactionOut
from finance_bro.db.account_repo import AccountRepo
from finance_bro.db.transaction_repo import TransactionRepo

router = APIRouter()


@router.get("/api/transactions", response_model=list[TransactionOut])
async def list_transactions(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TransactionOut]:
    card = await AccountRepo(session).get_first_card()
    if card is None:
        return []
    rows = await TransactionRepo(session).list_for_account(card.id)
    return [TransactionOut.model_validate(r) for r in rows]
