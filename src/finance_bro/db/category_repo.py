"""CategoryRepo — editable spending taxonomy CRUD + D-15 delete pre-check.

Mirrors the `TrackedFxCurrencyRepo` idiom: constructor takes the session, reads
go through the ORM `select(...).order_by(...)`, and targeted reference counts use
parameterized `text()` (NEVER an f-string SQL string — T-4-sqli / V5 input
validation). `reference_counts` backs the D-15 delete guard: a category
referenced by any rule or transaction returns nonzero counts so the route can
answer 409 with the breakdown before attempting the delete (the FK
`ON DELETE RESTRICT` from migration 0004 is the DB backstop).
"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Category


class CategoryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, name: str, color: str | None) -> Category:
        category = Category(name=name, color=color)
        self._s.add(category)
        await self._s.flush()
        return category

    async def get(self, cid: int) -> Category | None:
        result = await self._s.execute(select(Category).where(Category.id == cid))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Category]:
        result = await self._s.execute(select(Category).order_by(Category.id))
        return list(result.scalars().all())

    async def update(
        self, cid: int, name: str | None = None, color: str | None = None
    ) -> Category | None:
        category = await self.get(cid)
        if category is None:
            return None
        if name is not None:
            category.name = name
        if color is not None:
            category.color = color
        await self._s.flush()
        return category

    async def delete(self, cid: int) -> None:
        await self._s.execute(text("DELETE FROM categories WHERE id = :cid"), {"cid": cid})

    async def reference_counts(self, category_id: int) -> tuple[int, int]:
        """Return (rules_referencing, transactions_referencing).

        Two parameterized counts (never f-string SQL — D-15 / V5). A nonzero
        either count means the category is in use and must not be deleted.
        """
        rules_row = await self._s.execute(
            text("SELECT count(*) FROM rules WHERE category_id = :cid"),
            {"cid": category_id},
        )
        tx_row = await self._s.execute(
            text("SELECT count(*) FROM transactions WHERE category_id = :cid"),
            {"cid": category_id},
        )
        rules_n = rules_row.scalar_one()
        tx_n = tx_row.scalar_one()
        return (int(rules_n), int(tx_n))
