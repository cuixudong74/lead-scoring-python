"""Append-only audit log.

Events are serialized as one JSON object per line (JSONL). Writes append to
the file and fsync; the log is never rewritten, so it stays a faithful audit
trail even if a process dies mid-write. Every privileged action -- including
*denied* attempts -- is recorded with actor, action, version context and
inputs where applicable.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict


from .models import AuditEvent


class AuditLog:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._events: list[AuditEvent] = []
        if path is not None:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        raw = json.loads(line)
                        self._events.append(AuditEvent(**raw))

    def record(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            self._events.append(event)
            if self._path is not None:
                # append-only; a single line write plus fsync
                line = json.dumps(asdict(event), ensure_ascii=False,
                                  sort_keys=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        return event

    def query(self, action: str | None = None,
              actor: str | None = None) -> list[AuditEvent]:
        with self._lock:
            return [e for e in self._events
                    if (action is None or e.action == action)
                    and (actor is None or e.actor == actor)]

    def all_events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)
