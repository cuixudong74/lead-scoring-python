"""Parsing & validation of versioned rule-set configurations.

Configuration is supplied as plain JSON-compatible data so it can travel over
HTTP and be stored immutably::

    {
      "version": "v2026.10.1",
      "description": "...",
      "default_queue": "general",
      "default_degrade": "drop_rule",
      "rules": [
        {"id": "hot_cn", "name": "Hot CN lead",
         "condition": "country == 'CN' and score >= 80",
         "score_weight": 30, "priority": 100,
         "routing_queue": "cn-hot", "channel": "web", "enabled": true}
      ],
      "score_bands": [
        {"name": "hot", "min_score": 80, "max_score": 1000}
      ]
    }

Every condition is parsed eagerly: a bad expression makes the whole rule-set
invalid at creation time, never at scoring time.
"""
from __future__ import annotations

import time
from typing import Any

from .exceptions import ConfigValidationError
from .expression import parse
from .models import DegradePolicy, Rule, RuleSet, ScoreBand

VALID_DEGRADES = {p.value for p in DegradePolicy}


def _require(payload: dict[str, Any], key: str, types: tuple, ctx: str) -> Any:
    if key not in payload:
        raise ConfigValidationError(f"{ctx}: missing field {key!r}")
    value = payload[key]
    if not isinstance(value, types):
        names = "/".join(t.__name__ for t in types)
        raise ConfigValidationError(
            f"{ctx}: field {key!r} must be {names}, got {type(value).__name__}")
    return value


def _parse_rule(raw: Any) -> Rule:
    if not isinstance(raw, dict):
        raise ConfigValidationError(f"rule must be an object, got {type(raw).__name__}")
    rid = _require(raw, "id", (str,), "rule")
    if not rid.strip():
        raise ConfigValidationError("rule: 'id' must be non-empty")
    name = raw.get("name", rid)
    if not isinstance(name, str):
        raise ConfigValidationError(f"rule {rid}: 'name' must be a string")
    condition = _require(raw, "condition", (str,), f"rule {rid}")
    # eager parse: rejects broken expressions up front
    try:
        parse(condition, rid)
    except ConfigValidationError:
        raise
    except Exception as exc:  # RuleSyntaxError
        raise ConfigValidationError(f"rule {rid}: invalid condition: {exc}") from exc
    weight = _require(raw, "score_weight", (int, float), f"rule {rid}")
    if isinstance(weight, bool) or not _finite(weight):
        raise ConfigValidationError(f"rule {rid}: 'score_weight' must be finite number")
    priority = raw.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ConfigValidationError(f"rule {rid}: 'priority' must be an integer")
    queue = raw.get("routing_queue")
    if queue is not None and not isinstance(queue, str):
        raise ConfigValidationError(f"rule {rid}: 'routing_queue' must be string")
    channel = raw.get("channel")
    if channel is not None and not isinstance(channel, str):
        raise ConfigValidationError(f"rule {rid}: 'channel' must be string")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigValidationError(f"rule {rid}: 'enabled' must be boolean")
    return Rule(id=rid, name=name, condition=condition,
                score_weight=float(weight), priority=priority,
                routing_queue=queue, channel=channel, enabled=enabled)


def _finite(value: Any) -> bool:
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except TypeError:
        return False


def _parse_band(raw: Any) -> ScoreBand:
    if not isinstance(raw, dict):
        raise ConfigValidationError("score band must be an object")
    name = _require(raw, "name", (str,), "band")
    lo = _require(raw, "min_score", (int, float), f"band {name}")
    hi = _require(raw, "max_score", (int, float), f"band {name}")
    if isinstance(lo, bool) or isinstance(hi, bool) or lo > hi:
        raise ConfigValidationError(
            f"band {name}: require min_score <= max_score")
    return ScoreBand(name=name, min_score=float(lo), max_score=float(hi))


def parse_ruleset(payload: dict[str, Any], *, created_by: str | None = None) -> RuleSet:
    """Validate raw mapping data and build an immutable, fully-parsed RuleSet."""
    if not isinstance(payload, dict):
        raise ConfigValidationError("rule-set must be an object")
    version = _require(payload, "version", (str,), "ruleset")
    if not version.strip():
        raise ConfigValidationError("ruleset: 'version' must be non-empty")
    raw_rules = _require(payload, "rules", (list,), "ruleset")
    if not raw_rules:
        raise ConfigValidationError("ruleset: at least one rule is required")
    rules = tuple(_parse_rule(r) for r in raw_rules)
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ConfigValidationError(f"ruleset: duplicate rule ids: {dupes}")

    bands = tuple(_parse_band(b) for b in payload.get("score_bands", []))
    # bands must not overlap to keep tier lookup deterministic
    ordered = sorted(bands, key=lambda b: b.min_score)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.min_score < prev.max_score:
            raise ConfigValidationError(
                f"ruleset: score bands overlap: {prev.name!r} vs {nxt.name!r}")

    default_queue = payload.get("default_queue", "default")
    if not isinstance(default_queue, str) or not default_queue.strip():
        raise ConfigValidationError("ruleset: 'default_queue' must be non-empty string")

    degrade = payload.get("default_degrade", DegradePolicy.DROP_RULE.value)
    if not isinstance(degrade, str) or degrade not in VALID_DEGRADES:
        raise ConfigValidationError(
            f"ruleset: 'default_degrade' must be one of {sorted(VALID_DEGRADES)}")

    description = payload.get("description", "")
    if not isinstance(description, str):
        raise ConfigValidationError("ruleset: 'description' must be string")

    from .models import RuleSet as _RS
    created_at = float(payload["created_at"]) if "created_at" in payload \
        else time.time()
    return _RS(
        version=version,
        rules=rules,
        score_bands=bands,
        default_queue=default_queue,
        default_degrade=DegradePolicy(degrade),
        created_at=created_at,
        created_by=created_by or payload.get("created_by"),
        description=description,
    )


def ruleset_to_dict(ruleset: RuleSet) -> dict[str, Any]:
    """Serialize a RuleSet to a plain JSON-compatible mapping."""
    return {
        "version": ruleset.version,
        "description": ruleset.description,
        "default_queue": ruleset.default_queue,
        "default_degrade": ruleset.default_degrade.value,
        "created_at": ruleset.created_at,
        "created_by": ruleset.created_by,
        "rules": [
            {
                "id": r.id, "name": r.name, "condition": r.condition,
                "score_weight": r.score_weight, "priority": r.priority,
                "routing_queue": r.routing_queue, "channel": r.channel,
                "enabled": r.enabled,
            }
            for r in ruleset.rules
        ],
        "score_bands": [
            {"name": b.name, "min_score": b.min_score, "max_score": b.max_score}
            for b in ruleset.score_bands
        ],
    }
