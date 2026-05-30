"""Category CRUD endpoints — CAT-03 + the D-15 delete guard.

GET/POST/PATCH/DELETE /api/categories. The DELETE handler is the D-15 guard: it
calls `CategoryRepo.reference_counts(cid)` first and, if any rule or transaction
references the category, raises 409 with the counts in `detail` rather than
attempting the delete (the FK `ON DELETE RESTRICT` from migration 0004 is the DB
backstop if a path ever forgets). Repos are instantiated per-handler from the
request session — mirroring routes_transactions. No prefix, no auth (DEP-02 —
Tailscale/LAN is the trust boundary in v1).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.api.deps import get_session
from finance_bro.api.schemas import CategoryCreateIn, CategoryOut, CategoryUpdateIn
from finance_bro.db.category_repo import CategoryRepo

router = APIRouter()


@router.get("/api/categories", response_model=list[CategoryOut])
async def list_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CategoryOut]:
    rows = await CategoryRepo(session).list_all()
    return [CategoryOut.model_validate(c) for c in rows]


@router.post(
    "/api/categories",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    body: CategoryCreateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryOut:
    repo = CategoryRepo(session)
    created = await repo.create(name=body.name, color=body.color)
    await session.commit()
    return CategoryOut.model_validate(created)


@router.patch("/api/categories/{cid}", response_model=CategoryOut)
async def update_category(
    cid: int,
    body: CategoryUpdateIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryOut:
    repo = CategoryRepo(session)
    updated = await repo.update(cid, name=body.name, color=body.color)
    if updated is None:
        raise HTTPException(status_code=404, detail="category not found")
    await session.commit()
    return CategoryOut.model_validate(updated)


@router.delete("/api/categories/{cid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    cid: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    repo = CategoryRepo(session)
    existing = await repo.get(cid)
    if existing is None:
        raise HTTPException(status_code=404, detail="category not found")
    rules_n, tx_n = await repo.reference_counts(cid)
    if rules_n or tx_n:
        # D-15: a referenced category is not deletable — surface the breakdown so
        # the caller knows what to reassign first.
        raise HTTPException(
            status_code=409,
            detail={"rules": rules_n, "transactions": tx_n},
        )
    await repo.delete(cid)
    await session.commit()
