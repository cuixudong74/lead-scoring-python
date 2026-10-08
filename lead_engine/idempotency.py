"""Idempotency for batch submissions.

Scoring is pure and safe to repeat, but *distribution* is a side effect. A
submission therefore carries an idempotency key. The first request computes
the results and records the key -> result mapping; retries with the same key
return the stored results without re-running distribution.

Storage is a JSON file written atomically (tmp + rename) under a lock, with
an in-flight marker so concurrent requests using the same key cannot both
proceed: the second caller blocks briefly then sees the completed result.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

# In-memory sentinel; the on-disk marker is the JSON-safe string below.
_PENDING = object()
_PENDING_MARKER = "__pending__"


class IdempotencyStore:
    def __init__(self, path: str | None = None) -> None:
        self._cond = threading.Condition()
        self._path = path
        self._data: dict[str, Any] = {}
        if path is not None:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._data = {k: (_PENDING if v == _PENDING_MARKER else v)
                              for k, v in raw.items()}

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        on_disk = {k: (_PENDING_MARKER if v is _PENDING else v)
                   for k, v in self._data.items()}
        directory = os.path.dirname(os.path.abspath(self._path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".idem-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(on_disk, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def begin(self, key: str, timeout: float = 10.0) -> tuple[bool, Any]:
        """Claim *key* for a first-time computation.

        Returns ``(first_time, existing_result)``:
          * ``(True, None)``   -> caller must compute and call :meth:`complete`
          * ``(False, result)`` -> a completed result already exists
        If another thread is currently computing with the same key, wait until
        it completes (or timeout), ensuring concurrent duplicates see one
        deterministic result.
        """
        with self._cond:
            deadline = timeout
            while key in self._data and self._data[key] is _PENDING:
                if not self._cond.wait(timeout=deadline):
                    # treat timeout as seeing a pending result: caller should
                    # retry later rather than performing duplicate effects.
                    return False, {"__pending__": True}
            if key in self._data:
                return False, self._data[key]
            self._data[key] = _PENDING
            self._persist_locked()
            return True, None

    def complete(self, key: str, value: Any) -> None:
        with self._cond:
            self._data[key] = value
            self._persist_locked()
            self._cond.notify_all()

    def fail(self, key: str) -> None:
        """Remove an in-flight marker after a failed computation."""
        with self._cond:
            self._data.pop(key, None)
            self._persist_locked()
            self._cond.notify_all()

    def get(self, key: str) -> Any:
        with self._cond:
            value = self._data.get(key)
            return None if value is _PENDING else value
