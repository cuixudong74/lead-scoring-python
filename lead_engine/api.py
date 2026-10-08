"""FastAPI HTTP surface for the rule engine.

Auth model (kept dependency-light): each privileged request carries
``X-Actor`` and ``X-Roles`` (comma-separated) headers. The Authorizer's
roles provider resolves them server-side; in production swap the provider
for an IAM-backed one. Every endpoint maps engine errors to HTTP statuses
and the engine audits denials itself.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .auth import Authorizer
from .engine import RuleEngine
from .exceptions import (AuthorizationError, ConfigValidationError,
                         LeadEngineError, PublishConflictError,
                         RehearsalNotPassedError, VersionNotFoundError,
                         VersionStateError)
from .models import Lead

# ---------------------------------------------------------------------------
# request / response schemas
# ---------------------------------------------------------------------------


class RuleModel(BaseModel):
    id: str
    name: str | None = None
    condition: str
    score_weight: float
    priority: int = 0
    routing_queue: str | None = None
    channel: str | None = None
    enabled: bool = True


class BandModel(BaseModel):
    name: str
    min_score: float
    max_score: float


class RuleSetPayload(BaseModel):
    version: str
    description: str = ""
    default_queue: str = "default"
    default_degrade: str = "drop_rule"
    rules: list[RuleModel]
    score_bands: list[BandModel] = Field(default_factory=list)


class LeadModel(BaseModel):
    id: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class ScoreRequest(BaseModel):
    leads: list[LeadModel]
    idempotency_key: str | None = None


class GatesModel(BaseModel):
    max_abs_delta: float | None = None
    max_queue_change_rate: float | None = None
    max_error_rate: float | None = None


class RehearsalRequest(BaseModel):
    leads: list[LeadModel]
    gates: GatesModel | None = None


class PublishRequest(BaseModel):
    rehearsal_leads: list[LeadModel] | None = None
    gates: GatesModel | None = None


class RollbackRequest(BaseModel):
    version: str


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def _to_lead(m: LeadModel) -> Lead:
    return Lead(id=m.id, attributes=dict(m.attributes))


def create_app(engine: RuleEngine | None = None) -> FastAPI:
    engine = engine or RuleEngine()
    # The header role source is a *context var* so concurrent requests never
    # clobber each other's roles; the Authorizer itself is assigned once.
    import contextvars
    current_roles: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
        "current_roles", default=frozenset())
    engine.authorizer = Authorizer(lambda actor: current_roles.get())
    app = FastAPI(title="Lead Scoring Rule Engine", version="1.0.0")

    def run(actor: str | None, fn):
        try:
            return fn()
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except VersionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RehearsalNotPassedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (VersionStateError, PublishConflictError,
                ConfigValidationError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LeadEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def ctx(actor: str | None, x_roles: str | None) -> dict:
        roles = frozenset(r.strip() for r in (x_roles or "").split(",")
                          if r.strip())
        current_roles.set(roles)
        return {"actor": actor or "anonymous"}

    # ---------------------------------------------------------- rule sets ---
    @app.post("/api/rulesets", status_code=201)
    def create_ruleset(payload: RuleSetPayload,
                       x_actor: str | None = Header(default=None),
                       x_roles: str | None = Header(default=None)):
        kw = ctx(x_actor, x_roles)
        def op():
            version = engine.create_draft(
                payload.model_dump(exclude_none=True), **kw)
            return {"version": version}
        return run(x_actor, op)

    @app.get("/api/rulesets/active")
    def active_ruleset(x_actor: str | None = Header(default=None),
                       x_roles: str | None = Header(default=None)):
        ctx(x_actor, x_roles)
        version = engine.active_version()
        if version is None:
            raise HTTPException(404, "no active rule-set")
        from .config import ruleset_to_dict
        return ruleset_to_dict(engine.get_version(version))

    @app.post("/api/rulesets/{version}/rehearse")
    def rehearse(version: str, body: RehearsalRequest,
                 x_actor: str | None = Header(default=None),
                 x_roles: str | None = Header(default=None)):
        kw = ctx(x_actor, x_roles)
        def op():
            gates = body.gates.model_dump(exclude_none=True) if body.gates else None
            report = engine.rehearse(
                version, [_to_lead(l) for l in body.leads],
                gates=gates, **kw)
            return {
                "candidate_version": report.candidate_version,
                "baseline_version": report.baseline_version,
                "sample_size": report.sample_size,
                "passed": report.passed,
                "max_abs_delta": report.max_abs_delta,
                "queue_change_rate": report.queue_change_rate,
                "failure_reasons": list(report.failure_reasons),
                "diffs": [d.__dict__ for d in report.diffs],
            }
        return run(x_actor, op)

    @app.post("/api/rulesets/{version}/publish")
    def publish(version: str, body: PublishRequest | None = None,
                x_actor: str | None = Header(default=None),
                x_roles: str | None = Header(default=None)):
        kw = ctx(x_actor, x_roles)
        def op():
            leads = gates = None
            if body is not None:
                leads = [_to_lead(l) for l in body.rehearsal_leads] \
                    if body.rehearsal_leads else None
                gates = body.gates.model_dump(exclude_none=True) \
                    if body.gates else None
            engine.publish(version, rehearsal_leads=leads, gates=gates, **kw)
            return {"version": version, "active": engine.active_version()}
        return run(x_actor, op)

    @app.post("/api/rulesets/rollback")
    def rollback(body: RollbackRequest,
                 x_actor: str | None = Header(default=None),
                 x_roles: str | None = Header(default=None)):
        kw = ctx(x_actor, x_roles)
        def op():
            engine.rollback(body.version, **kw)
            return {"active": engine.active_version()}
        return run(x_actor, op)

    # -------------------------------------------------------------- scoring
    @app.post("/api/score")
    def score(body: ScoreRequest,
              x_actor: str | None = Header(default=None),
              x_roles: str | None = Header(default=None)):
        kw = ctx(x_actor, x_roles)
        leads = [_to_lead(l) for l in body.leads]

        def op():
            if body.idempotency_key:
                results = engine.submit(leads, body.idempotency_key, **kw)
            else:
                results = engine.score_batch(leads, **kw)
            return {"results": [_serialize_result(r) for r in results]}
        return run(x_actor, op)

    # ---------------------------------------------------------------- audit
    @app.get("/api/audit")
    def audit(action: str | None = None, actor: str | None = None,
              x_actor: str | None = Header(default=None),
              x_roles: str | None = Header(default=None)):
        kw = ctx(x_actor, x_roles)

        def op():
            engine._authorize(kw["actor"], "audit:read", None)
            return {"events": [e.__dict__ for e in
                               engine.audit.query(action=action, actor=actor)]}
        return run(x_actor, op)

    @app.get("/")
    def root():
        return {"service": "lead-scoring-rule-engine",
                "active_version": engine.active_version()}

    return app


def _serialize_result(r) -> dict[str, Any]:
    from .engine import _result_to_dict
    return _result_to_dict(r)
