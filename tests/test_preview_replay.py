from lead_scoring.models import Lead, Rule, RuleSet
from lead_scoring.service import RuleEngineService
from tests.test_service_versions import ADMIN


def v1() -> RuleSet:
    return RuleSet(
        "v1",
        (Rule("score80", "Score 80", 10, {"field": "score", "op": "gte", "value": 80},
              points=80, route="sales"),),
        default_route="nurture",
    )


def v2() -> RuleSet:
    return RuleSet(
        "v2",
        (Rule("score70", "Score 70", 10, {"field": "score", "op": "gte", "value": 70},
              points=70, route="inside-sales"),),
        default_route="nurture",
    )


def test_preview_compares_new_ruleset_against_active_baseline() -> None:
    service = RuleEngineService()
    old = v1()
    service.save_draft(old, ADMIN)
    report_old = service.preview(old, (Lead("a", {"score": 75}),), ADMIN)
    service.publish("v1", ADMIN, report_old)

    new = v2()
    service.save_draft(new, ADMIN)
    report = service.preview(new, (Lead("a", {"score": 75}), Lead("b", {"score": 85})), ADMIN)
    assert report.passed is True
    assert report.baseline_version == "v1"
    by_lead = {item.lead_id: item for item in report.comparisons}
    assert by_lead["a"].old_score == 0
    assert by_lead["a"].new_score == 70
    assert by_lead["a"].old_route == "nurture"
    assert by_lead["a"].new_route == "inside-sales"
    assert by_lead["a"].changed is True
    assert by_lead["b"].old_score == 80
    assert by_lead["b"].new_score == 70
    assert by_lead["b"].changed is True


def test_replay_stored_draft_against_current_version() -> None:
    service = RuleEngineService()
    old = v1()
    service.save_draft(old, ADMIN)
    service.publish("v1", ADMIN, service.preview(old, (Lead("a", {"score": 75}),), ADMIN))

    new = v2()
    service.save_draft(new, ADMIN)
    comparison = service.replay("v2", (Lead("a", {"score": 75}),), ADMIN)
    assert comparison[0].old_version == "v1"
    assert comparison[0].new_version == "v2"
    assert comparison[0].changed is True
