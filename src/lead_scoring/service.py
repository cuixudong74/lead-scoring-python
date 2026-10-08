from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .auth import AuthorizationError, require_permission
from .dispatch import Dispatcher
from .engine import evaluate_ruleset
from .expressions import ExpressionError, validate_condition
from .models import (
    AuditEvent,
    DispatchRecord,
    Lead,
    Permission,
    PreviewReport,
    ReplayComparison,
    RuleSet,
    ScoreResult,
    StoredVersion,
    User,
    ValidationReport,
    VersionStatus,
)
from .repository import RuleRepository


class PreviewRejectedError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_default(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def ruleset_checksum(ruleset: RuleSet) -> str:
    payload = {
        "version": ruleset.version,
        "pass_score": ruleset.pass_score,
        "default_route": ruleset.default_route,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "name": rule.name,
                "priority": rule.priority,
                "condition": rule.condition,
                "points": rule.points,
                "weight": rule.weight,
                "route": rule.route,
                "error_policy": rule.error_policy,
            }
            for rule in sorted(ruleset.rules, key=lambda r: r.rule_id)
        ],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compare_results(old: ScoreResult, new: ScoreResult) -> ReplayComparison:
    changed = old.score != new.score or old.route != new.route
    return ReplayComparison(
        lead_id=old.lead_id,
        old_version=old.version,
        new_version=new.version,
        old_score=old.score,
        new_score=new.score,
        old_route=old.route,
        new_route=new.route,
        changed=changed,
    )


