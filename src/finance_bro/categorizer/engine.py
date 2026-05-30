"""First-match-wins engine + unconditional lock skip — CAT-02 / D-09 / D-11.

`categorize_row` returns:
  * SKIP  — the row is `is_user_locked` (refused before any rule is considered;
            defense-in-depth — the caller also filters at the query level, D-09).
  * int   — the category_id of the FIRST rule whose flat AND predicate matches.
  * None  — no rule matched (NULL category — D-02).

Rules are evaluated in the order given; the caller pre-sorts `priority ASC, id
ASC` once at fetch time (Pitfall 6), not per row. `categorize_rows` is the
batch entry point reused verbatim by the import step (Plan 03) and the history
sweep (Plan 04): it skips SKIP rows from its output entirely.

This module is pure — no DB, no I/O.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from finance_bro.categorizer.fields import RowView
from finance_bro.categorizer.interpreter import eval_condition
from finance_bro.categorizer.predicate import RulePredicate


class _Skip:
    """Singleton sentinel distinct from `None` ("no match -> NULL") — D-09."""

    _instance: "_Skip | None" = None

    def __new__(cls) -> "_Skip":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "SKIP"


SKIP = _Skip()


@dataclass(frozen=True)
class CompiledRule:
    """A rule ready for evaluation: ordering keys + target + parsed predicate."""

    priority: int
    id: int
    category_id: int
    predicate: RulePredicate


class _RuleRow(Protocol):
    """Structural shape of a stored rule (the ORM `Rule` satisfies it) — kept as
    a Protocol so this module stays pure (no SQLAlchemy import)."""

    priority: int
    id: int
    category_id: int
    predicate: dict[str, Any]


def compile_rules(rules: list[_RuleRow]) -> list[CompiledRule]:
    """Adapt stored rules (priority-ordered ORM rows with a JSON-dict predicate)
    into `CompiledRule`s the engine evaluates. The predicate JSON is validated
    into the closed-op AST here — the ONE place the at-rest JSON becomes a
    `RulePredicate` for evaluation (mirrors the route DTO's boundary validation,
    V5). The caller passes rows already sorted `priority ASC, id ASC`
    (`RuleRepo.list_active_ordered`); order is preserved (Pitfall 6)."""
    return [
        CompiledRule(
            priority=r.priority,
            id=r.id,
            category_id=r.category_id,
            predicate=RulePredicate.model_validate(r.predicate),
        )
        for r in rules
    ]


def categorize_row(row: RowView, rules: list[CompiledRule]) -> int | None | _Skip:
    # D-09 invariant — the engine NEVER touches a locked row, even if a rule
    # would match. The caller must also filter; this is defense-in-depth.
    if row.is_user_locked:
        return SKIP
    for rule in rules:  # caller pre-sorts priority ASC, id ASC
        if all(eval_condition(c, row) for c in rule.predicate.all):  # AND-only (D-06)
            return rule.category_id  # first-match-wins (CAT-02)
    return None  # no rule matched -> category_id NULL (D-02)


def categorize_rows(rows: list[RowView], rules: list[CompiledRule]) -> list[tuple[int, int | None]]:
    """Batch categorize. Returns (row_id, category_id) for every NON-locked row;
    locked rows (SKIP) are omitted from the output (D-09)."""
    out: list[tuple[int, int | None]] = []
    for row in rows:
        result = categorize_row(row, rules)
        if result is SKIP:
            continue
        # result is int | None here (SKIP filtered above)
        out.append((row.id, result))  # type: ignore[arg-type]
    return out
