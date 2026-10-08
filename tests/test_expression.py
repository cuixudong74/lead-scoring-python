"""Tests for the safe expression language."""
import pytest

from lead_engine.exceptions import RuleExecutionError, RuleSyntaxError
from lead_engine.expression import parse, evaluate


def ev(expr, attrs):
    return evaluate(parse(expr, "r"), attrs, "r")


@pytest.mark.parametrize("expr,attrs", [
    ("score >= 80", {"score": 80}),
    ("score > 79 and country == 'CN'", {"score": 80, "country": "CN"}),
    ("tier in ['hot', 'warm']", {"tier": "warm"}),
    ("tier not in ['hot']", {"tier": "cold"}),
    ("tags contains 'vip'", {"tags": ["vip"]}),
    ("not unsubscribed", {}),
    ("profile.age > 18 or source == 'ad'", {"source": "ad"}),
    ("items[0] == 'a'", {"items": ["a", "b"]}),
    ("flag", {"flag": True}),
    ("delta > -5", {"delta": -1}),
    ("(score > 1 and score < 100) or country == 'CN'", {"country": "CN"}),
    ("a == 1 and b == 2", {"a": 1, "b": 2}),
    ("name == null", {"name": None}),
    ("name != null", {"name": "x"}),
])
def test_true_expressions(expr, attrs):
    assert ev(expr, attrs) is True


@pytest.mark.parametrize("expr,attrs", [
    ("score >= 80", {"score": 79}),
    ("flag", {}),
    ("country == 'CN'", {"country": "US"}),
    ("tags contains 'vip'", {"tags": ["other"]}),
])
def test_false_expressions(expr, attrs):
    assert ev(expr, attrs) is False


def test_missing_field_in_comparison_raises_typed_error():
    with pytest.raises(RuleExecutionError) as exc:
        ev("a.b > 1", {})
    assert exc.value.missing_field
    assert "a" in str(exc.value)


def test_three_valued_logic_short_circuits():
    # OR decided by present side
    assert ev("missing_x or true", {"x": 1}) is True
    assert ev("missing_x or false", {"x": 1}) is False
    # AND decided by present side
    assert ev("missing_x and false", {"x": 1}) is False
    # undecidable missing branch propagates
    with pytest.raises(RuleExecutionError):
        ev("x == 1 or (y > 2 and z == 3)", {"x": 2, "y": 3})


def test_type_mismatch_is_execution_error_not_crash():
    with pytest.raises(RuleExecutionError) as exc:
        ev("a > 1", {"a": "not-a-number"})
    assert not exc.value.missing_field
    with pytest.raises(RuleExecutionError):
        ev("x in 5", {"x": 1})


@pytest.mark.parametrize("bad", [
    "score >", "==", "score > 1 and", "][", "score >> 1", "'' ''",
    "a..b == 1",
])
def test_syntax_errors(bad):
    with pytest.raises(RuleSyntaxError):
        parse(bad, "r")


def test_empty_condition_rejected():
    with pytest.raises(RuleSyntaxError):
        parse("   ", "r")


def test_determinism_across_repeated_evaluation():
    ast = parse("score >= 80 and country in ['CN','US']", "r")
    attrs = {"score": 90, "country": "CN"}
    values = {evaluate(ast, dict(attrs), "r") for _ in range(20)}
    assert values == {True}
