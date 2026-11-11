"""The well-known role and permission codes of identity (``sandloom_identity.rbac``)."""

import ast
from pathlib import Path

from sandloom_identity import rbac
from sandloom_identity.rbac import Permissions, Roles


def test_permission_codes_are_the_seeded_wire_values() -> None:
    assert {
        name: value for name, value in vars(Permissions).items() if name.isupper()
    } == {
        "USERS_READ": "perm.users.read",
        "USERS_WRITE": "perm.users.write",
        "USERS_DELETE": "perm.users.delete",
        "ROLES_READ": "perm.roles.read",
        "ROLES_WRITE": "perm.roles.write",
        "ROLES_DELETE": "perm.roles.delete",
        "PERMISSIONS_READ": "perm.permissions.read",
        "PERMISSIONS_WRITE": "perm.permissions.write",
        "SYSTEM_ADMIN": "perm.system.admin",
    }


def test_role_codes_are_the_seeded_wire_values() -> None:
    assert {name: value for name, value in vars(Roles).items() if name.isupper()} == {
        "ADMIN": "admin",
        "SUPPORT": "support",
        "OPS": "ops",
    }


def test_module_imports_nothing_and_has_no_side_effects() -> None:
    tree = ast.parse(Path(rbac.__file__).read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]
    assert [n.module for n in imports if isinstance(n, ast.ImportFrom)] == [
        "__future__"
    ]
    assert not [n for n in imports if isinstance(n, ast.Import)]
    # Only class definitions run at import: no calls at module level.
    assert all(
        isinstance(n, ast.ClassDef | ast.ImportFrom | ast.Expr) for n in tree.body
    )
    assert [n.name for n in tree.body if isinstance(n, ast.ClassDef)] == [
        "Permissions",
        "Roles",
    ]
