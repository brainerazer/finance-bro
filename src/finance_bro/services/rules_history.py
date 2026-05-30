"""RulesHistoryService — CAT-05 run-rules-over-history with a staleness token.

Sweeps the PURE Plan 01 engine (`engine.categorize_rows`) over ALL non-locked,
non-deleted rows in an account (D-14) and returns the full diff plus a sha256
staleness token (D-12 / D-13). `commit` recomputes the token from CURRENT state
and applies the diff ONLY when it matches the submitted one — a mismatch raises
`StaleRunError` (the route maps it to HTTP 409, "stale — re-preview"), and NO
write happens. This closes the lost-update / stale-write hazard (T-4-stale /
Pitfall 4): the diff captured at preview time is never blind-applied.

There is NO second categorization mechanism here — this service reuses the
exact `compile_rules` + `categorize_rows` the import step calls verbatim (D-11).
Locked rows are excluded twice over: `fetch_all_for_categorize` filters
`NOT is_user_locked` in SQL (D-09 / Pitfall 1) AND the engine returns SKIP, so
`apply_categories` can never write a locked row (T-4-lock / CAT-04).

The session block does only DB work; the pure engine runs OUTSIDE the session
(mirrors `import_service` Step 4b composition). `_compute` is the single source
of truth used by BOTH preview and commit, guaranteeing the commit re-runs the
SAME computation the token was derived from (D-13).
"""

import hashlib
import json
from dataclasses import dataclass
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_bro.api.schemas import CategoryChange, RunPreviewOut
from finance_bro.categorizer import categorize_rows, compile_rules
from finance_bro.categorizer.engine import CompiledRule, RuleRowLike
from finance_bro.categorizer.fields import RowView
from finance_bro.db.rule_repo import RuleRepo
from finance_bro.db.transaction_repo import TransactionRepo


class StaleRunError(RuntimeError):
    """Raised by `commit` when the recomputed token differs from the submitted
    one — the rules or the data changed since the preview was issued (D-13).
    The route maps this to HTTP 409 so the caller re-previews."""


@dataclass(frozen=True)
class _Computation:
    """The result of one stateless sweep: the token derived from CURRENT state,
    the row-level diff (CategoryChange with old→new), and the skipped-locked
    count. `_compute` returns this; preview and commit both consume it."""

    token: str
    diff: list[CategoryChange]
    skipped_locked_count: int


def _compute_token(rules: list[CompiledRule], rows: list[RowView]) -> str:
    """sha256 over (ordered rules signature, current row→category state) —
    RESEARCH §Pattern 5. `rules` are already priority-ordered; `row_state` is
    sorted for determinism. Any rule edit or any non-locked row's category
    change flips the token, which is exactly the stale-write guard (D-13)."""
    rules_sig = [(r.priority, r.category_id, r.predicate.model_dump(mode="json")) for r in rules]
    row_state = sorted((row.id, row.category_id) for row in rows)
    payload = json.dumps(
        {"rules": rules_sig, "rows": row_state},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class RulesHistoryService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _compute(self, account_id: int) -> _Computation:
        """Single source of truth for both preview and commit (D-13): load the
        ordered rules + all non-locked rows in a session, then run the PURE
        engine OUTSIDE the session and derive the diff + token from CURRENT
        state. Calling this on commit guarantees the SAME computation the token
        was issued from is re-run."""
        async with self._session_factory() as session:
            # ORM `Rule` rows satisfy RuleRowLike at runtime (Mapped[int] -> int,
            # Mapped[dict] -> dict on instance access); cast at the boundary —
            # keeps the categorizer package SQLAlchemy-free.
            raw_rules = await RuleRepo(session).list_active_ordered()
            tx_repo = TransactionRepo(session)
            rows = await tx_repo.fetch_all_for_categorize(account_id)
            skipped_locked_count = await tx_repo.count_locked(account_id)

        # Pure engine OUTSIDE the session (mirrors import_service Step 4b).
        rules = compile_rules(cast("list[RuleRowLike]", raw_rules))
        updates = categorize_rows(rows, rules)

        current_by_id = {row.id: row.category_id for row in rows}
        diff = [
            CategoryChange(
                transaction_id=row_id,
                old_category_id=current_by_id[row_id],
                new_category_id=new_cid,
            )
            for row_id, new_cid in updates
            if new_cid != current_by_id[row_id]
        ]
        token = _compute_token(rules, rows)
        return _Computation(token=token, diff=diff, skipped_locked_count=skipped_locked_count)

    async def preview(self, account_id: int) -> RunPreviewOut:
        """Re-evaluate ALL non-locked rows and return the full diff + token
        (D-12). `overwritten_count` counts only rows whose OLD category was
        non-None and changed (a rule→rule reassignment), distinct from
        first-time categorization of a previously-NULL row."""
        comp = await self._compute(account_id)
        overwritten_count = sum(1 for c in comp.diff if c.old_category_id is not None)
        return RunPreviewOut(
            changed_count=len(comp.diff),
            overwritten_count=overwritten_count,
            skipped_locked_count=comp.skipped_locked_count,
            changes=comp.diff,
            token=comp.token,
        )

    async def commit(self, account_id: int, token: str) -> dict[str, int]:
        """Recompute the token from CURRENT state and apply the diff ONLY on
        match (D-13). A mismatch raises `StaleRunError` and writes nothing
        (T-4-stale / Pitfall 4 — never blind-apply a preview-time diff). The
        write touches only the diffed non-locked rows; `apply_categories` stamps
        `category_source='rule'` (NULL allowed for an evaluated-but-unmatched
        row, D-02). Locked rows are unreachable here — excluded by the SQL read
        AND the engine SKIP (T-4-lock)."""
        comp = await self._compute(account_id)
        if comp.token != token:
            raise StaleRunError(
                "Rules or data changed since preview; re-preview before committing."
            )
        updates = [(c.transaction_id, c.new_category_id) for c in comp.diff]
        async with self._session_factory() as session, session.begin():
            await TransactionRepo(session).apply_categories(updates)
        return {"applied": len(comp.diff)}
