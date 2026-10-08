from __future__ import annotations

from .models import Permission, User


class AuthorizationError(PermissionError):
    """Raised when an actor attempts an operation without permission."""

    def __init__(self, user_id: str, permission: Permission) -> None:
        self.user_id = user_id
        self.permission = permission
        super().__init__(f"user {user_id!r} lacks permission {permission.value!r}")


def require_permission(user: User, permission: Permission) -> None:
    """Raise :class:`AuthorizationError` unless *user* has *permission*."""
    if not isinstance(user, User):
        raise AuthorizationError("<anonymous>", permission)
    if permission not in user.permissions:
        raise AuthorizationError(user.user_id, permission)
