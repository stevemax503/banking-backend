"""Customer API access: locked customers may read, not mutate money/account actions."""
from rest_framework.permissions import IsAuthenticated

LOCKED_ACCOUNT_MESSAGE = 'Your account is locked. Please contact support for assistance.'

_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

_MUTATION_ALLOWLIST_PREFIXES = (
    '/api/auth/logout',
    '/api/auth/token/refresh',
    '/api/support/',
    '/api/notifications/',
)


class CustomerAccessPermission(IsAuthenticated):
    """Authenticated users; locked customers cannot POST/PUT/PATCH/DELETE except allowlisted paths."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        user = request.user
        if not getattr(user, 'is_locked', False):
            return True
        if getattr(user, 'is_admin_user', False) or getattr(user, 'is_staff', False):
            return True
        if request.method in _SAFE_METHODS:
            return True
        path = request.path or ''
        if any(path.startswith(prefix) for prefix in _MUTATION_ALLOWLIST_PREFIXES):
            return True
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied(detail=LOCKED_ACCOUNT_MESSAGE)
