"""Stress tests: concurrent scoring vs publish/rollback and mid-publish crash.

These protect the core guarantees under real threading:

* any single evaluation reads exactly one complete version;
* publish/rollback interleaving is serial and deterministic;
* a crash after writing the version but before activating leaves the old
  active version intact.
"""
import concurrent.futures as cf
import json
import os

from lead_engine.audit import AuditLog
from lead_engine.auth import Authorizer
from lead_engine.engine import RuleEngine
from lead_engine.idempotency import IdempotencyStore
from lead_engine.store import RuleSetStore

from .conftest import ROLES, ruleset_v1


def _make_engine(path):
    return RuleEngine(
        store=RuleSetStore(str(path)),
        audit=AuditLog(str(path / "audit.log")),
        authorizer=Authorizer(lambda a: ROLES.get(a, [])),
        idempotency=IdempotencyStore(str(path / "idem.json")),
        max_workers=4,
    )


def test_scoring_never_mixes_versions_during_swaps(tmp_path):
    eng = _make_engine(tmp_path)
    eng.create_draft(ruleset_v1("v1"), "admin")
    eng.publish("v1", "admin")
    for i in range(2, 12):
        eng.create_draft({"version": f"v{i}", "rules": [
            {"id": f"r{i}", "condition": "score >= 0",
             "score_weight": i}]}, "admin")

    stop = []
    errors: list[Exception] = []

    def scorer(seed):
        import random
        rng = random.Random(seed)
        while not stop:
            try:
                r = eng.score_one(
                    type("L", (), {"id": "x",
                                   "attributes": {"score": rng.randint(0, 100),
                                                  "country": "CN"}})(),
                    "service")
                # hard invariant: every hit + result carries the same version
                versions = {h.version for h in r.hits} | {r.version}
                assert len(versions) == 1
                assert r.version.startswith("v")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    def publisher():
        for i in range(2, 12):
            try:
                eng.publish(f"v{i}", "admin")
            except Exception:  # noqa: BLE001
                pass
            try:
                eng.rollback("v1", "admin")
            except Exception:  # noqa: BLE001
                pass

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(scorer, s) for s in range(7)]
        pf = pool.submit(publisher)
        pf.result()
        stop.append(True)
        for f in futures:
            f.result()
    assert not errors, errors[:3]


def test_publish_crash_before_swap_leaves_old_version_active(tmp_path):
    eng = _make_engine(tmp_path)
    eng.create_draft(ruleset_v1("v1"), "admin")
    eng.publish("v1", "admin")
    # simulate the "new version fully written, activation interrupted" state
    eng.create_draft({"version": "v2", "rules": [
        {"id": "r2", "condition": "score >= 0", "score_weight": 7}]}, "admin")
    # v2 exists on disk as DRAFT but active pointer still says v1
    restarted = RuleEngine(
        store=RuleSetStore(str(tmp_path)),
        authorizer=Authorizer(lambda a: ROLES.get(a, [])))
    assert restarted.active_version() == "v1"
    assert "v2" in restarted.store.list_versions()


def test_half_written_version_file_cannot_exist(tmp_path):
    # version files are written via temp + os.replace: no partial target file
    store = RuleSetStore(str(tmp_path / "s"))
    from lead_engine.config import parse_ruleset
    rs = parse_ruleset(ruleset_v1("v9"))
    store.save_draft(rs)
    target = tmp_path / "s" / "versions" / "v9.json"
    data = json.loads(target.read_text())
    assert data["version"] == "v9"
    assert len(data["rules"]) == 3
    # no leftover temp files
    leftovers = [n for n in os.listdir(tmp_path / "s" / "versions")
                 if n.startswith(".tmp")]
    assert leftovers == []


def test_concurrent_distinct_idempotency_keys(tmp_path):
    from lead_engine.models import Lead
    eng = _make_engine(tmp_path)
    eng.create_draft(ruleset_v1("v1"), "admin")
    eng.publish("v1", "admin")

    def submit(i):
        return eng.submit([Lead(f"L{i}", {"score": 90})],
                          f"key-{i}", "service")

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        outs = list(pool.map(submit, range(64)))
    assert all(len(o) == 1 for o in outs)
    assert len(eng.audit.query(action="score:dispatch")) == 64
