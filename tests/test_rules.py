import pytest

from fraudalert.rules.engine import RuleError, RuleSpec, describe, evaluate, validate_rule

CTX = {"amount": 150.0, "currency": "USD", "merchant": "Amazon Mktpl", "card_last4": "1234",
       "is_foreign": False, "hour": 3, "weekday": 5, "anomaly_score": None}


def rule(match, conds):
    return RuleSpec(1, "r", match, validate_rule(match, conds))


def test_default_rule_any():
    r = rule("any", [{"field": "amount", "op": "gt", "value": "100"},
                     {"field": "is_foreign", "op": "eq", "value": "true"}])
    assert evaluate(r, CTX)
    assert not evaluate(r, CTX | {"amount": 50.0})
    assert evaluate(r, CTX | {"amount": 5.0, "is_foreign": True})
    assert describe(r) == "amount > 100 OR is_foreign = true"


def test_all_and_text_ops():
    r = rule("all", [{"field": "merchant", "op": "contains", "value": "amazon"},
                     {"field": "hour", "op": "lt", "value": 6}])
    assert evaluate(r, CTX)
    assert not evaluate(r, CTX | {"hour": 12})
    assert evaluate(rule("all", [{"field": "currency", "op": "not_in", "value": "usd, cad"}]), CTX | {"currency": "EUR"})
    assert evaluate(rule("all", [{"field": "merchant", "op": "regex", "value": r"^amazon"}]), CTX)


def test_missing_value_never_matches():
    r = rule("all", [{"field": "anomaly_score", "op": "lt", "value": 0.5}])
    assert not evaluate(r, CTX)
    assert evaluate(r, CTX | {"anomaly_score": 0.1})


@pytest.mark.parametrize("cond", [
    {"field": "nope", "op": "eq", "value": 1},
    {"field": "amount", "op": "contains", "value": "1"},
    {"field": "merchant", "op": "gt", "value": "1"},
    {"field": "amount", "op": "gt", "value": "abc"},
    {"field": "merchant", "op": "regex", "value": "("},
    {"field": "is_foreign", "op": "eq", "value": "maybe"},
])
def test_validation(cond):
    with pytest.raises(RuleError):
        validate_rule("all", [cond])
