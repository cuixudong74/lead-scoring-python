import threading
from concurrent.futures import ThreadPoolExecutor

from lead_scoring.models import Lead, Permission, Rule, RuleSet, User
from tests.test_service_versions import ADMIN, save_preview_publish
from lead_scoring.service import RuleEngineService


def ruleset(version: str) -> RuleSet:
    return RuleSet(
        version=version,
        rules=(
            Rule("a", "A", 20, {"field": "country", "op": "in",
                                "value": ["CN", "SG"]}, points=20, route="apac"),
            Rule("b", "B", 10, {"field": "active", "op": "eq", "value": True},
                 points=10),
        ),
        default_route="nurture",
    )


def test_batch_is_order_insensitive_and_deduplicates_repeated_leads() -> None:
    service = RuleEngineService()
    rs = ruleset("v1")
    service.save_draft(rs, ADMIN)
    report = service.preview(rs, (Lead("1", {"country": "CN", "active": True}),), ADMIN)
    service.publish("v1", ADMIN, report)

    leads_1 = (
        Lead("1", {"active": True, "country": "CN"}),
        Lead("2", {"country": "SG", "active": False}),
    )
    leads_2 = (
        Lead("2", {"country": "SG", "active": False}),
        Lead("1", {"country": "CN", "active": True}),
        Lead("1", {"country": "CN", "active": True}),
    )

    one = {r.lead_id: r for r in service.score_batch(leads_1, ADMIN)}
    two = {r.lead_id: r for r in service.score_batch(leads_2, ADMIN)}
    assert set(two) == {"1", "2"}
    for lead_id in ("1", "2"):
        assert one[lead_id] == two[lead_id]
    assert one["1"].score == 30
    assert one["1"].route == "apac"
    assert one["2"].score == 20


def test_concurrent_scores_use_consistent_snapshot() -> None:
    service = RuleEngineService(max_workers=4)
    save_preview_publish(service, "v1")
    barrier = threading.Barrier(32)

    def score_once(i: int):
        barrier.wait()
        return service.score(Lead(f"lead-{i % 8}", {"score": 90}), ADMIN)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = tuple(pool.map(score_once, range(128)))
    assert {result.version for result in results} == {"v1"}


def test_concurrent_publish_and_rollback_ends_with_one_published_version() -> None:
    service = RuleEngineService()
    save_preview_publish(service, "v1")
    save_preview_publish(service, "v2")
    service.rollback("v1", ADMIN)
    # v2 was published before; its approved preview remains valid for republish.
    barrier = threading.Barrier(2)

    def publish_v2():
        barrier.wait()
        service.publish("v2", ADMIN)

    def rollback_v1():
        barrier.wait()
        service.rollback("v1", ADMIN)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish_v2), pool.submit(rollback_v1)]
        for future in futures:
            future.result()

    active = service.repository.active_snapshot()
    assert active is not None
    assert active.status.value == "published"
    assert active.ruleset.version in {"v1", "v2"}
    assert service.score(Lead("h", {"score": 90}), ADMIN).version == active.ruleset.version


def test_repeated_dispatch_has_one_side_effect() -> None:
    service = RuleEngineService()
    save_preview_publish(service, "v1")
    lead = Lead("h1", {"score": 90})
    sent = []
    service.dispatcher._sender = lambda record: sent.append(record.idempotency_key)

    first = service.dispatch(lead, ADMIN)
    second = service.dispatch(lead, ADMIN)
    assert first == second
    assert len(sent) == 1


def test_duplicate_concurrent_dispatch_has_one_side_effect() -> None:
    service = RuleEngineService()
    save_preview_publish(service, "v1")
    lead = Lead("h1", {"score": 90})
    sent = []
    lock = threading.Lock()

    def sender(record):
        with lock:
            sent.append(record.idempotency_key)

    service.dispatcher._sender = sender
    barrier = threading.Barrier(16)

    def dispatch_once():
        barrier.wait()
        return service.dispatch(lead, ADMIN)

    with ThreadPoolExecutor(max_workers=16) as pool:
        records = tuple(pool.map(lambda _: dispatch_once(), range(16)))
    assert len({record.idempotency_key for record in records}) == 1
    assert len(sent) == 1


def test_dispatch_without_route_does_not_invoke_sender() -> None:
    from lead_scoring.models import Rule

    unrouted = RuleSet(
        "v1",
        (Rule("active", "Active", 1, {"field": "active", "op": "eq", "value": True},
              points=10),),
    )
    service = RuleEngineService()
    service.save_draft(unrouted, ADMIN)
    service.publish(
        "v1",
        ADMIN,
        service.preview(unrouted, (Lead("1", {"active": True}),), ADMIN),
    )
    sent = []
    service.dispatcher._sender = lambda record: sent.append(record)
    service.dispatch(Lead("1", {"active": True}), ADMIN)
    assert sent == []
