"""Shared vocabulary of well-known role and permission codes.

These constants live in ``shared`` (not ``identity``) because any business
module may reference them to build a policy — ``require(Perm(Permissions.\
USERS_READ))`` — without importing the identity module. Identity owns the
*authoritative* role/permission rows in its database and seeds these codes.
"""

from __future__ import annotations


class Permissions:
    """Predefined fine-grained permission codes."""

    USERS_READ = "perm.users.read"
    USERS_WRITE = "perm.users.write"
    USERS_DELETE = "perm.users.delete"
    ROLES_READ = "perm.roles.read"
    ROLES_WRITE = "perm.roles.write"
    ROLES_DELETE = "perm.roles.delete"
    PERMISSIONS_READ = "perm.permissions.read"
    PERMISSIONS_WRITE = "perm.permissions.write"
    SYSTEM_ADMIN = "perm.system.admin"


class Roles:
    """Predefined global role codes."""

    ADMIN = "admin"
    SUPPORT = "support"
    OPS = "ops"
