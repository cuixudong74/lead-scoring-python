"""Tiny role-based authorization.

Privileged actions:
  ruleset:create, ruleset:rehearse, ruleset:publish,
  ruleset:rollback, audit:read

The engine never decides permissions inline: it asks the Authorizer, which
also returns the role/permission decision so *denials* can be audited.
"""
from __future__ import annotations

from collections.abc import Callable

from .exceptions import AuthorizationError

PERMISSIONS: dict[str, frozenset[str]] = {
    "ruleset:create": frozenset({"rule_editor", "rule_admin"}),
    "ruleset:rehearse": frozenset({"rule_editor", "rule_admin", "reviewer"}),
    "ruleset:publish": frozenset({"rule_admin"}),
    "ruleset:rollback": frozenset({"rule_admin"}),
    "audit:read": frozenset({"rule_admin", "auditor"}),
    "score:submit": frozenset({"rule_admin", "operator", "service"}),
}


class Authorizer:
    """Maps an actor id to a set of roles.

    *roles_provider* is ``callable(actor) -> Iterable[str]``. Unknown actors
    get no roles and are therefore denied every privileged action.
    """

    def __init__(self, roles_provider: Callable[[str], object] | None = None,
                 permissions: dict[str, frozenset[str]] | None = None) -> None:
        self._roles_provider = roles_provider or (lambda actor: ())
        self._permissions = permissions or PERMISSIONS

    def roles_of(self, actor: str) -> frozenset[str]:
        try:
            roles = self._roles_provider(actor)
        except Exception:
            return frozenset()
        return frozenset(roles or ())

    def check(self, actor: str, action: str) -> bool:
        allowed = self._permissions.get(action, frozenset())
        return bool(allowed & self.roles_of(actor))

    def require(self, actor: str, action: str) -> None:
        if not self.check(actor, action):
            raise AuthorizationError(
                f"actor {actor!r} is not permitted to perform {action!r}")
