from fastapi.testclient import TestClient

from lead_scoring.api import create_app
from lead_scoring.models import Permission
from lead_scoring.service import RuleEngineService


def headers(*permissions: Permission) -> dict[str, str]:
    return {
        "X-User-Id": "tester",
        "X-Permissions": ",".join(item.value for item in permissions),
    }


RULESET = {
    "version": "v1",
    "rules": [{
        "rule_id": "hot",
        "name": "Hot lead",
        "priority": 10,
        "condition": {"field": "score", "op": "gte", "value": 80},
        "points": 100,
        "weight": 1,
        "route": "sales",
    }],
    "pass_score": 80,
    "default_route": "nurture",
}


def test_api_end_to_end_publish_score_dispatch_and_audit() -> None:
    service = RuleEngineService()
    with TestClient(create_app(service)) as client:
        assert client.get("/health").json()["active_version"] is None

        all_perms = [
            Permission.SCORE, Permission.DISPATCH, Permission.PUBLISH,
            Permission.PREVIEW, Permission.READ_AUDIT,
        ]
        h = headers(*all_perms)
        assert client.post("/rulesets/draft", json=RULESET, headers=h).status_code == 200
        preview = client.post("/rulesets/preview", json={
            "ruleset": RULESET,
            "leads": [{"lead_id": "l1", "data": {"score": 90}}],
        }, headers=h)
        assert preview.status_code == 200
        assert preview.json()["passed"] is True

        published = client.post("/rulesets/publish", json={"version": "v1"}, headers=h)
        assert published.status_code == 200
        score = client.post("/score", json={"lead_id": "l1", "data": {"score": 90}}, headers=h)
        assert score.json()["version"] == "v1"
        assert score.json()["route"] == "sales"
        dispatch = client.post("/dispatch", json={"lead_id": "l1", "data": {"score": 90}}, headers=h)
        assert dispatch.status_code == 200

        audit = client.get("/audit", headers=h).json()["events"]
        assert any(event["action"] == "ruleset.publish" for event in audit)
        assert all(event["allowed"] for event in audit)


def test_api_forbidden_publish_is_denied() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/rulesets/draft",
            json=RULESET,
            headers=headers(Permission.SCORE),
        )
        assert response.status_code == 403

        audit_response = client.get("/audit", headers=headers(Permission.READ_AUDIT))
        assert audit_response.status_code == 200
        denied = [e for e in audit_response.json()["events"] if not e["allowed"]]
        assert len(denied) == 1
        assert denied[0]["actor"] == "tester"


def test_api_batch_deduplicates_and_preserves_outputs_by_lead_id() -> None:
    service = RuleEngineService()
    with TestClient(create_app(service)) as client:
        all_perms = [Permission.SCORE, Permission.PUBLISH, Permission.PREVIEW]
        h = headers(*all_perms)
        client.post("/rulesets/draft", json=RULESET, headers=h)
        client.post("/rulesets/preview", json={
            "ruleset": RULESET,
            "leads": [{"lead_id": "l1", "data": {"score": 90}}],
        }, headers=h)
        client.post("/rulesets/publish", json={"version": "v1"}, headers=h)
        response = client.post("/score/batch", json={"leads": [
            {"lead_id": "l2", "data": {"score": 10}},
            {"lead_id": "l1", "data": {"score": 90}},
            {"lead_id": "l1", "data": {"score": 90}},
        ]}, headers=h)
        results = response.json()["results"]
        assert [item["lead_id"] for item in results] == ["l2", "l1"]
        assert results[1]["hits"][0]["rule_id"] == "hot"


def test_api_replay_saved_version() -> None:
    service = RuleEngineService()
    with TestClient(create_app(service)) as client:
        h = headers(Permission.SCORE, Permission.PUBLISH, Permission.PREVIEW)
        client.post("/rulesets/draft", json=RULESET, headers=h)
        client.post("/rulesets/preview", json={
            "ruleset": RULESET,
            "leads": [{"lead_id": "l1", "data": {"score": 90}}],
        }, headers=h)
        client.post("/rulesets/publish", json={"version": "v1"}, headers=h)

        v2 = {
            **RULESET,
            "version": "v2",
            "rules": [{**RULESET["rules"][0], "rule_id": "warm",
                       "priority": 5, "points": 60}],
        }
        client.post("/rulesets/draft", json=v2, headers=h)
        response = client.post("/rulesets/replay", json={
            "version": "v2",
            "leads": [{"lead_id": "l1", "data": {"score": 90}}],
        }, headers=h)
        assert response.status_code == 200
        item = response.json()["comparisons"][0]
        assert item["old_version"] == "v1"
        assert item["new_version"] == "v2"
        assert item["changed"] is True
