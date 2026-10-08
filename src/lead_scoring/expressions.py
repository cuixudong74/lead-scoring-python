from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_MISSING = object()


_OPERATORS = {
    "eq", "ne", "lt", "lte", "gt", "gte",
    "in", "contains", "exists", "matches",
}


def _ensure_only_key(condition: Mapping[str, Any], key: str) -> None:
    extras = set(condition) - {key}
    if extras:
        raise ExpressionError(f"condition with {key!r} has unexpected keys: {sorted(extras)}")


def _validate_field_condition(condition: Mapping[str, Any]) -> None:
    extras = set(condition) - {"field", "op", "value"}
    if extras:
        raise ExpressionError(f"field condition has unexpected keys: {sorted(extras)}")
    op = condition.get("op", "eq")
    if op not in _OPERATORS:
        raise ExpressionError(f"unsupported operator: {op}")
    _get_path({}, condition["field"])
    if op != "exists" and "value" not in condition:
        raise ExpressionError(f"operator {op!r} requires a value")
    if op == "in" and not isinstance(condition["value"], list):
        raise ExpressionError("'in' value must be a list")
    if op == "matches":
        try:
            re.compile(str(condition["value"]))
        except re.error as exc:
            raise ExpressionError(f"invalid regular expression: {exc}") from exc


class ExpressionError(ValueError):
    """Raised when a condition is invalid or cannot be evaluated."""


def _get_path(lead_data: Mapping[str, Any], path: str) -> Any:
    if not isinstance(path, str) or not path:
        raise ExpressionError("condition field must be a non-empty string")

    current: Any = lead_data
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise ExpressionError(f"cannot index list with {part!r}") from exc
            if index < 0 or index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if actual is _MISSING:
        return False
    if op == "lt":
        return actual < expected
    if op == "lte":
        return actual <= expected
    if op == "gt":
        return actual > expected
    if op == "gte":
        return actual >= expected
    if op == "in":
        if not isinstance(expected, (list, tuple, set, frozenset)):
            raise ExpressionError("'in' value must be a list")
        return actual in expected
    if op == "contains":
        return expected in actual
    if op == "exists":
        return True
    if op == "matches":
        if not isinstance(actual, str):
            return False
        try:
            return re.fullmatch(str(expected), actual) is not None
        except re.error as exc:
            raise ExpressionError(f"invalid regular expression: {exc}") from exc
    raise ExpressionError(f"unsupported operator: {op}")


def validate_condition(condition: Mapping[str, Any]) -> None:
    """Validate condition shape without lead data."""
    if not isinstance(condition, Mapping):
        raise ExpressionError("condition must be an object")

    if "all" in condition:
        _ensure_only_key(condition, "all")
        children = condition["all"]
        if not isinstance(children, list) or not children:
            raise ExpressionError("'all' must be a non-empty list")
        for child in children:
            validate_condition(child)
        return

    if "any" in condition:
        _ensure_only_key(condition, "any")
        children = condition["any"]
        if not isinstance(children, list) or not children:
            raise ExpressionError("'any' must be a non-empty list")
        for child in children:
            validate_condition(child)
        return

    if "not" in condition:
        _ensure_only_key(condition, "not")
        validate_condition(condition["not"])
        return

    if "field" in condition:
        _validate_field_condition(condition)
        return

    if not condition:
        return
    raise ExpressionError(f"unknown condition keys: {sorted(condition)}")


def evaluate_condition(condition: Mapping[str, Any], lead_data: Mapping[str, Any]) -> bool:
    """Evaluate a small, deterministic condition DSL against lead data.

    Supported forms::

        {"all": [condition, ...]}
        {"any": [condition, ...]}
        {"not": condition}
        {"field": "company.size", "op": "gte", "value": 100}

    Missing fields never raise during comparison; malformed conditions raise
    :class:`ExpressionError` so the engine can isolate that rule.
    """
    if not isinstance(condition, Mapping):
        raise ExpressionError("condition must be an object")

    if "all" in condition:
        _ensure_only_key(condition, "all")
        children = condition["all"]
        if not isinstance(children, (list, tuple)):
            raise ExpressionError("'all' must contain a list")
        return all(evaluate_condition(child, lead_data) for child in children)

    if "any" in condition:
        _ensure_only_key(condition, "any")
        children = condition["any"]
        if not isinstance(children, (list, tuple)):
            raise ExpressionError("'any' must contain a list")
        return any(evaluate_condition(child, lead_data) for child in children)

    if "not" in condition:
        _ensure_only_key(condition, "not")
        return not evaluate_condition(condition["not"], lead_data)

    if "field" in condition:
        extras = set(condition) - {"field", "op", "value"}
        if extras:
            raise ExpressionError(f"field condition has unexpected keys: {sorted(extras)}")
        op = condition.get("op", "eq")
        if op not in _OPERATORS:
            raise ExpressionError(f"unsupported operator: {op}")
        actual = _get_path(lead_data, condition["field"])
        if op == "exists":
            return actual is not _MISSING
        expected = condition.get("value")
        try:
            return _compare(op, actual, expected)
        except TypeError as exc:
            raise ExpressionError(f"type mismatch for operator {op!r}: {exc}") from exc

    if not condition:
        return True
    raise ExpressionError(f"unknown condition keys: {sorted(condition)}")
