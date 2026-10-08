from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException

from .auth import AuthorizationError
from .models import (
    Lead,
    Permission,
    Rule,
    RuleErrorPolicy,
    RuleSet,
    User,
)
from .schemas import (
    LeadIn,
    LeadsIn,
    PreviewRequest,
    PublishRequest,
    ReplayRequest,
    RuleSetIn,
)
from .service import PreviewRejectedError, RuleEngineService


def to_ruleset(payload: RuleSetIn) -> RuleSet:
    return RuleSet(
        version=payload.version,
        rules=tuple(
            Rule(
                rule_id=rule.rule_id,
                name=rule.name,
                priority=rule.priority,
                condition=rule.condition,
                points=rule.points,
                weight=rule.weight,
                route=rule.route,
                error_policy=RuleErrorPolicy(rule.error_policy),
            )
            for rule in payload.rules
        ),
        pass_score=payload.pass_score,
        default_route=payload.default_route,
    )


def to_lead(payload) -> Lead:
    return Lead(lead_id=payload.lead_id, data=payload.data)


def current_user(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_permissions: Annotated[str | None, Header(alias="X-Permissions")] = None,
) -> User:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="missing X-User-Id")
    values = {item.strip() for item in (x_permissions or "").split(",") if item.strip()}
    try:
        permissions = frozenset(Permission(value) for value in values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unknown permission") from exc
    return User(user_id=x_user_id, permissions=permissions)


def create_app(service: RuleEngineService | None = None) -> FastAPI:
    app = FastAPI(title="Lead Scoring Rule Engine")
    app.state.service = service or RuleEngineService()

    @app.get("/health")
    def health():
        ref = app.state.service.repository.active_version_ref()
        return {"status": "ok", "active_version": ref}

    @app.post("/rulesets/draft")
    def save_draft(payload: RuleSetIn, user: Annotated[User, Depends(current_user)]):
        try:
            stored = app.state.service.save_draft(to_ruleset(payload), user)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"version": stored.ruleset.version, "status": stored.status.value,
                "checksum": stored.checksum}

    @app.post("/rulesets/preview")
    def preview(payload: PreviewRequest, user: Annotated[User, Depends(current_user)]):
        try:
            report = app.state.service.preview(
                to_ruleset(payload.ruleset),
                tuple(to_lead(lead) for lead in payload.leads),
                user,
                payload.require_no_degradation,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(report)

    @app.post("/rulesets/replay")
    def replay(payload: ReplayRequest, user: Annotated[User, Depends(current_user)]):
        try:
            comparisons = app.state.service.replay(
                payload.version,
                tuple(to_lead(lead) for lead in payload.leads),
                user,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"comparisons": [asdict(item) for item in comparisons]}

    @app.post("/rulesets/publish")
    def publish(payload: PublishRequest, user: Annotated[User, Depends(current_user)]):
        try:
            stored = app.state.service.publish(payload.version, user)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except PreviewRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"version": stored.ruleset.version, "status": stored.status.value}

    @app.post("/rulesets/{version}/rollback")
    def rollback(version: str, user: Annotated[User, Depends(current_user)]):
        try:
            stored = app.state.service.rollback(version, user)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"version": stored.ruleset.version, "status": stored.status.value}

    @app.post("/score")
    def score(payload: LeadIn, user: Annotated[User, Depends(current_user)]):
        try:
            result = app.state.service.score(to_lead(payload), user)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return asdict(result)

    @app.post("/score/batch")
    def score_batch(payload: LeadsIn, user: Annotated[User, Depends(current_user)]):
        try:
            results = app.state.service.score_batch(
                tuple(to_lead(lead) for lead in payload.leads), user
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"results": [asdict(result) for result in results]}

    @app.post("/dispatch")
    def dispatch(payload: LeadIn, user: Annotated[User, Depends(current_user)]):
        try:
            record = app.state.service.dispatch(to_lead(payload), user)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return asdict(record)

    @app.get("/audit")
    def audit(user: Annotated[User, Depends(current_user)]):
        try:
            events = app.state.service.list_audit(user)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"events": [asdict(event) for event in events]}

    return app



app = create_app()
