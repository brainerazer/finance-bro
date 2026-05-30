"""Pure categorization engine — CAT-01/CAT-02 / D-04..D-09 / D-11.

This package is PURE: it imports NO SQLAlchemy and no DB connection of any kind.
It is the single categorization mechanism (the rules engine — D-04) and is reused
verbatim by both the import-time step (Plan 03) and the run-over-history sweep
(Plan 04).

Public surface:
  * predicate — the closed-op Pydantic discriminated-union AST (D-05/D-06).
  * fields    — the RowView field resolver (column vs raw_payload — D-08).
  * interpreter — eval_condition, a `match`-based op evaluator (NO eval/regex).
  * engine    — categorize_row / categorize_rows (first-match-wins, lock-skip).
"""

from finance_bro.categorizer.engine import (
    SKIP,
    CompiledRule,
    categorize_row,
    categorize_rows,
)
from finance_bro.categorizer.fields import RowView, make_row
from finance_bro.categorizer.interpreter import eval_condition
from finance_bro.categorizer.predicate import (
    AmountRange,
    AmountSign,
    Condition,
    Equals,
    HoldIs,
    IContains,
    InInt,
    InStr,
    RulePredicate,
)

__all__ = [
    "SKIP",
    "AmountRange",
    "AmountSign",
    "CompiledRule",
    "Condition",
    "Equals",
    "HoldIs",
    "IContains",
    "InInt",
    "InStr",
    "RowView",
    "RulePredicate",
    "categorize_row",
    "categorize_rows",
    "eval_condition",
    "make_row",
]
