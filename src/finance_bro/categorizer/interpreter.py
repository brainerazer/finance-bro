"""Closed-op interpreter — `match` over the discriminated union (D-05).

`eval_condition` is the single evaluation primitive: it resolves the condition's
field off the RowView and compares. There is NO string->code path anywhere — the
`op` tag (already validated by Pydantic at decode time) selects a pre-written
branch; values are only ever COMPARED, never executed (T-4-eval / Anti-pattern 8).
A None field value yields False (no match), never an exception (Pitfall 5).

This module imports no `re`, calls no `eval`/`exec`, and is pure.
"""

from finance_bro.categorizer.fields import RowView
from finance_bro.categorizer.predicate import (
    AmountRange,
    AmountSign,
    Condition,
    Equals,
    HoldIs,
    IContains,
    InInt,
    InStr,
)


def eval_condition(cond: Condition, row: RowView) -> bool:
    match cond:
        case IContains():
            v = row.text(cond.field)
            return v is not None and cond.value.casefold() in v.casefold()
        case Equals():
            v = row.text(cond.field) if cond.field != "currency" else row.str_field(cond.field)
            return v is not None and v == cond.value
        case InInt():
            v = row.int_field(cond.field)
            return v is not None and v in set(cond.values)
        case InStr():
            v = row.str_field(cond.field)
            return v is not None and v in set(cond.values)
        case AmountSign():
            return (row.amount_minor < 0) if cond.sign == "debit" else (row.amount_minor > 0)
        case AmountRange():
            a = row.amount_minor
            return (cond.min_minor is None or a >= cond.min_minor) and (
                cond.max_minor is None or a <= cond.max_minor
            )
        case HoldIs():
            return row.hold == cond.value
    # Exhaustive over the closed union — a new op without a case is a typing
    # error, never a silent True.
    raise AssertionError(f"unhandled condition op: {cond!r}")  # pragma: no cover
