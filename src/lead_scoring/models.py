from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Permission(str, Enum):
    SCORE = "score"
    DISPATCH = "dispatch"
    PUBLISH = "ruleset:publish"
    ROLLBACK = "ruleset:rollback"
    PREVIEW = "ruleset:preview"
    READ_AUDIT = "audit:read"


@dataclass(frozen=True)
class User:
    user_id: str
    permissions: frozenset[Permission] = frozenset()


@dataclass(frozen=True)
class Lead:
    lead_id: str
    data: Mapping[str, Any] = field(default_factory=dict)


class RuleErrorPolicy(str, Enum):
    IGNORE = "ignore"
    DEGRADE = "degrade"
    SKIP_RULE = "skip_rule"


class VersionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    name: str
    priority: int
    condition: Mapping[str, Any]
    points: float = 0.0
    weight: float = 1.0
    route: str | None = None
    error_policy: RuleErrorPolicy = RuleErrorPolicy.DEGRADE


@dataclass(frozen=True)
class RuleSet:
    version: str
    rules: tuple[Rule, ...]
    pass_score: float = 0.0
    default_route: str | None = None


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    source_version: str
    priority: int
    weight: float
    points: float
    contribution: float
    route: str | None


@dataclass(frozen=True)
class RuleError:
    rule_id: str
    source_version: str
    reason: str


@dataclass(frozen=True)
class ScoreResult:
    lead_id: str
    version: str
    score: float
    pass_score: float
    qualified: bool
    route: str | None
    hits: tuple[RuleHit, ...]
    rule_errors: tuple[RuleError, ...]
    degraded: bool
    input_hash: str


@dataclass(frozen=True)
class StoredVersion:
    ruleset: RuleSet
    status: VersionStatus
    checksum: str


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    timestamp: str
    actor: str
    action: str
    resource: str
    allowed: bool
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchRecord:
    idempotency_key: str
    lead_id: str
    version: str
    route: str
    dispatched: bool


@dataclass(frozen=True)
class ReplayComparison:
    lead_id: str
    old_version: str
    new_version: str
    old_score: float
    new_score: float
    old_route: str | None
    new_route: str | None
    changed: bool


@dataclass(frozen=True)
class PreviewReport:
    checksum: str
    new_version: str
    baseline_version: str | None
    passed: bool
    reasons: tuple[str, ...]
    results: tuple[ScoreResult, ...]
    comparisons: tuple[ReplayComparison, ...] = ()


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: tuple[str, ...]
