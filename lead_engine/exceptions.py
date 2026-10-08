"""Exceptions raised by the lead scoring rule engine."""
from __future__ import annotations


class LeadEngineError(Exception):
    """Base error for the rule engine."""


class RuleSyntaxError(LeadEngineError):
    """A rule condition cannot be parsed."""

    def __init__(self, rule_id: str, message: str) -> None:
        super().__init__(f"rule {rule_id}: {message}")
        self.rule_id = rule_id
        self.message = message


class RuleExecutionError(LeadEngineError):
    """A parsed rule failed while evaluating a lead."""

    def __init__(self, rule_id: str, message: str, *, missing_field: bool = False) -> None:
        super().__init__(f"rule {rule_id}: {message}")
        self.rule_id = rule_id
        self.message = message
        self.missing_field = missing_field


class VersionNotFoundError(LeadEngineError):
    """Referenced rule-set version does not exist."""


class VersionStateError(LeadEngineError):
    """Operation invalid for the version's current lifecycle state."""


class PublishConflictError(LeadEngineError):
    """Concurrent publish/rollback lost the atomic swap."""


class RehearsalNotPassedError(LeadEngineError):
    """A version cannot be activated because its rehearsal failed."""


class AuthorizationError(LeadEngineError):
    """The actor is not allowed to perform the action; it is still audited."""


class ConfigValidationError(LeadEngineError):
    """A rule-set configuration is structurally invalid."""