class RuleEngineService:
    def __init__(
        self,
        repository: RuleRepository | None = None,
        max_workers: int = 8,
        sender=None,
    ) -> None:
        self.repository = repository or RuleRepository()
        self._operation_lock = __import__("threading").RLock()
        self._max_workers = max_workers
        self._approved_previews: set[str] = set()
        self.dispatcher = Dispatcher(self.repository, sender)

    def _audit(self, actor: User | None, action: str, resource: str,
               allowed: bool, **details: Any) -> AuditEvent:
        event = AuditEvent(
            event_id=str(uuid4()),
            timestamp=_utc_now(),
            actor=actor.user_id if actor else "<anonymous>",
            action=action,
            resource=resource,
            allowed=allowed,
            details=details,
        )
        self.repository.add_audit(event)
        return event

    def _authorize(self, actor: User, permission: Permission,
                   action: str, resource: str) -> None:
        try:
            require_permission(actor, permission)
        except AuthorizationError:
            self._audit(actor, action, resource, False, reason="forbidden")
            raise
        self._audit(actor, action, resource, True)

    def validate(self, ruleset: RuleSet) -> ValidationReport:
        errors: list[str] = []
        if not isinstance(ruleset, RuleSet):
            return ValidationReport(False, ("ruleset must be a RuleSet instance",))
        if not ruleset.version:
            errors.append("ruleset version must be non-empty")
        if not ruleset.rules:
            errors.append("ruleset must contain at least one rule")

        try:
            numeric_pass = float(ruleset.pass_score)
            if numeric_pass != numeric_pass:  # NaN check
                errors.append("pass_score must be a finite number")
        except (TypeError, ValueError):
            errors.append("pass_score must be numeric")

        seen: set[str] = set()
        for rule in ruleset.rules:
            if not rule.rule_id:
                errors.append("rule_id must be non-empty")
            elif rule.rule_id in seen:
                errors.append(f"duplicate rule_id: {rule.rule_id}")
            seen.add(rule.rule_id)

            if not rule.name:
                errors.append(f"rule {rule.rule_id!r}: name must be non-empty")
            if not isinstance(rule.priority, int):
                errors.append(f"rule {rule.rule_id!r}: priority must be an integer")

            for field_name, value in (("points", rule.points), ("weight", rule.weight)):
                try:
                    numeric = float(value)
                    if numeric != numeric or numeric in (float("inf"), float("-inf")):
                        errors.append(
                            f"rule {rule.rule_id!r}: {field_name} must be finite"
                        )
                    elif field_name == "weight" and numeric < 0:
                        errors.append(f"rule {rule.rule_id!r}: weight must be >= 0")
                except (TypeError, ValueError):
                    errors.append(f"rule {rule.rule_id!r}: {field_name} must be numeric")

            try:
                validate_condition(rule.condition)
            except ExpressionError as exc:
                errors.append(f"rule {rule.rule_id!r}: {exc}")
            except Exception as exc:  # noqa: BLE001 - malformed configuration boundary
                errors.append(f"rule {rule.rule_id!r}: {type(exc).__name__}: {exc}")

        return ValidationReport(valid=not errors, errors=tuple(errors))

    def save_draft(self, ruleset: RuleSet, actor: User) -> StoredVersion:
        self._authorize(actor, Permission.PUBLISH, "ruleset.save_draft",
                        ruleset.version)
        report = self.validate(ruleset)
        if not report.valid:
            self._audit(actor, "ruleset.invalid", ruleset.version, True,
                        errors=report.errors)
            raise ValueError("invalid ruleset: " + "; ".join(report.errors))

        stored = StoredVersion(
            ruleset=ruleset,
            status=VersionStatus.DRAFT,
            checksum=ruleset_checksum(ruleset),
        )
        self.repository.save_version(stored)
        return stored

    def preview(
        self,
        ruleset: RuleSet,
        leads: tuple[Lead, ...],
        actor: User,
        require_no_degradation: bool = True,
    ) -> PreviewReport:
        self._authorize(actor, Permission.PREVIEW, "ruleset.preview",
                        ruleset.version)
        validation = self.validate(ruleset)
        baseline = self.repository.active_snapshot()
        new_results = self._evaluate_many(ruleset, leads)
        comparisons: tuple[ReplayComparison, ...] = ()

        if baseline is not None:
            old_results = self._evaluate_many(baseline.ruleset, leads)
            old_by_id = {result.lead_id: result for result in old_results}
            comparisons = tuple(
                compare_results(old_by_id[result.lead_id], result)
                for result in new_results
                if result.lead_id in old_by_id
            )

        reasons: list[str] = list(validation.errors)
        if not leads:
            reasons.append("preview requires at least one historical lead")
        if require_no_degradation and any(result.degraded for result in new_results):
            reasons.append("preview contains rule errors or degraded results")

        report = PreviewReport(
            checksum=ruleset_checksum(ruleset),
            new_version=ruleset.version,
            baseline_version=baseline.ruleset.version if baseline else None,
            passed=not reasons,
            reasons=tuple(reasons),
            results=tuple(new_results),
            comparisons=comparisons,
        )
        if report.passed:
            with self._operation_lock:
                self._approved_previews.add(report.checksum)
        return report

    def publish(
        self,
        version: str,
        actor: User,
        preview_report: PreviewReport | None = None,
    ) -> StoredVersion:
        self._authorize(actor, Permission.PUBLISH, "ruleset.publish", version)
        with self._operation_lock:
            stored = self.repository.get_version(version)
            checksum = stored.checksum

            if preview_report is not None:
                if preview_report.checksum != checksum:
                    self._audit(actor, "ruleset.publish_rejected", version, True,
                                reason="preview checksum mismatch")
                    raise PreviewRejectedError("preview report does not match version")
                if not preview_report.passed or checksum not in self._approved_previews:
                    self._audit(actor, "ruleset.publish_rejected", version, True,
                                reasons=preview_report.reasons)
                    raise PreviewRejectedError("preview has not passed")
            elif checksum not in self._approved_previews:
                self._audit(actor, "ruleset.publish_rejected", version, True,
                            reason="missing successful preview")
                raise PreviewRejectedError("a successful preview is required")

            if stored.status == VersionStatus.PUBLISHED and \
                    self.repository.active_version_ref() == version:
                return stored

            previous_ref = self.repository.active_version_ref()
            try:
                activated = self.repository.commit_activation(
                    version=version,
                    expected_status=VersionStatus.DRAFT,
                    previous_status=VersionStatus.ROLLED_BACK,
                )
            except ValueError:
                # Republishing a previously rolled-back version is allowed after
                # its status was changed to ROLLED_BACK by an activation switch.
                activated = self.repository.commit_activation(
                    version=version,
                    expected_status=VersionStatus.ROLLED_BACK,
                    previous_status=VersionStatus.ROLLED_BACK,
                )
            self._audit(
                actor,
                "ruleset.publish_committed",
                version,
                True,
                checksum=checksum,
                previous_version=previous_ref,
                active_version=version,
            )
            return activated

    def rollback(self, version: str, actor: User) -> StoredVersion:
        self._authorize(actor, Permission.ROLLBACK, "ruleset.rollback", version)
        with self._operation_lock:
            stored = self.repository.get_version(version)
            if stored.status not in {VersionStatus.PUBLISHED, VersionStatus.ROLLED_BACK}:
                self._audit(actor, "ruleset.rollback_rejected", version, True,
                            reason="target was never successfully published")
                raise ValueError("only a previously published version can be activated by rollback")
            previous_ref = self.repository.active_version_ref()
            activated = self.repository.commit_activation(
                version=version,
                expected_status=stored.status,
                previous_status=VersionStatus.ROLLED_BACK,
            )
            self._audit(
                actor,
                "ruleset.rollback_committed",
                version,
                True,
                checksum=activated.checksum,
                previous_version=previous_ref,
                active_version=version,
            )
            return activated

    def active_ruleset(self) -> RuleSet:
        snapshot = self.repository.active_snapshot()
        if snapshot is None:
            raise RuntimeError("no active ruleset")
        return snapshot.ruleset

    def score(self, lead: Lead, actor: User) -> ScoreResult:
        self._authorize(actor, Permission.SCORE, "ruleset.score", lead.lead_id)
        result = evaluate_ruleset(self.active_ruleset(), lead)
        self._audit(
            actor,
            "ruleset.score_result",
            lead.lead_id,
            True,
            version=result.version,
            input_hash=result.input_hash,
            input=dict(lead.data),
            score=result.score,
            route=result.route,
            hits=[asdict(hit) for hit in result.hits],
            rule_errors=[asdict(error) for error in result.rule_errors],
            degraded=result.degraded,
        )
        return result

    def _evaluate_many(self, ruleset: RuleSet,
                       leads: tuple[Lead, ...]) -> tuple[ScoreResult, ...]:
        unique: dict[str, Lead] = {}
        for lead in leads:
            # Repeated identical submissions reuse the first lead object.
            unique.setdefault(lead.lead_id, lead)

        ordered = tuple(unique.values())
        max_workers = min(self._max_workers, max(1, len(ordered)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return tuple(
                executor.map(lambda lead: evaluate_ruleset(ruleset, lead), ordered)
            )

    def score_batch(self, leads: tuple[Lead, ...], actor: User) -> tuple[ScoreResult, ...]:
        self._authorize(actor, Permission.SCORE, "ruleset.score_batch", "batch")
        results = self._evaluate_many(self.active_ruleset(), leads)
        self._audit(
            actor,
            "ruleset.score_batch_result",
            "batch",
            True,
            submitted_count=len(leads),
            evaluated_count=len(results),
            results=[asdict(result) for result in results],
        )
        return results

    def dispatch(self, lead: Lead, actor: User) -> DispatchRecord:
        result = self.score(lead, actor)
        self._authorize(actor, Permission.DISPATCH, "ruleset.dispatch", lead.lead_id)
        record = self.dispatcher.dispatch(lead, result)
        self._audit(
            actor,
            "ruleset.dispatch_result",
            lead.lead_id,
            True,
            version=result.version,
            input_hash=result.input_hash,
            route=result.route,
            idempotency_key=record.idempotency_key,
            dispatched=record.dispatched,
        )
        return record

    def replay(
        self,
        version: str,
        leads: tuple[Lead, ...],
        actor: User,
    ) -> tuple[ReplayComparison, ...]:
        self._authorize(actor, Permission.PREVIEW, "ruleset.replay", version)
        target = self.repository.get_version(version).ruleset
        baseline = self.repository.active_snapshot()
        if baseline is None:
            raise RuntimeError("no active baseline ruleset")

        new_results = self._evaluate_many(target, leads)
        old_results = self._evaluate_many(baseline.ruleset, leads)
        old_by_id = {result.lead_id: result for result in old_results}
        return tuple(
            compare_results(old_by_id[result.lead_id], result)
            for result in new_results
            if result.lead_id in old_by_id
        )

    def list_audit(self, actor: User) -> tuple[AuditEvent, ...]:
        self._authorize(actor, Permission.READ_AUDIT, "audit.read", "audit")
        return self.repository.list_audit()
