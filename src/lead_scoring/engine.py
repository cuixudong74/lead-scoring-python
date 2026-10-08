from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .expressions import ExpressionError, evaluate_condition
from .models import Lead, Rule, RuleError, RuleErrorPolicy, RuleHit, RuleSet, ScoreResult


class RuleEvaluationError(RuntimeError):
    """Raised for engine-level failures that are not tied to one rule."""


def _canonical_lead_hash(version: str, lead: Lead) -> str:
    payload: Mapping[str, Any] = {
        "version": version,
        "lead_id": lead.lead_id,
        "data": lead.data,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rule_sort_key(rule: Rule) -> tuple[int, str]:
    # Larger priority values win; rule_id is the deterministic tie-breaker.
    return (-rule.priority, rule.rule_id)


def evaluate_ruleset(ruleset: RuleSet, lead: Lead) -> ScoreResult:
    """Evaluate one immutable ruleset snapshot against one lead.

    Rule exceptions never escape this function. Matching rules are reported in
    deterministic priority order. Every matched rule contributes
    ``points * weight`` to the score, while dispatch conflict resolution uses
    the highest-priority rule that declares a route (rule_id breaks ties).
    """
    if not isinstance(ruleset, RuleSet):
        raise RuleEvaluationError("ruleset must be a RuleSet instance")
    if not isinstance(lead, Lead):
        raise RuleEvaluationError("lead must be a Lead instance")

    hits: list[RuleHit] = []
    errors: list[RuleError] = []
    degraded = False

    for rule in sorted(ruleset.rules, key=_rule_sort_key):
        try:
            if not isinstance(rule.condition, Mapping):
                raise ExpressionError("rule condition must be an object")
            matched = evaluate_condition(rule.condition, lead.data)
        except Exception as exc:  # noqa: BLE001 - isolation boundary for one rule
            if rule.error_policy != RuleErrorPolicy.IGNORE:
                errors.append(
                    RuleError(
                        rule_id=rule.rule_id,
                        source_version=ruleset.version,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )
                if rule.error_policy == RuleErrorPolicy.DEGRADE:
                    degraded = True
            continue

        if not matched:
            continue

        contribution = round(float(rule.points) * float(rule.weight), 10)
        hits.append(
            RuleHit(
                rule_id=rule.rule_id,
                source_version=ruleset.version,
                priority=rule.priority,
                weight=float(rule.weight),
                points=float(rule.points),
                contribution=contribution,
                route=rule.route,
            )
        )

    score = round(sum(hit.contribution for hit in hits), 10)
    routed_hit = next((hit for hit in hits if hit.route is not None), None)
    route = routed_hit.route if routed_hit is not None else ruleset.default_route

    return ScoreResult(
        lead_id=lead.lead_id,
        version=ruleset.version,
        score=score,
        pass_score=float(ruleset.pass_score),
        qualified=score >= float(ruleset.pass_score),
        route=route,
        hits=tuple(hits),
        rule_errors=tuple(errors),
        degraded=degraded,
        input_hash=_canonical_lead_hash(ruleset.version, lead),
    )
