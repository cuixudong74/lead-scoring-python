import pytest

from lead_scoring.models import (
    Lead,
    Permission,
    Rule,
    RuleSet,
    User,
    VersionStatus,
)
from lead_scoring.service import PreviewRejectedError, RuleEngineService

ADMIN = User("admin", frozenset({
    Permission.SCORE, Permission.DISPATCH, Permission.PUBLISH,
    Permission.ROLLBACK, Permission.PREVIEW, Permission.READ_AUDIT,
}))
READER = User("reader", frozenset({Permission.SCORE}))


def ruleset(version: str, route: str = "sales") -> RuleSet:
    return RuleSet(
        version=version,
        rules=(
            Rule("hot", "Hot", 100, {"field": "score", "op": "gte", "value": 80},
                 points=100, route=route),
            Rule("warm", "Warm", 10, {"field": "score", "op": "gte", "value": 50},
                 points=50),
        ),
        default_route="nurture",
    )


def save_preview_publish(service: RuleEngineService, version: str) -> None:
    rs = ruleset(version, f"route-{version}")
    service.save_draft(rs, ADMIN)
    report = service.preview(rs, (Lead("h1", {"score": 90}),), ADMIN)
    assert report.passed is True
    service.publish(version, ADMIN, report)


def test_draft_cannot_publish_without_successful_preview() -> None:
    service = RuleEngineService()
    service.save_draft(ruleset("v1"), ADMIN)
    with pytest.raises(PreviewRejectedError):
        service.publish("v1", ADMIN)
    assert service.repository.active_version_ref() is None


def test_failed_preview_blocks_publication() -> None:
    service = RuleEngineService()
    rs = ruleset("v1")
    service.save_draft(rs, ADMIN)
    report = service.preview(
        rs,
        (Lead("bad", {"score": "broken"}),),
        ADMIN,
    )
    assert report.passed is False
    assert report.reasons
    with pytest.raises(PreviewRejectedError):
        service.publish("v1", ADMIN, report)


def test_preview_checksum_mismatch_blocks_publication() -> None:
    service = RuleEngineService()
    v1 = ruleset("v1")
    service.save_draft(v1, ADMIN)
    other = ruleset("other")
    report = service.preview(other, (Lead("h1", {"score": 90}),), ADMIN)
    with pytest.raises(PreviewRejectedError):
        service.publish("v1", ADMIN, report)
    assert service.repository.active_version_ref() is None


def test_publish_uses_one_complete_snapshot() -> None:
    service = RuleEngineService()
    save_preview_publish(service, "v1")
    active = service.repository.active_snapshot()
    assert active is not None
    assert active.status is VersionStatus.PUBLISHED
    assert active.ruleset.version == "v1"
    result = service.score(Lead("h1", {"score": 90}), ADMIN)
    assert result.version == "v1"
    assert result.route == "route-v1"
    assert {hit.source_version for hit in result.hits} == {"v1"}


def test_rollback_activates_previous_published_snapshot() -> None:
    service = RuleEngineService()
    save_preview_publish(service, "v1")
    save_preview_publish(service, "v2")
    assert service.score(Lead("h1", {"score": 90}), ADMIN).version == "v2"

    service.rollback("v1", ADMIN)
    assert service.score(Lead("h1", {"score": 90}), ADMIN).version == "v1"
    assert service.repository.get_version("v2").status is VersionStatus.ROLLED_BACK
    assert service.repository.get_version("v1").status is VersionStatus.PUBLISHED


def test_cannot_rollback_to_never_published_draft() -> None:
    service = RuleEngineService()
    service.save_draft(ruleset("v1"), ADMIN)
    with pytest.raises(ValueError):
        service.rollback("v1", ADMIN)


def test_forbidden_operation_is_rejected_and_audited() -> None:
    service = RuleEngineService()
    with pytest.raises(PermissionError):
        service.save_draft(ruleset("v1"), READER)
    denied = [event for event in service.list_audit(ADMIN) if not event.allowed]
    assert len(denied) == 1
    assert denied[0].actor == "reader"
    assert denied[0].action == "ruleset.save_draft"


def test_score_audit_contains_version_input_and_hits() -> None:
    service = RuleEngineService()
    save_preview_publish(service, "v1")
    service.score(Lead("h1", {"score": 90}), ADMIN)
    events = service.list_audit(ADMIN)
    score_event = next(event for event in events if event.action == "ruleset.score_result")
    assert score_event.details["version"] == "v1"
    assert score_event.details["input"] == {"score": 90}
    assert score_event.details["hits"][0]["rule_id"] == "hot"
    assert score_event.details["hits"][0]["source_version"] == "v1"


def test_invalid_weight_and_condition_are_rejected_at_draft() -> None:
    service = RuleEngineService()
    invalid = RuleSet(
        "bad",
        (Rule("bad-rule", "Bad", 1, {"unknown": True}, points=1, weight=-1),),
    )
    with pytest.raises(ValueError) as exc_info:
        service.save_draft(invalid, ADMIN)
    message = str(exc_info.value)
    assert "weight must be >= 0" in message
    assert "unknown condition keys" in message


def test_hot_publish_switches_only_subsequent_evaluations() -> None:
    service = RuleEngineService()
    save_preview_publish(service, "v1")
    before = service.score(Lead("h", {"score": 90}), ADMIN)
    assert before.version == "v1"

    save_preview_publish(service, "v2")
    after = service.score(Lead("h", {"score": 90}), ADMIN)
    assert before.version == "v1"
    assert after.version == "v2"
    assert after.route == "route-v2"
