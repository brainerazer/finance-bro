"""Rule CRUD + priority reorder endpoints — CAT-01 / CAT-02.

GET/POST/PATCH/DELETE /api/rules plus PATCH /api/rules/reorder. The request DTO
carries `predicate: RulePredicate`, so Pydantic validates the discriminated-union
predicate at request parse (V5 / T-4-validate): an unknown `op` or malformed
shape → 422 before the interpreter ever runs. GET returns rules priority-ordered
(`list_active_ordered`). Repos are instantiated per-handler from the request
session — mirroring routes_transactions. No prefix, no auth (DEP-02).

NOTE: `/api/rules/reorder` is declared BEFORE `/api/rules/{rid}` so the literal
path wins over the `{rid}` path parameter (otherwise "reorder" would be parsed as
an `int` rid and 422).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import (
    RuleCreateIn,
    RuleOut,
    RuleReorderIn,
    RuleUpdateIn,
)
from finance_bro.db.rule_repo import RuleRepo

router = APIRouter()


@router.get("/api/rules", response_model=list[RuleOut])
async def list_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[RuleOut]:
    rows = await RuleRepo(session).list_active_ordered()
    return [RuleOut.model_validate(r) for r in rows]


@router.post(
    "/api/rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_rule(
    body: RuleCreateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RuleOut:
    repo = RuleRepo(session)
    created = await repo.create(
        priority=body.priority,
        category_id=body.category_id,
        # The predicate was validated by Pydantic at parse; persist the JSON form.
        predicate_json=body.predicate.model_dump(mode="json"),
        description=body.description,
    )
    await session.commit()
    return RuleOut.model_validate(created)


@router.patch("/api/rules/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_rules(
    body: RuleReorderIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await RuleRepo(session).reorder(body.ordered_ids)
    await session.commit()


@router.patch("/api/rules/{rid}", response_model=RuleOut)
async def update_rule(
    rid: int,
    body: RuleUpdateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RuleOut:
    repo = RuleRepo(session)
    updated = await repo.update(
        rid,
        priority=body.priority,
        category_id=body.category_id,
        predicate_json=(
            body.predicate.model_dump(mode="json") if body.predicate is not None else None
        ),
        description=body.description,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="rule not found")
    await session.commit()
    return RuleOut.model_validate(updated)


@router.delete("/api/rules/{rid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rid: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    repo = RuleRepo(session)
    existing = await repo.get(rid)
    if existing is None:
        raise HTTPException(status_code=404, detail="rule not found")
    await repo.delete(rid)
    await session.commit()
