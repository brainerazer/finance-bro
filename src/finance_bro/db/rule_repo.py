"""RuleRepo — priority-ordered categorization rule CRUD + deterministic order.

`list_active_ordered()` orders `priority ASC, id ASC` (the deterministic
tiebreak the engine relies on — Pitfall 6). The predicate is stored/returned as
a plain JSON dict; validation into `RulePredicate` lives at the route DTO
boundary (V5 input validation), so the repo never `eval`s or re-parses it.

`reorder(ordered_ids)` rewrites `priority` for the given sequence in ONE
transaction without tripping the `uq_rules_priority` UNIQUE constraint, using a
two-phase rewrite: first park the affected rows in a high, collision-free band
(id-derived so each is distinct), then renumber them 1..N in the requested
order. Reads/writes use ORM or parameterized `text()` — no f-string SQL.
"""

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Rule


class RuleRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        priority: int,
        category_id: int,
        predicate_json: dict[str, Any],
        description: str | None,
    ) -> Rule:
        rule = Rule(
            priority=priority,
            category_id=category_id,
            predicate=predicate_json,
            description=description,
        )
        self._s.add(rule)
        await self._s.flush()
        return rule

    async def get(self, rid: int) -> Rule | None:
        result = await self._s.execute(select(Rule).where(Rule.id == rid))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Rule]:
        result = await self._s.execute(select(Rule).order_by(Rule.id))
        return list(result.scalars().all())

    async def list_active_ordered(self) -> list[Rule]:
        """priority ASC, id ASC — the deterministic order the engine consumes
        (Pitfall 6)."""
        result = await self._s.execute(select(Rule).order_by(Rule.priority, Rule.id))
        return list(result.scalars().all())

    async def update(
        self,
        rid: int,
        priority: int | None = None,
        category_id: int | None = None,
        predicate_json: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> Rule | None:
        rule = await self.get(rid)
        if rule is None:
            return None
        if priority is not None:
            rule.priority = priority
        if category_id is not None:
            rule.category_id = category_id
        if predicate_json is not None:
            rule.predicate = predicate_json
        if description is not None:
            rule.description = description
        await self._s.flush()
        return rule

    async def delete(self, rid: int) -> None:
        await self._s.execute(text("DELETE FROM rules WHERE id = :rid"), {"rid": rid})

    async def reorder(self, ordered_ids: list[int]) -> None:
        """Rewrite priorities so the given ids become 1..N in that order.

        Two-phase to dodge the `uq_rules_priority` UNIQUE collision: phase 1
        parks each affected row at a distinct, collision-free temporary priority
        derived from its id (well above any real priority); phase 2 assigns the
        final 1-based priorities in the requested order. Both phases run inside
        the caller's transaction.
        """
        if not ordered_ids:
            return
        # Phase 1: park each row at id-derived temporary priorities. Using the id
        # (offset into a high band) guarantees uniqueness during the swap so no
        # two parked rows ever collide on the UNIQUE constraint.
        temp_offset = 1_000_000
        for rid in ordered_ids:
            await self._s.execute(
                text("UPDATE rules SET priority = :p WHERE id = :rid"),
                {"p": temp_offset + rid, "rid": rid},
            )
        # Phase 2: assign final 1-based priorities in the requested order.
        for new_priority, rid in enumerate(ordered_ids, start=1):
            await self._s.execute(
                text("UPDATE rules SET priority = :p WHERE id = :rid"),
                {"p": new_priority, "rid": rid},
            )
        await self._s.flush()
