import pytest


def test_known_codes():
    from finance_bro.importers.currency_map import numeric_to_alpha

    assert numeric_to_alpha(980) == "UAH"
    assert numeric_to_alpha(840) == "USD"
    assert numeric_to_alpha(978) == "EUR"


def test_unknown_code_raises():
    from finance_bro.importers.currency_map import numeric_to_alpha

    with pytest.raises(ValueError) as exc:
        numeric_to_alpha(123)
    assert "123" in str(exc.value)
