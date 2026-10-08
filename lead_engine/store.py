"""Atomic, crash-safe persistence for rule-set versions.

Layout when a directory path is given::

    <dir>/versions/<version>.json   immutable version files
    <dir>/active.json.tmp           staging file for the active pointer
    <dir>/active.json               the single active pointer (atomic rename)

Guarantees:
* Evaluation reads the active pointer + one complete version file. A version
  file appears only after a full fsynced write, so a crashed publish can never
  leave a half-written ("field-level mixed") version behind.
* Activation is a single os.rename: readers see either the old or the new
  pointer, never a torn write.
* When *path* is None the store is in-memory (useful for tests).

Concurrency is serialized with an RLock; the engine additionally uses the
store lock to make the read-current + score path stable across swaps.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from .config import ruleset_to_dict
from .exceptions import (PublishConflictError, VersionNotFoundError,
                         VersionStateError)
from .models import RuleSet, VersionState


class RuleSetStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._drafts: dict[str, RuleSet] = {}
        self._states: dict[str, VersionState] = {}
        self._active: str | None = None
        if path is not None:
            os.makedirs(os.path.join(path, "versions"), exist_ok=True)
            self._load_from_disk()

    # ------------------------------------------------------------------ disk
    def _load_from_disk(self) -> None:
        from .config import parse_ruleset
        versions_dir = os.path.join(self._path, "versions")
        for name in os.listdir(versions_dir):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(versions_dir, name), encoding="utf-8") as fh:
                payload = json.load(fh)
            rs = parse_ruleset(payload, created_by=payload.get("created_by"))
            self._drafts[rs.version] = rs
            self._states[rs.version] = VersionState.SUPERSEDED
        active_file = os.path.join(self._path, "active.json")
        if os.path.exists(active_file):
            with open(active_file, encoding="utf-8") as fh:
                self._active = json.load(fh)["active"]
            self._states[self._active] = VersionState.ACTIVE

    def _atomic_write_json(self, target: str, data: dict[str, Any]) -> None:
        directory = os.path.dirname(target) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --------------------------------------------------------------- writers
    def save_draft(self, ruleset: RuleSet) -> None:
        """Persist a new draft. Fails if the version already exists."""
        with self._lock:
            if ruleset.version in self._states:
                raise VersionStateError(
                    f"version {ruleset.version!r} already exists; versions are "
                    "immutable, create a new version instead")
            if self._path is not None:
                target = os.path.join(self._path, "versions",
                                      f"{_safe(ruleset.version)}.json")
                if os.path.exists(target):
                    raise VersionStateError(
                        f"version file for {ruleset.version!r} already exists")
                self._atomic_write_json(target, ruleset_to_dict(ruleset))
            self._drafts[ruleset.version] = ruleset
            self._states[ruleset.version] = VersionState.DRAFT

    def mark_published(self, version: str) -> None:
        """Move a draft into the published-but-inactive pool."""
        with self._lock:
            state = self._states.get(version)
            if state is None:
                raise VersionNotFoundError(version)
            if state != VersionState.DRAFT:
                raise VersionStateError(
                    f"version {version!r} is {state.value}, only drafts can be "
                    "published")
            self._states[version] = VersionState.SUPERSEDED

    def activate(self, version: str) -> str | None:
        """Atomically make *version* active; returns the previous active id.

        The version must already be fully written and validated (a published
        draft). Concurrent activation: the lock + pointer file make the
        outcome deterministic; attempts to activate an unknown version fail
        before the pointer is touched.
        """
        with self._lock:
            state = self._states.get(version)
            if state is None:
                raise VersionNotFoundError(version)
            if state == VersionState.DRAFT:
                raise VersionStateError(
                    f"version {version!r} is still a draft; publish it first")
            previous = self._active
            if previous == version:
                raise PublishConflictError(
                    f"version {version!r} is already active")
            if self._path is not None:
                self._atomic_write_json(
                    os.path.join(self._path, "active.json"),
                    {"active": version})
            if previous is not None:
                self._states[previous] = VersionState.SUPERSEDED
            self._states[version] = VersionState.ACTIVE
            self._active = version
            return previous

    # ---------------------------------------------------------------- readers
    def get(self, version: str) -> RuleSet:
        with self._lock:
            rs = self._drafts.get(version)
            if rs is None:
                raise VersionNotFoundError(version)
            return rs

    def get_active(self) -> RuleSet | None:
        """Return the currently active RuleSet, or None if nothing is active."""
        with self._lock:
            if self._active is None:
                return None
            return self._drafts[self._active]

    def state_of(self, version: str) -> VersionState:
        with self._lock:
            state = self._states.get(version)
            if state is None:
                raise VersionNotFoundError(version)
            return state

    def active_version(self) -> str | None:
        with self._lock:
            return self._active

    def list_versions(self) -> list[str]:
        with self._lock:
            return sorted(self._states)

    def lock(self):
        """Exposed so the engine can hold the same lock across read+score."""
        return self._lock


def _safe(version: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in version)
