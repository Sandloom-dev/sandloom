"""Well-known role and permission codes of the identity module.

Identity owns the authoritative role and permission rows in its database;
these constants name the codes it relies on.

Other modules must not import this module (module isolation). They reference
the codes as plain strings in their policies, for example
``require(Perm("perm.users.read"))`` or ``require(Role("admin"))``.
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
