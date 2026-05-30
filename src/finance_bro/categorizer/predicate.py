"""Predicate AST — the closed op vocabulary (D-05/D-06/D-08).

A rule's predicate is a FLAT AND-list of typed conditions (D-06). Each condition
carries a `Literal` `op` tag; the union is discriminated on that tag so Pydantic
decodes stored predicate JSON into the concrete model WITHOUT any `eval` — a
malformed predicate is rejected at parse time, never reaching the interpreter
(T-4-eval / Anti-pattern 8).

Field vocabularies are closed `Literal` enums encoding D-08's column-vs-raw_payload
split:
  * TextField   — substring/equality targets: description (column),
                  comment / counter_iban / counter_edrpou (raw_payload).
  * IntSetField — membership over integer columns/payload: mcc, original_mcc,
                  account_id.
  * StrSetField — membership over the currency column.

Amounts are integer minor units only — never float (CLAUDE.md §Money).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# --- Field enums: column-backed vs raw_payload-backed (D-08). ---
TextField = Literal["description", "comment", "counter_iban", "counter_edrpou"]
IntSetField = Literal["mcc", "original_mcc", "account_id"]
StrSetField = Literal["currency"]


class IContains(BaseModel):
    """Case-insensitive substring test (NOT a regex — D-05)."""

    op: Literal["icontains"] = "icontains"
    field: TextField
    value: str


class Equals(BaseModel):
    """Exact (case-sensitive) string equality."""

    op: Literal["equals"] = "equals"
    field: TextField | StrSetField
    value: str


class InInt(BaseModel):
    """Integer set-membership — handles the common OR case (D-06)."""

    op: Literal["in_int"] = "in_int"
    field: IntSetField
    values: list[int]


class InStr(BaseModel):
    """String set-membership (e.g. currency in [...])."""

    op: Literal["in_str"] = "in_str"
    field: StrSetField
    values: list[str]


class AmountSign(BaseModel):
    """Sign test on amount_minor: debit => amount_minor < 0, credit => > 0."""

    op: Literal["amount_sign"] = "amount_sign"
    sign: Literal["debit", "credit"]


class AmountRange(BaseModel):
    """Inclusive integer-minor-unit range (either bound optional)."""

    op: Literal["amount_range"] = "amount_range"
    min_minor: int | None = None
    max_minor: int | None = None


class HoldIs(BaseModel):
    """Boolean test on the hold flag."""

    op: Literal["hold_is"] = "hold_is"
    value: bool


Condition = Annotated[
    IContains | Equals | InInt | InStr | AmountSign | AmountRange | HoldIs,
    Field(discriminator="op"),
]


class RulePredicate(BaseModel):
    """Flat AND-only predicate — all conditions must match (D-06)."""

    all: list[Condition] = Field(min_length=1)
