"""Domain types for the configurable lead scoring & distribution rule engine."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Rules / rule sets
# ---------------------------------------------------------------------------


class VersionState(str, Enum):
    """Lifecycle state of a rule-set version.

    DRAFT      -> editable, never used for evaluation.
    ACTIVE     -> the unique published version currently serving traffic.
    SUPERSEDED -> was active (or merely published) and is retained read-only,
                  eligible for rollback.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class DegradePolicy(str, Enum):
    """What happens when a single rule fails to evaluate."""

    DROP_RULE = "drop_rule"   # ignore the failing rule, continue scoring
    NEUTRAL = "neutral"       # treat contribution as zero, record the error
    FAIL_BATCH = "fail_batch"  # abort the whole evaluation batch


@dataclass(frozen=True)
class Rule:
    """One scoring rule.

    score_weight is added to the lead's score when the condition matches.
    A rule may also attach a routing queue used by distribution.
    """

    id: str
    name: str
    condition: str
    score_weight: float
    priority: int = 0
    routing_queue: str | None = None
    channel: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class RuleSet:
    """A complete, self-contained scoring/distribution configuration.

    A RuleSet is the atomic unit of publication: evaluation always sees one
    whole RuleSet, never a mix of fields pulled from different versions.
    """

    version: str
    rules: tuple[Rule, ...]
    score_bands: tuple["ScoreBand", ...] = ()
    default_queue: str = "default"
    default_degrade: DegradePolicy = DegradePolicy.DROP_RULE
    created_at: float = field(default_factory=time.time)
    created_by: str | None = None
    description: str = ""


@dataclass(frozen=True)
class ScoreBand:
    """Maps a score range to a human tier (hot/warm/cold ...)."""

    name: str
    min_score: float
    max_score: float


# ---------------------------------------------------------------------------
# Leads & evaluation output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lead:
    """A lead to be scored. Attributes are an arbitrary nested mapping."""

    id: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleHit:
    """An explainable contribution of one rule to one lead's result."""

    rule_id: str
    rule_name: str
    version: str
    weight: float
    priority: int
    routing_queue: str | None
    source: str  # channel attribution of the rule, for multi-channel alignment


@dataclass(frozen=True)
class RuleEvaluationError:
    """A failure isolated to a single rule."""

    rule_id: str
    rule_name: str
    version: str
    error_type: str
    message: str
    handled_by: DegradePolicy


@dataclass(frozen=True)
class ScoreResult:
    """Deterministic evaluation output for one lead under one version."""

    lead_id: str
    version: str
    score: float
    tier: str | None
    routed_queue: str
    hits: tuple[RuleHit, ...]
    errors: tuple[RuleEvaluationError, ...]
    degraded: bool


# ---------------------------------------------------------------------------
# Rehearsal / canary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadDiff:
    """Score difference for one lead between two versions."""

    lead_id: str
    old_version: str | None
    new_version: str
    old_score: float | None
    new_score: float
    score_delta: float | None
    old_queue: str | None
    new_queue: str
    queue_changed: bool


@dataclass(frozen=True)
class RehearsalReport:
    """Result of replaying historical leads against a candidate version."""

    candidate_version: str
    baseline_version: str | None
    sample_size: int
    passed: bool
    max_abs_delta: float
    queue_change_rate: float
    failure_reasons: tuple[str, ...]
    diffs: tuple[LeadDiff, ...]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """Append-only audit record."""

    event_id: str
    timestamp: float
    actor: str
    action: str
    denied: bool
    detail: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new(actor: str, action: str, detail: dict[str, Any] | None = None,
            denied: bool = False) -> "AuditEvent":
        return AuditEvent(
            event_id=uuid.uuid4().hex,
            timestamp=time.time(),
            actor=actor,
            action=action,
            denied=denied,
            detail=detail or {},
        )
