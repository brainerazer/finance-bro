"""First-match-wins engine + unconditional lock skip — CAT-02 / D-09 (pure).

Rules are evaluated in the order given (the caller pre-sorts priority ASC, id
ASC). The first fully-matching rule wins; a locked row returns the SKIP sentinel;
a row matching no rule returns None (NULL category — D-02).
"""

from finance_bro.categorizer import (
    SKIP,
    AmountSign,
    CompiledRule,
    IContains,
    InInt,
    RulePredicate,
    categorize_row,
    categorize_rows,
    make_row,
)


def _groceries_rule(priority: int, rule_id: int) -> CompiledRule:
    return CompiledRule(
        priority=priority,
        id=rule_id,
        category_id=10,
        predicate=RulePredicate(
            all=[InInt(field="mcc", values=[5411, 5499]), AmountSign(sign="debit")]
        ),
    )


def _atb_rule(priority: int, rule_id: int) -> CompiledRule:
    return CompiledRule(
        priority=priority,
        id=rule_id,
        category_id=99,
        predicate=RulePredicate(
            all=[
                InInt(field="mcc", values=[5411, 5499]),
                AmountSign(sign="debit"),
                IContains(field="description", value="ATB"),
            ]
        ),
    )


def test_first_match_wins_in_given_order():
    row = make_row(mcc=5411, amount_minor=-1500, description="ATB Market")
    # the ATB rule sits FIRST (lower priority number) — it wins over the generic
    # groceries rule even though both match.
    rules = [_atb_rule(100, 1), _groceries_rule(200, 2)]
    assert categorize_row(row, rules) == 99
    # reversed order -> generic groceries wins
    rules_rev = [_groceries_rule(100, 2), _atb_rule(200, 1)]
    assert categorize_row(row, rules_rev) == 10


def test_no_rule_matches_returns_none():
    row = make_row(mcc=4111, amount_minor=-1500, description="Bolt")
    assert categorize_row(row, [_groceries_rule(100, 1)]) is None


def test_empty_rules_returns_none():
    row = make_row(mcc=5411, amount_minor=-1500)
    assert categorize_row(row, []) is None


def test_locked_row_returns_skip_sentinel():
    row = make_row(mcc=5411, amount_minor=-1500, description="ATB", is_user_locked=True)
    # even though the rule WOULD match, a locked row is refused (D-09).
    result = categorize_row(row, [_atb_rule(100, 1)])
    assert result is SKIP


def test_categorize_rows_skips_locked_and_emits_id_category_pairs():
    rows = [
        make_row(id=1, mcc=5411, amount_minor=-1500, description="ATB"),  # -> 99
        make_row(id=2, mcc=5411, amount_minor=-200),  # -> 10 (groceries)
        make_row(id=3, mcc=4111, amount_minor=-50),  # -> None (no match)
        make_row(id=4, mcc=5411, amount_minor=-1, is_user_locked=True),  # skipped
    ]
    rules = [_atb_rule(100, 1), _groceries_rule(200, 2)]
    out = categorize_rows(rows, rules)
    # locked row (id=4) is absent from the output entirely.
    assert out == [(1, 99), (2, 10), (3, None)]
    assert all(rid != 4 for rid, _ in out)
