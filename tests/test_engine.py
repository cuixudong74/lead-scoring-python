"""Engine tests: conflicts, hot reload, rollback, concurrency, duplicate
submission and per-rule fault isolation."""
import concurrent.futures as cf
import threading

import pytest

from lead_engine.exceptions import (AuthorizationError, LeadEngineError,
                                    RehearsalNotPassedError,
                                    VersionStateError)
from lead_engine.models import DegradePolicy, Lead, RuleSet

from .conftest import ruleset_v1


# ---------------------------------------------------------------------------
# scoring semantics: priority, conflict resolution, explainability
# ---------------------------------------------------------------------------

def test_multi_rule_hit_with_weights_and_sources(active_engine):
    r = active_engine.score_one(Lead("L1", {"score": 90, "country": "CN"}),
                                "service")
    assert r.version == "v1"
    assert r.score == 60.0
    assert r.tier == "hot"
    # highest-priority hit decides the distribution queue
    assert r.routed_queue == "hot-q"
    ids = [h.rule_id for h in r.hits]
    assert ids == ["hot", "cn_bonus"]
    assert r.hits[0].weight == 40 and r.hits[0].source == "web"
    assert r.hits[1].weight == 20 and r.hits[1].source == "partner"


def test_priority_tie_broken_deterministically_by_id(engine):
    payload = {"version": "v", "rules": [
        {"id": "zeta", "condition": "s == 'x'", "score_weight": 10,
         "priority": 5, "routing_queue": "q-zeta"},
        {"id": "alpha", "condition": "s == 'x'", "score_weight": 10,
         "priority": 5, "routing_queue": "q-alpha"},
    ]}
    engine.create_draft(payload, "editor")
    engine.publish("v", "admin")
    for _ in range(5):
        r = engine.score_one(Lead("l", {"s": "x"}), "service")
        # deterministic queue even though priorities tie
        assert r.routed_queue == "q-alpha"
        assert [h.rule_id for h in r.hits] == ["alpha", "zeta"]


def test_score_is_clamped_and_bands_apply(engine):
    engine.create_draft({"version": "v", "rules": [
        {"id": "a", "condition": "x == 1", "score_weight": 500},
        {"id": "b", "condition": "y == 1", "score_weight": -500},
    ]}, "editor")
    engine.publish("v", "admin")
    assert engine.score_one(Lead("p", {"x": 1}), "service").score == 100.0
    low = engine.score_one(Lead("n", {"y": 1}), "service")
    assert low.score == 0.0 and low.routed_queue == "default"


def test_no_active_version_errors(engine):
    with pytest.raises(LeadEngineError):
        engine.score_one(Lead("l", {}), "service")


# ---------------------------------------------------------------------------
# rule isolation / degrade policies
# ---------------------------------------------------------------------------

def test_failing_rule_is_isolated_and_explained(active_engine):
    r = active_engine.score_one(Lead("L2", {"score": 90, "country": "US"}),
                                "service")
    assert r.score == 40  # 'hot' still contributes
    assert r.degraded is True
    err = next(e for e in r.errors if e.rule_id == "broken")
    assert err.version == "v1"
    assert err.handled_by == DegradePolicy.DROP_RULE
    assert "missing" in err.message


def test_fail_batch_degrade_aborts_without_silence(engine):
    p = ruleset_v1(default_degrade="fail_batch")
    engine.create_draft(p, "editor")
    engine.publish("v1", "admin")
    from lead_engine.exceptions import RuleExecutionError
    with pytest.raises(RuleExecutionError):
        engine.score_batch([Lead("l", {"score": 90})], "service")


# ---------------------------------------------------------------------------
# determinism / concurrency / duplicate input
# ---------------------------------------------------------------------------

def test_batch_order_and_duplicates_are_consistent(active_engine, sample_leads):
    import random
    baseline = {r.lead_id: r for r in
                active_engine.score_batch(sample_leads, "service")}
    dup = list(sample_leads) + list(sample_leads)
    random.Random(7).shuffle(dup)
    again = {r.lead_id: r for r in active_engine.score_batch(dup, "service")}
    for lid, want in baseline.items():
        got = again[lid]
        assert got.score == want.score
        assert got.routed_queue == want.routed_queue
        assert [h.rule_id for h in got.hits] == [h.rule_id for h in want.hits]


def test_concurrent_scoring_is_deterministic(active_engine, sample_leads):
    results: list[dict] = []

    def worker(seed):
        local = list(sample_leads)
        random.Random(seed).shuffle(local)
        return {r.lead_id: (r.score, r.routed_queue,
                            tuple(h.rule_id for h in r.hits))
                for r in active_engine.score_batch(local, "service")}

    import random
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(worker, range(24)))
    reference = results[0]
    assert all(r == reference for r in results)


