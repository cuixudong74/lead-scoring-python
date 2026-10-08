from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from .models import DispatchRecord, Lead, ScoreResult
from .repository import RuleRepository


def build_idempotency_key(lead: Lead, result: ScoreResult) -> str:
    """Build the business key for one deterministic routing decision."""
    payload: dict[str, Any] = {
        "lead_id": lead.lead_id,
        "input_hash": result.input_hash,
        "version": result.version,
        "route": result.route,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class Dispatcher:
    """Performs dispatch side effects at most once per business key."""

    def __init__(
        self,
        repository: RuleRepository,
        sender: Callable[[DispatchRecord], None] | None = None,
    ) -> None:
        self._repository = repository
        self._sender = sender

    def dispatch(self, lead: Lead, result: ScoreResult) -> DispatchRecord:
        key = build_idempotency_key(lead, result)
        existing = self._repository.get_dispatch(key)
        if existing is not None:
            return existing

        record = DispatchRecord(
            idempotency_key=key,
            lead_id=lead.lead_id,
            version=result.version,
            route=result.route or "",
            dispatched=result.route is not None,
        )
        stored, created = self._repository.put_dispatch_if_absent(record)
        # The caller that wins the put-if-absent race performs the side effect.
        if created and stored.dispatched and self._sender is not None:
            self._sender(stored)
        return stored
