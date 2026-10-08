import pytest

from lead_scoring.engine import evaluate_ruleset
from lead_scoring.expressions import ExpressionError, evaluate_condition
from lead_scoring.models import Lead, Rule, RuleErrorPolicy, RuleSet


def test_condition_all_any_not_and_nested_fields() -> None:
    data = {"company": {"size": 250}, "source": "webinar", "tags": ["hot"]}
    assert evaluate_condition(
        {"all": [
            {"field": "company.size", "op": "gte", "value": 100},
            {"any": [
                {"field": "source", "op": "eq", "value": "webinar"},
                {"field": "source", "op": "eq", "value": "ad"},
            ]},
            {"not": {"field": "region", "op": "exists"}},
            {"field": "tags", "op": "contains", "value": "hot"},
        ]},
        data,
    )


def test_invalid_expression_raises_expression_error() -> None:
    with pytest.raises(ExpressionError):
        evaluate_condition({"field": "score", "op": "unknown", "value": 1}, {})


def test_conflicting_routes_resolve_by_priority_then_rule_id() -> None:
    ruleset = RuleSet(
        version="v1",
        rules=(
            Rule("low", "Low", 1, {}, points=10, route="sales-east"),
            Rule("equal-a", "A", 10, {}, points=5, route="sales-a"),
            Rule("equal-b", "B", 10, {}, points=5, route="sales-b"),
        ),
        default_route="nurture",
    )
    result = evaluate_ruleset(ruleset, Lead("l1", {}))
    assert result.score == 20
    assert [hit.rule_id for hit in result.hits] == ["equal-a", "equal-b", "low"]
    assert result.route == "sales-a"
    assert all(hit.source_version == "v1" for hit in result.hits)
    assert result.hits[0].contribution == 5


def test_rule_error_is_isolated_and_reported() -> None:
    ruleset = RuleSet(
        version="v1",
        rules=(
            Rule("bad", "Bad", 10, {"field": "score", "op": "gte", "value": 1}),
            Rule("good", "Good", 1, {}, points=3, weight=2),
        ),
    )
    result = evaluate_ruleset(ruleset, Lead("l1", {"score": "not-number"}))
    assert result.score == 6
    assert result.degraded is True
    assert result.rule_errors[0].rule_id == "bad"
    assert result.rule_errors[0].source_version == "v1"
    assert "Type mismatch".lower() in result.rule_errors[0].reason.lower()


def test_ignore_policy_does_not_mark_degradation() -> None:
    ruleset = RuleSet(
        version="v1",
        rules=(
            Rule("bad", "Bad", 10, {"bad": True},
                 error_policy=RuleErrorPolicy.IGNORE),
        ),
    )
    result = evaluate_ruleset(ruleset, Lead("l1", {}))
    assert result.rule_errors == ()
    assert result.degraded is False


def test_same_lead_and_version_have_deterministic_input_hash() -> None:
    rs = RuleSet("v1", (Rule("r", "R", 1, {}),))
    first = evaluate_ruleset(rs, Lead("l", {"a": 1, "b": 2}))
    second = evaluate_ruleset(rs, Lead("l", {"b": 2, "a": 1}))
    assert first.input_hash == second.input_hash


def test_pass_score_and_default_route_are_reported() -> None:
    ruleset = RuleSet(
        version="v1",
        rules=(Rule("small", "Small", 1, {}, points=10),),
        pass_score=20,
        default_route="nurture",
    )
    result = evaluate_ruleset(ruleset, Lead("l", {}))
    assert result.pass_score == 20
    assert result.qualified is False
    assert result.route == "nurture"


def test_skip_rule_records_reason_without_batch_degradation() -> None:
    ruleset = RuleSet(
        version="v1",
        rules=(
            Rule("bad", "Bad", 10, {"field": "score", "op": "gte", "value": 1},
                 error_policy=RuleErrorPolicy.SKIP_RULE),
            Rule("good", "Good", 1, {}, points=2),
        ),
    )
    result = evaluate_ruleset(ruleset, Lead("l1", {"score": "bad"}))
    assert result.degraded is False
    assert len(result.rule_errors) == 1
    assert result.rule_errors[0].rule_id == "bad"
    assert result.score == 2


def test_static_validation_rejects_invalid_regex_and_mixed_condition() -> None:
    from lead_scoring.expressions import validate_condition

    with pytest.raises(ExpressionError):
        validate_condition({"field": "name", "op": "matches", "value": "["})
    with pytest.raises(ExpressionError):
        validate_condition({"all": [], "field": "name"})
    with pytest.raises(ExpressionError):
        validate_condition({"field": "name", "op": "eq", "value": "x", "extra": 1})