def test_duplicate_submission_has_single_side_effect(active_engine):
    leads = [Lead("X", {"score": 90})]
    r1 = active_engine.submit(leads, "key-9", "service")
    r2 = active_engine.submit(leads, "key-9", "service")
    r3 = active_engine.submit(leads, "key-9", "service")
    assert r1 == r2 == r3
    dispatches = active_engine.audit.query(action="score:dispatch")
    replays = active_engine.audit.query(action="score:replay")
    assert len(dispatches) == 1
    assert len(replays) == 2
    assert dispatches[0].detail["dispatches"] == [
        {"lead_id": "X", "queue": "hot-q"}]


def test_concurrent_duplicate_keys_compute_once(active_engine):
    barrier = threading.Barrier(8)
    seen = []

    def submit(_):
        barrier.wait()
        out = active_engine.submit([Lead("C", {"score": 90})], "dup",
                                   "service")
        seen.append(out)
        return out

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        outs = list(pool.map(submit, range(8)))
    assert all(o == outs[0] for o in outs)
    assert len(active_engine.audit.query(action="score:dispatch")) == 1


# ---------------------------------------------------------------------------
# publish / hot reload / rollback
# ---------------------------------------------------------------------------

def test_versions_are_immutable_and_complete(engine, persisted_engine):
    # evaluation only ever sees a fully published version
    pe = persisted_engine
    pe.create_draft(ruleset_v1("v1"), "editor")
    # draft must not serve traffic
    assert pe.active_version() is None
    with pytest.raises(LeadEngineError):
        pe.score_one(Lead("l", {"score": 90}), "service")
    pe.publish("v1", "admin")
    assert pe.score_one(Lead("l", {"score": 90}), "service").version == "v1"


def test_hot_reload_swap(active_engine, sample_leads, persisted_engine=None):
    eng = active_engine
    v2 = {"version": "v2", "rules": [
        {"id": "all", "condition": "score >= 0", "score_weight": 5}]}
    eng.create_draft(v2, "editor")
    assert eng.active_version() == "v1"  # draft invisible to scoring
    eng.publish("v2", "admin")
    assert eng.active_version() == "v2"
    r = eng.score_one(sample_leads[0], "service")
    assert r.version == "v2" and r.score == 5
    events = [e.action for e in eng.audit.all_events()]
    assert events.count("ruleset:publish") == 2


def test_rollback_restores_previous_version(active_engine, sample_leads):
    eng = active_engine
    eng.create_draft({"version": "v2", "rules": [
        {"id": "all", "condition": "score >= 0", "score_weight": 5}]},
        "editor")
    eng.publish("v2", "admin")
    assert eng.score_one(sample_leads[0], "service").version == "v2"
    eng.rollback("v1", "admin")
    r = eng.score_one(sample_leads[0], "service")
    assert r.version == "v1" and r.score == 60
    rollbacks = eng.audit.query(action="ruleset:rollback")
    assert rollbacks[0].detail["version"] == "v1"
    assert rollbacks[0].detail["previous_version"] == "v2"


def test_cannot_rollback_to_draft(active_engine):
    active_engine.create_draft({"version": "wip", "rules": [
        {"id": "r", "condition": "a == 1", "score_weight": 1}]}, "editor")
    with pytest.raises(VersionStateError):
        active_engine.rollback("wip", "admin")


def test_duplicate_version_rejected(active_engine):
    with pytest.raises(VersionStateError):
        active_engine.create_draft(ruleset_v1("v1"), "editor")


def test_concurrent_publish_and_rollback_is_consistent(active_engine):
    eng = active_engine
    eng.create_draft({"version": "v2", "rules": [
        {"id": "all", "condition": "score >= 0", "score_weight": 5}]},
        "editor")
    eng.publish("v2", "admin")

    outcomes = []

    def do_publish_v2_again():
        # v2 is not a draft -> must fail deterministically
        try:
            eng.create_draft({"version": "v3", "rules": [
                {"id": "a", "condition": "x == 1", "score_weight": 7}]},
                "editor")
            eng.publish("v3", "admin")
            outcomes.append("v3")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"err:{type(exc).__name__}")

    def do_rollback():
        try:
            eng.rollback("v1", "admin")
            outcomes.append("v1")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"err:{type(exc).__name__}")

    t1 = threading.Thread(target=do_publish_v2_again)
    t2 = threading.Thread(target=do_rollback)
    t1.start(); t2.start(); t1.join(); t2.join()
    active = eng.active_version()
    assert active in {"v1", "v3"}
    # whichever won, scoring reads exactly one complete version
    r = eng.score_one(Lead("l", {"x": 1, "score": 90}), "service")
    assert r.version == active
    assert all(h.version == active for h in r.hits)


# ---------------------------------------------------------------------------
# rehearsal gates
# ---------------------------------------------------------------------------

