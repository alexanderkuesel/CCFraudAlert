"""Declarative rule evaluation.

A rule is `{"match": "all"|"any", "conditions": [{"field", "op", "value"}, ...]}`, stored as JSON in
the database so rules can be added/edited at runtime from the web UI or API. Example, "purchases
over 100 or in a foreign currency":

    {"match": "any", "conditions": [
        {"field": "amount", "op": "gt", "value": 100},
        {"field": "is_foreign", "op": "eq", "value": true}]}
"""

import re
from dataclasses import dataclass
from typing import Any

# field -> type. Values here are what `transaction_context` produces.
FIELDS: dict[str, type] = {
    "amount": float,
    "currency": str,
    "merchant": str,
    "card_last4": str,
    "is_foreign": bool,
    "hour": int,  # 0-23, local time
    "weekday": int,  # 0=Mon .. 6=Sun
    "anomaly_score": float,  # 0..1 from the anomaly detector, None until enough history
}

OPS: dict[str, str] = {
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "≠",
    "contains": "contains", "not_contains": "doesn't contain",
    "in": "in", "not_in": "not in", "regex": "matches",
}
_NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
_TEXT_OPS = {"contains", "not_contains", "regex"}


class RuleError(ValueError):
    pass


def _coerce(value: Any, typ: type) -> Any:
    if typ is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"true", "yes", "1", "y"}:
            return True
        if s in {"false", "no", "0", "n"}:
            return False
        raise RuleError(f"expected true/false, got {value!r}")
    if typ in (int, float):
        try:
            return typ(value)
        except (TypeError, ValueError) as exc:
            raise RuleError(f"expected a number, got {value!r}") from exc
    return str(value).strip()


def normalize_condition(cond: dict) -> dict:
    """Validate a condition and coerce its value; returns a clean copy. Raises RuleError."""
    field, op, value = cond.get("field"), cond.get("op"), cond.get("value")
    if field not in FIELDS:
        raise RuleError(f"unknown field {field!r}; choose from {', '.join(FIELDS)}")
    if op not in OPS:
        raise RuleError(f"unknown operator {op!r}; choose from {', '.join(OPS)}")
    typ = FIELDS[field]
    if op in _NUMERIC_OPS and typ not in (int, float):
        raise RuleError(f"operator {op!r} needs a numeric field, {field!r} is text")
    if op in _TEXT_OPS and typ is not str:
        raise RuleError(f"operator {op!r} needs a text field, {field!r} is not")
    if op in ("in", "not_in"):
        items = value if isinstance(value, list) else str(value).split(",")
        value = [_coerce(v, typ) for v in items if str(v).strip()]
        if not value:
            raise RuleError("'in' needs at least one value")
    elif op == "regex":
        value = str(value)
        try:
            re.compile(value)
        except re.error as exc:
            raise RuleError(f"bad regex: {exc}") from exc
    else:
        value = _coerce(value, typ)
    return {"field": field, "op": op, "value": value}


def validate_rule(match: str, conditions: list[dict]) -> list[dict]:
    if match not in ("all", "any"):
        raise RuleError("match must be 'all' or 'any'")
    if not conditions:
        raise RuleError("a rule needs at least one condition")
    return [normalize_condition(c) for c in conditions]


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    return a == b


def eval_condition(cond: dict, ctx: dict) -> bool:
    actual = ctx.get(cond["field"])
    if actual is None:
        return False  # e.g. no anomaly score yet: never matches, in either direction
    op, value = cond["op"], cond["value"]
    if op == "gt":
        return actual > value
    if op == "gte":
        return actual >= value
    if op == "lt":
        return actual < value
    if op == "lte":
        return actual <= value
    if op == "eq":
        return _eq(actual, value)
    if op == "ne":
        return not _eq(actual, value)
    if op == "contains":
        return value.casefold() in actual.casefold()
    if op == "not_contains":
        return value.casefold() not in actual.casefold()
    if op == "in":
        return any(_eq(actual, v) for v in value)
    if op == "not_in":
        return not any(_eq(actual, v) for v in value)
    if op == "regex":
        return re.search(value, actual, re.IGNORECASE) is not None
    raise RuleError(f"unknown operator {op!r}")


@dataclass
class RuleSpec:
    """Plain-data view of a rule, so the engine does not depend on the ORM."""

    id: int | None
    name: str
    match: str
    conditions: list[dict]
    severity: str = "medium"


def evaluate(rule: RuleSpec, ctx: dict) -> bool:
    results = (eval_condition(c, ctx) for c in rule.conditions)
    return all(results) if rule.match == "all" else any(results)


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def describe_condition(cond: dict) -> str:
    value = cond["value"]
    text = ", ".join(map(_fmt, value)) if isinstance(value, list) else _fmt(value)
    return f"{cond['field']} {OPS[cond['op']]} {text}"


def describe(rule: RuleSpec) -> str:
    joiner = " AND " if rule.match == "all" else " OR "
    return joiner.join(describe_condition(c) for c in rule.conditions)
