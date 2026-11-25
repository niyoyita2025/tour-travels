"""
Role-based access control.

Two roles share one authentication system (Django's User model):

* ADMIN  – a user with is_staff=True and an active account. Manages the whole website.
* CLIENT – any other active account. Browses tours, books, and manages their own account.

is_staff is the single source of truth, so a role can never disagree with the
permission check. Only administrators can grant or remove the ADMIN role
(see admin_views.py).
"""

from django.core.exceptions import PermissionDenied  # noqa: F401  (re-exported for convenience)

ROLE_ADMIN = 'ADMIN'
ROLE_CLIENT = 'CLIENT'


def is_admin_user(user):
    return bool(user and user.is_authenticated and user.is_active and user.is_staff)


def user_role(user):
    if not (user and user.is_authenticated):
        return None
    return ROLE_ADMIN if is_admin_user(user) else ROLE_CLIENT