def test_failed_rehearsal_blocks_activation(active_engine):
    eng = active_engine
    eng.create_draft({"version": "vbad", "rules": [
        {"id": "big", "condition": "score >= 0", "score_weight": 90}]},
        "editor")
    hist = [Lead("h1", {"score": 90, "country": "CN"}),
            Lead("h2", {"score": 10})]
    report = eng.rehearse("vbad", hist, "reviewer",
                          gates={"max_abs_delta": 5,
                                 "max_queue_change_rate": 1.0})
    assert report.passed is False
    assert report.max_abs_delta > 5
    with pytest.raises(RehearsalNotPassedError):
        eng.publish("vbad", "admin", rehearsal_leads=hist,
                    gates={"max_abs_delta": 5,
                           "max_queue_change_rate": 1.0})
    # failed publish leaves old version active
    assert eng.active_version() == "v1"
    assert eng.score_one(Lead("x", {"score": 90}), "service").version == "v1"


def test_passed_rehearsal_allows_activation_and_reports_diff(active_engine):
    eng = active_engine
    eng.create_draft({"version": "vnext", "default_queue": "general",
                      "rules": [
        {"id": "hot", "condition": "score >= 80", "score_weight": 40,
         "priority": 100, "routing_queue": "hot-q", "channel": "web"}],
        "score_bands": [{"name": "hot", "min_score": 50, "max_score": 100}]},
        "editor")
    hist = [Lead("h1", {"score": 90})]
    report = eng.rehearse("vnext", hist, "reviewer",
                          gates={"max_abs_delta": 0,
                                 "max_queue_change_rate": 0})
    assert report.passed is True
    d = report.diffs[0]
    assert d.score_delta == 0 and d.queue_changed is False
    eng.publish("vnext", "admin")
    assert eng.active_version() == "vnext"


def test_rehearsal_error_rate_gate(active_engine):
    eng = active_engine
    # a rule referencing a missing field with drop policy still records errors
    report = eng.rehearse(
        "v1", [Lead("h", {"score": 90})], "reviewer",
        gates={"max_abs_delta": 1000.0, "max_queue_change_rate": 1.0,
               "max_error_rate": 0.0})
    assert report.passed is False
    assert any("error_rate" in r for r in report.failure_reasons)


# ---------------------------------------------------------------------------
# authorization & audit
# ---------------------------------------------------------------------------

def test_unauthorized_actions_are_denied_and_audited(engine):
    payload = ruleset_v1()
    with pytest.raises(AuthorizationError):
        engine.create_draft(payload, "nobody")
    denied = [e for e in engine.audit.all_events() if e.denied]
    assert len(denied) == 1
    assert denied[0].actor == "nobody"
    assert denied[0].action == "ruleset:create"


def test_role_separation_publish_requires_admin(active_engine):
    active_engine.create_draft({"version": "v2", "rules": [
        {"id": "a", "condition": "x == 1", "score_weight": 1}]}, "editor")
    with pytest.raises(AuthorizationError):
        active_engine.publish("v2", "editor")
    # reviewer may rehearse but not publish
    active_engine.rehearse("v2", [Lead("l", {"x": 1})], "reviewer")
    with pytest.raises(AuthorizationError):
        active_engine.rollback("v1", "reviewer")


def test_audit_trail_captures_version_and_inputs(active_engine):
    active_engine.submit([Lead("A", {"score": 90})], "audit-key", "service")
    dispatch = active_engine.audit.query(action="score:dispatch")[0]
    assert dispatch.actor == "service"
    assert dispatch.detail["version"] == "v1"
    assert dispatch.detail["dispatches"][0]["lead_id"] == "A"


# ---------------------------------------------------------------------------
# persistence across restart
# ---------------------------------------------------------------------------

def test_active_version_survives_restart(persisted_engine):
    pe = persisted_engine
    pe.create_draft(ruleset_v1("v1"), "editor")
    pe.publish("v1", "admin")
    pe.create_draft({"version": "v2", "rules": [
        {"id": "a", "condition": "x == 1", "score_weight": 3}]}, "editor")
    pe.publish("v2", "admin")

    from lead_engine.engine import RuleEngine
    from lead_engine.store import RuleSetStore
    restarted = RuleEngine(store=RuleSetStore(pe.store._path),
                           authorizer=pe.authorizer)
    assert restarted.active_version() == "v2"
    r = restarted.score_one(Lead("l", {"x": 1}), "service")
    assert r.version == "v2"
    # rollback still works after restart (v1 retained, not deleted)
    restarted.rollback("v1", "admin")
    assert restarted.active_version() == "v1"


def test_score_evaluate_audit_has_version_hits_and_input(active_engine):
    active_engine.score_batch([Lead("A", {"score": 90, "country": "CN"})],
                              "service")
    ev = active_engine.audit.query(action="score:evaluate")
    assert len(ev) == 1
    lead_rec = ev[0].detail["leads"][0]
    assert ev[0].detail["version"] == "v1"
    assert lead_rec["lead_id"] == "A"
    assert "hot" in lead_rec["hits"] and "cn_bonus" in lead_rec["hits"]
    assert lead_rec["error_rules"] == ["broken"]
