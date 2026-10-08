"""Shared fixtures."""
from __future__ import annotations

import pytest

from lead_engine.auth import Authorizer
from lead_engine.engine import RuleEngine
from lead_engine.models import Lead

ROLES = {
    "admin": ["rule_admin"],
    "editor": ["rule_editor"],
    "reviewer": ["reviewer"],
    "auditor": ["auditor"],
    "service": ["service", "operator"],
    "nobody": [],
}


@pytest.fixture
def engine(tmp_path):
    return RuleEngine(
        authorizer=Authorizer(lambda actor: ROLES.get(actor, [])),
    )


@pytest.fixture
def persisted_engine(tmp_path):
    from lead_engine.audit import AuditLog
    from lead_engine.idempotency import IdempotencyStore
    from lead_engine.store import RuleSetStore
    return RuleEngine(
        store=RuleSetStore(str(tmp_path / "store")),
        audit=AuditLog(str(tmp_path / "audit.log")),
        authorizer=Authorizer(lambda actor: ROLES.get(actor, [])),
        idempotency=IdempotencyStore(str(tmp_path / "idem.json")),
    )


def ruleset_v1(version: str = "v1", **over):
    payload = {
        "version": version,
        "default_queue": "general",
        "rules": [
            {"id": "hot", "name": "Hot lead",
             "condition": "score >= 80", "score_weight": 40,
             "priority": 100, "routing_queue": "hot-q", "channel": "web"},
            {"id": "cn_bonus", "name": "China bonus",
             "condition": "country == 'CN'", "score_weight": 20,
             "priority": 50, "routing_queue": "cn-q", "channel": "partner"},
            {"id": "broken", "name": "Broken threshold",
             "condition": "profile.missing.deep > 10", "score_weight": 99,
             "priority": 10},
        ],
        "score_bands": [
            {"name": "hot", "min_score": 50, "max_score": 100},
            {"name": "cold", "min_score": 0, "max_score": 49.999},
        ],
    }
    payload.update(over)
    return payload


@pytest.fixture
def active_engine(engine):
    engine.create_draft(ruleset_v1(), "editor")
    engine.publish("v1", "admin")
    return engine


@pytest.fixture
def sample_leads():
    return [
        Lead("L1", {"score": 90, "country": "CN"}),
        Lead("L2", {"score": 90, "country": "US"}),
        Lead("L3", {"score": 30, "country": "CN"}),
        Lead("L4", {"score": 10, "country": "US"}),
    ]
