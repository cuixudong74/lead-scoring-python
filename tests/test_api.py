"""HTTP integration tests covering the full lifecycle over FastAPI."""
import pytest
from fastapi.testclient import TestClient

from lead_engine.api import create_app
from lead_engine.engine import RuleEngine

from .conftest import ruleset_v1

ADMIN = {"X-Actor": "alice", "X-Roles": "rule_admin"}
EDITOR = {"X-Actor": "ed", "X-Roles": "rule_editor"}
SERVICE = {"X-Actor": "svc", "X-Roles": "service"}


@pytest.fixture
def client():
    return TestClient(create_app(RuleEngine()))


def _publish_v1(client):
    assert client.post("/api/rulesets", json=ruleset_v1(),
                       headers=EDITOR).status_code == 201
    r = client.post("/api/rulesets/v1/publish", json={}, headers=ADMIN)
    assert r.status_code == 200


def test_create_publish_score_and_explain(client):
    _publish_v1(client)
    r = client.post("/api/score",
                    json={"leads": [{"id": "L1",
                                     "attributes": {"score": 90,
                                                    "country": "CN"}}]},
                    headers=SERVICE)
    assert r.status_code == 200
    result = r.json()["results"][0]
    assert result["version"] == "v1"
    assert result["score"] == 60
    assert [h["rule_id"] for h in result["hits"]] == ["hot", "cn_bonus"]
    assert result["errors"][0]["rule_id"] == "broken"


def test_score_requires_role(client):
    r = client.post("/api/score", json={"leads": []})
    assert r.status_code == 403


def test_publish_forbidden_for_editor_and_audited(client):
    client.post("/api/rulesets", json=ruleset_v1(), headers=EDITOR)
    r = client.post("/api/rulesets/v1/publish", json={}, headers=EDITOR)
    assert r.status_code == 403
    r = client.get("/api/audit?action=ruleset:publish", headers=ADMIN)
    assert any(e["denied"] for e in r.json()["events"])


def test_invalid_ruleset_returns_409(client):
    bad = {"version": "x", "rules": [
        {"id": "r", "condition": "bad syntax >>", "score_weight": 1}]}
    r = client.post("/api/rulesets", json=bad, headers=EDITOR)
    assert r.status_code == 409


def test_idempotent_submission_single_dispatch(client):
    _publish_v1(client)
    body = {"leads": [{"id": "X", "attributes": {"score": 90}}],
            "idempotency_key": "K"}
    first = client.post("/api/score", json=body, headers=SERVICE)
    second = client.post("/api/score", json=body, headers=SERVICE)
    assert first.json() == second.json()
    events = client.get("/api/audit", headers=ADMIN).json()["events"]
    assert [e["action"] for e in events].count("score:dispatch") == 1
    assert [e["action"] for e in events].count("score:replay") == 1


def test_rehearsal_failure_blocks_publish(client):
    _publish_v1(client)
    v2 = {"version": "v2", "rules": [
        {"id": "big", "condition": "score >= 0", "score_weight": 90}]}
    client.post("/api/rulesets", json=v2, headers=EDITOR)
    r = client.post("/api/rulesets/v2/rehearse",
                    json={"leads": [{"id": "h",
                                     "attributes": {"score": 10}}],
                          "gates": {"max_abs_delta": 5}},
                    headers=ADMIN)
    assert r.json()["passed"] is False
    r = client.post("/api/rulesets/v2/publish",
                    json={"rehearsal_leads": [{"id": "h",
                                               "attributes": {"score": 10}}],
                          "gates": {"max_abs_delta": 5}},
                    headers=ADMIN)
    assert r.status_code == 422
    assert client.get("/api/rulesets/active").json()["version"] == "v1"


def test_rollback_endpoint(client):
    _publish_v1(client)
    v2 = {"version": "v2", "rules": [
        {"id": "a", "condition": "x == 1", "score_weight": 3}]}
    client.post("/api/rulesets", json=v2, headers=EDITOR)
    client.post("/api/rulesets/v2/publish", json={}, headers=ADMIN)
    r = client.post("/api/rulesets/rollback", json={"version": "v1"},
                    headers=ADMIN)
    assert r.status_code == 200 and r.json()["active"] == "v1"


def test_audit_access_control(client):
    assert client.get("/api/audit",
                      headers={"X-Actor": "a", "X-Roles": "auditor"}
                      ).status_code == 200
    assert client.get("/api/audit").status_code == 403
