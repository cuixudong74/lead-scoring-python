"""Tests for rule-set parsing/validation and round-trip serialization."""
import pytest

from lead_engine.config import parse_ruleset, ruleset_to_dict
from lead_engine.exceptions import ConfigValidationError
from lead_engine.models import DegradePolicy


def valid_payload(**over):
    p = {"version": "v1", "default_queue": "gen",
         "rules": [
             {"id": "r1", "name": "R1", "condition": "score >= 80",
              "score_weight": 40, "priority": 10, "routing_queue": "hot",
              "channel": "web"},
             {"id": "r2", "condition": "country == 'CN'",
              "score_weight": 20},
         ],
         "score_bands": [{"name": "hot", "min_score": 50, "max_score": 100}]}
    p.update(over)
    return p


def test_parses_valid_ruleset():
    rs = parse_ruleset(valid_payload())
    assert rs.version == "v1"
    assert rs.default_degrade == DegradePolicy.DROP_RULE
    assert {r.id for r in rs.rules} == {"r1", "r2"}
    assert rs.score_bands[0].name == "hot"


def test_round_trip_serialization():
    rs = parse_ruleset(valid_payload(description="d"))
    data = ruleset_to_dict(rs)
    assert data["version"] == "v1" and data["description"] == "d"
    rs2 = parse_ruleset(data)
    assert rs2 == rs


def test_bad_condition_rejected_at_parse_time():
    p = valid_payload(rules=[{"id": "r", "condition": "score >> 1",
                              "score_weight": 1}])
    with pytest.raises(ConfigValidationError):
        parse_ruleset(p)


def test_duplicate_rule_ids_rejected():
    p = valid_payload(rules=[
        {"id": "r", "condition": "a == 1", "score_weight": 1},
        {"id": "r", "condition": "b == 1", "score_weight": 1}])
    with pytest.raises(ConfigValidationError, match="duplicate"):
        parse_ruleset(p)


def test_empty_rules_rejected():
    with pytest.raises(ConfigValidationError):
        parse_ruleset(valid_payload(rules=[]))


def test_overlapping_bands_rejected():
    p = valid_payload(score_bands=[
        {"name": "a", "min_score": 0, "max_score": 10},
        {"name": "b", "min_score": 5, "max_score": 20}])
    with pytest.raises(ConfigValidationError, match="overlap"):
        parse_ruleset(p)


def test_invalid_degrade_policy_rejected():
    with pytest.raises(ConfigValidationError):
        parse_ruleset(valid_payload(default_degrade="nonsense"))


@pytest.mark.parametrize("mut", [
    {"version": ""},
    {"default_queue": ""},
    {"rules": "not-a-list"},
], )
def test_invalid_structures(mut):
    with pytest.raises(ConfigValidationError):
        parse_ruleset(valid_payload(**mut))


def test_non_finite_weight_rejected():
    p = valid_payload(rules=[{"id": "r", "condition": "a == 1",
                              "score_weight": float("inf")}])
    with pytest.raises(ConfigValidationError):
        parse_ruleset(p)
