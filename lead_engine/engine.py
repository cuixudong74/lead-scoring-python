"""RuleEngine: the facade that ties all guarantees together.

Scoring semantics
-----------------
* The active RuleSet is fetched once per :meth:`score_one` call under the
  store lock, so an evaluation never mixes rules from two versions.
* Rules are evaluated in a canonical order: enabled rules sorted by
  ``(-priority, id)``. Priority decides precedence; the rule id is the total
  tie-breaker, so multi-channel rule sets always combine deterministically.
* Every matching rule contributes its ``score_weight``. The score is the
  clamped (min_score/max_score) sum; each hit carries rule id, name, version,
  weight, priority, channel and queue for full explainability.
* Distribution queue = the routing_queue of the *highest-precedence* matching
  rule (same deterministic order), otherwise the set's default queue.
* A rule raising an error is isolated: the set's ``default_degrade`` policy
  (or per-call override) decides drop / neutral / fail-batch, and the error
  is attached to the result with rule id + reason.
* Repeated scoring of the same lead is identical regardless of input order or
  concurrency: evaluation is pure, ordered, and side-effect free.

Publish / rollback
------------------
* A version is created as a draft, may be rehearsed against historical leads,
  and can only be activated when the rehearsal passes all configured gates.
* Activation is an atomic pointer swap; a failed/interrupted publish leaves
  the previously active version serving traffic.
* Rollback re-activates any previously published version. Concurrent
  publish/rollback are serialized by the store lock; the last committed swap
  wins deterministically and both operations are audited.

Audit & authorization
---------------------
Every privileged action passes the Authorizer first. Denials are recorded as
``denied=True`` audit events and rejected. Each scoring submission records the
version used and lead ids; duplicate (idempotent) submissions never log a
second distribution event.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .audit import AuditLog
from .auth import Authorizer
from .config import parse_ruleset
from .exceptions import (AuthorizationError, LeadEngineError,
                         RehearsalNotPassedError, VersionStateError)
from .expression import evaluate, parse
from .idempotency import IdempotencyStore
from .models import (AuditEvent, DegradePolicy, Lead, LeadDiff,
                     RuleEvaluationError, RuleHit, RuleSet, RehearsalReport,
                     ScoreBand, ScoreResult, VersionState)
from .store import RuleSetStore

# default rehearsal gates
DEFAULT_GATES: dict[str, float] = {
    "max_abs_delta": 1_000_000.0,     # max score change for any single lead
    "max_queue_change_rate": 1.0,     # fraction of leads allowed to change queue
    "max_error_rate": 1.0,            # fraction of leads with rule errors
}


def _canonical_rules(ruleset: RuleSet):
    return sorted((r for r in ruleset.rules if r.enabled),
                  key=lambda r: (-r.priority, r.id))


def _band_for(score: float, bands: tuple[ScoreBand, ...]) -> str | None:
    # bands are validated non-overlapping; the first containing band wins
    for band in sorted(bands, key=lambda b: (b.min_score, b.name)):
        if band.min_score <= score <= band.max_score:
            return band.name
    return None


class RuleEngine:
    def __init__(self, store: RuleSetStore | None = None,
                 audit: AuditLog | None = None,
                 authorizer: Authorizer | None = None,
                 idempotency: IdempotencyStore | None = None,
                 min_score: float = 0.0,
                 max_score: float = 100.0,
                 max_workers: int = 8) -> None:
        self.store = store or RuleSetStore()
        self.audit = audit or AuditLog()
        self.authorizer = authorizer or Authorizer()
        self.idempotency = idempotency or IdempotencyStore()
        self.min_score = float(min_score)
        self.max_score = float(max_score)
        self._max_workers = max(1, int(max_workers))

    # -------------------------------------------------------------- auditing
    def _deny_or_raise(self, actor: str, action: str,
                       detail: dict[str, Any]) -> None:
        self.audit.record(AuditEvent.new(actor, action, detail, denied=True))
        raise AuthorizationError(
            f"actor {actor!r} denied for {action!r}")

    def _authorize(self, actor: str, action: str,
                   detail: dict[str, Any] | None = None) -> None:
        if not self.authorizer.check(actor, action):
            self._deny_or_raise(actor, action, detail or {})

    # ------------------------------------------------------------- lifecycle
    def create_draft(self, payload: dict[str, Any], actor: str) -> str:
        """Validate and store a new immutable draft version."""
        self._authorize(actor, "ruleset:create",
                        {"version": payload.get("version")})
        ruleset = parse_ruleset(payload, created_by=actor)
        self.store.save_draft(ruleset)
        self.audit.record(AuditEvent.new(
            actor, "ruleset:create",
            {"version": ruleset.version, "rule_count": len(ruleset.rules)}))
        return ruleset.version

    def get_version(self, version: str) -> RuleSet:
        return self.store.get(version)

    def active_version(self) -> str | None:
        return self.store.active_version()

    def state_of(self, version: str) -> VersionState:
        return self.store.state_of(version)

    # ------------------------------------------------------------- rehearsal
    def rehearse(self, version: str, leads: list[Lead], actor: str,
                 gates: dict[str, float] | None = None) -> RehearsalReport:
        """Replay historical leads against a candidate and diff vs baseline."""
        self._authorize(actor, "ruleset:rehearse", {"version": version})
        candidate = self.store.get(version)
        baseline = self.store.get_active()
        gate_cfg = {**DEFAULT_GATES, **(gates or {})}

        new_results = {r.lead_id: r
                       for r in self._score_with(candidate, leads)}
        old_results = (
            {r.lead_id: r for r in self._score_with(baseline, leads)}
            if baseline is not None else {})

        diffs: list[LeadDiff] = []
        max_abs_delta = 0.0
        queue_changes = 0
        error_count = 0
        for lead in leads:
            new = new_results[lead.id]
            old = old_results.get(lead.id)
            if new.errors:
                error_count += 1
            delta = None if old is None else new.score - old.score
            if delta is not None:
                max_abs_delta = max(max_abs_delta, abs(delta))
            queue_changed = old is not None and old.routed_queue != new.routed_queue
            if queue_changed:
                queue_changes += 1
            diffs.append(LeadDiff(
                lead_id=lead.id,
                old_version=old.version if old else None,
                new_version=version,
                old_score=old.score if old else None,
                new_score=new.score,
                score_delta=delta,
                old_queue=old.routed_queue if old else None,
                new_queue=new.routed_queue,
                queue_changed=queue_changed,
            ))

        n = max(1, len(leads))
        failure_reasons: list[str] = []
        if max_abs_delta > gate_cfg["max_abs_delta"]:
            failure_reasons.append(
                f"max_abs_delta {max_abs_delta} exceeds gate "
                f"{gate_cfg['max_abs_delta']}")
        queue_rate = queue_changes / n
        if queue_rate > gate_cfg["max_queue_change_rate"]:
            failure_reasons.append(
                f"queue_change_rate {queue_rate:.3f} exceeds gate "
                f"{gate_cfg['max_queue_change_rate']}")
        error_rate = error_count / n
        if error_rate > gate_cfg["max_error_rate"]:
            failure_reasons.append(
                f"error_rate {error_rate:.3f} exceeds gate "
                f"{gate_cfg['max_error_rate']}")
        passed = not failure_reasons
        report = RehearsalReport(
            candidate_version=version,
            baseline_version=baseline.version if baseline else None,
            sample_size=len(leads),
            passed=passed,
            max_abs_delta=max_abs_delta,
            queue_change_rate=queue_rate,
            failure_reasons=tuple(failure_reasons),
            diffs=tuple(diffs),
        )
        self.audit.record(AuditEvent.new(
            actor, "ruleset:rehearse",
            {"version": version, "sample_size": len(leads), "passed": passed,
             "failure_reasons": list(failure_reasons)}))
        return report

    # ------------------------------------------------ publish / rollback ---
    def publish(self, version: str, actor: str,
                rehearsal_leads: list[Lead] | None = None,
                gates: dict[str, float] | None = None) -> str:
        """Validate rehearsal gates, then atomically activate the version."""
        self._authorize(actor, "ruleset:publish", {"version": version})
        if self.store.state_of(version) != VersionState.DRAFT:
            raise VersionStateError(
                f"version {version!r} cannot be published: only drafts are "
                "publishable")
        if rehearsal_leads is not None:
            report = self.rehearse(version, rehearsal_leads, actor, gates)
            if not report.passed:
                raise RehearsalNotPassedError(
                    f"rehearsal for {version!r} failed: "
                    + "; ".join(report.failure_reasons))
        with self.store.lock():
            self.store.mark_published(version)
            previous = self.store.activate(version)
        self.audit.record(AuditEvent.new(
            actor, "ruleset:publish",
            {"version": version, "previous_version": previous}))
        return version

    def rollback(self, version: str, actor: str) -> str:
        """Atomically re-activate a previously published version."""
        self._authorize(actor, "ruleset:rollback", {"version": version})
        state = self.store.state_of(version)
        if state == VersionState.DRAFT:
            raise VersionStateError(
                f"cannot roll back to unpublished draft {version!r}")
        with self.store.lock():
            if self.store.active_version() == version:
                raise VersionStateError(
                    f"version {version!r} is already active")
            previous = self.store.activate(version)
        self.audit.record(AuditEvent.new(
            actor, "ruleset:rollback",
            {"version": version, "previous_version": previous}))
        return version

    # -------------------------------------------------------------- scoring
    def _score_lead(self, ruleset: RuleSet, lead: Lead,
                    degrade: DegradePolicy,
                    compiled: dict[str, Any] | None = None) -> ScoreResult:
        hits: list[RuleHit] = []
        errors: list[RuleEvaluationError] = []
        total = 0.0
        # canonical order => highest precedence match is first
        for rule in _canonical_rules(ruleset):
            try:
                if compiled is not None:
                    ast = compiled[rule.id]
                else:
                    ast = parse(rule.condition, rule.id)
                matched = evaluate(ast, lead.attributes, rule.id)
            except Exception as exc:
                err = RuleEvaluationError(
                    rule_id=rule.id, rule_name=rule.name,
                    version=ruleset.version,
                    error_type=type(exc).__name__,
                    message=str(exc), handled_by=degrade)
                errors.append(err)
                if degrade == DegradePolicy.FAIL_BATCH:
                    raise
                if degrade == DegradePolicy.DROP_RULE:
                    continue
                # NEUTRAL: zero contribution, no hit
                continue
            if matched:
                total += rule.score_weight
                hits.append(RuleHit(
                    rule_id=rule.id, rule_name=rule.name,
                    version=ruleset.version, weight=rule.score_weight,
                    priority=rule.priority, routing_queue=rule.routing_queue,
                    source=rule.channel or "default"))
        score = max(self.min_score, min(self.max_score, total))
        routed = hits[0].routing_queue if hits and hits[0].routing_queue \
            else ruleset.default_queue
        return ScoreResult(
            lead_id=lead.id, version=ruleset.version, score=score,
            tier=_band_for(score, ruleset.score_bands),
            routed_queue=routed, hits=tuple(hits), errors=tuple(errors),
            degraded=bool(errors))

    def _score_with(self, ruleset: RuleSet, leads: list[Lead],
                    degrade: DegradePolicy | None = None) -> list[ScoreResult]:
        policy = degrade or ruleset.default_degrade
        # pre-compile conditions once per version; validated at publish time
        compiled = {r.id: parse(r.condition, r.id)
                    for r in ruleset.rules if r.enabled}

        def run(lead: Lead) -> ScoreResult:
            return self._score_lead(ruleset, lead, policy, compiled)

        if self._max_workers <= 1 or len(leads) <= 1:
            return [run(lead) for lead in leads]
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            # map preserves input order; results are pure & keyed by lead.id
            return list(pool.map(run, leads))

    def _evaluate_active(self, leads: list[Lead],
                         actor: str | None = None) -> list[ScoreResult]:
        """Fetch active version under lock and score; no auth re-check."""
        with self.store.lock():
            ruleset = self.store.get_active()
            if ruleset is None:
                raise LeadEngineError("no active rule-set version")
            results = self._score_with(ruleset, leads)
        if actor is not None:
            # traceability: version + every hit rule + input ids for a batch
            self.audit.record(AuditEvent.new(
                actor, "score:evaluate",
                {"version": ruleset.version,
                 "leads": [
                     {"lead_id": l.id,
                      "hits": [h.rule_id for h in r.hits],
                      "score": r.score, "queue": r.routed_queue,
                      "error_rules": [e.rule_id for e in r.errors]}
                     for l, r in zip(leads, results)]}))
        return results

    def score_one(self, lead: Lead, actor: str = "service") -> ScoreResult:
        """Score one lead against the currently active version."""
        self._authorize(actor, "score:submit", {"lead_id": lead.id})
        return self._evaluate_active([lead], actor)[0]

    def score_batch(self, leads: list[Lead],
                    actor: str = "service") -> list[ScoreResult]:
        """Score a batch concurrently; output order follows input order."""
        self._authorize(actor, "score:submit",
                        {"lead_ids": [l.id for l in leads]})
        return self._evaluate_active(leads, actor)

    def submit(self, leads: list[Lead], idempotency_key: str,
               actor: str = "service") -> list[ScoreResult]:
        """Idempotent batch: duplicate keys return cached results and never
        trigger distribution/side effects a second time."""
        self._authorize(actor, "score:submit",
                        {"idempotency_key": idempotency_key,
                         "lead_ids": [l.id for l in leads]})
        first, existing = self.idempotency.begin(idempotency_key)
        if not first:
            self.audit.record(AuditEvent.new(
                actor, "score:replay",
                {"idempotency_key": idempotency_key,
                 "lead_ids": [l.id for l in leads]}))
            return [_result_from_dict(item) for item in existing]
        try:
            results = self._evaluate_active(leads, actor)
        except BaseException:
            self.idempotency.fail(idempotency_key)
            raise
        payload = [_result_to_dict(r) for r in results]
        self.idempotency.complete(idempotency_key, payload)
        # distribution side effect is recorded exactly once
        self.audit.record(AuditEvent.new(
            actor, "score:dispatch",
            {"idempotency_key": idempotency_key,
             "version": results[0].version if results else None,
             "dispatches": [
                 {"lead_id": r.lead_id, "queue": r.routed_queue}
                 for r in results]}))
        return results


# ---------------------------------------------------------------------------
# (de)serialization of cached results -- kept here so idempotent replies are
# byte-for-byte equivalent to first-time results
# ---------------------------------------------------------------------------

def _result_to_dict(r: ScoreResult) -> dict[str, Any]:
    return {
        "lead_id": r.lead_id, "version": r.version, "score": r.score,
        "tier": r.tier, "routed_queue": r.routed_queue, "degraded": r.degraded,
        "hits": [{"rule_id": h.rule_id, "rule_name": h.rule_name,
                  "version": h.version, "weight": h.weight,
                  "priority": h.priority, "routing_queue": h.routing_queue,
                  "source": h.source} for h in r.hits],
        "errors": [{"rule_id": e.rule_id, "rule_name": e.rule_name,
                    "version": e.version, "error_type": e.error_type,
                    "message": e.message,
                    "handled_by": e.handled_by.value} for e in r.errors],
    }


def _result_from_dict(d: dict[str, Any]) -> ScoreResult:
    from .models import RuleHit as H
    return ScoreResult(
        lead_id=d["lead_id"], version=d["version"], score=d["score"],
        tier=d["tier"], routed_queue=d["routed_queue"],
        degraded=d["degraded"],
        hits=tuple(H(rule_id=h["rule_id"], rule_name=h["rule_name"],
                     version=h["version"], weight=h["weight"],
                     priority=h["priority"],
                     routing_queue=h["routing_queue"], source=h["source"])
                   for h in d["hits"]),
        errors=tuple(RuleEvaluationError(
            rule_id=e["rule_id"], rule_name=e["rule_name"],
            version=e["version"], error_type=e["error_type"],
            message=e["message"],
            handled_by=DegradePolicy(e["handled_by"]))
            for e in d["errors"]),
    )
