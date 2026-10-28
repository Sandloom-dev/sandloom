"""Architecture rule: one canonical authorization subsystem.

``sandloom.shared.authz`` is the single approved access-enforcement path. To stop the
fragmentation it replaced from creeping back, this ratchet forbids:

1. New ``guards.py`` modules in business modules (authorization belongs in
   ``sandloom.shared.authz`` and per-module ``<module>/access/`` atoms, not ad-hoc
   guard files). Only the pre-existing legacy guards are allowlisted, and the
   allowlist only ever shrinks.
2. Defining a :class:`Requirement` subclass (an authorization atom) anywhere
   except ``sandloom.shared.authz`` or a module's own ``access/`` package — so a
   cross-cutting check can't be smuggled into arbitrary module code.

It also asserts the canonical package exposes the unified entry points.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"

# Legacy guard modules that predated the unified subsystem have all been
# migrated onto sandloom.shared.authz (pure role gates) or relocated into each
# module's access/ package. The allowlist is empty and may only stay empty:
# no business module may (re)introduce a guards.py.
LEGACY_GUARD_ALLOWLIST: set[str] = set()


def _rel(path: Path) -> str:
    return path.relative_to(SANDLOOM_ROOT).as_posix()


def _module_py_files() -> list[Path]:
    return [
        p
        for p in SANDLOOM_ROOT.glob("**/*.py")
        if "tests" not in p.parts and "_fixtures" not in p.parts
    ]


def test_no_new_guard_modules() -> None:
    """No business module may add a ``guards.py`` beyond the legacy allowlist."""
    offenders = [
        _rel(p)
        for p in SANDLOOM_ROOT.glob("**/guards.py")
        if "tests" not in p.parts and _rel(p) not in LEGACY_GUARD_ALLOWLIST
    ]
    if offenders:
        pytest.fail(
            "New guard module(s) found — authorization belongs in "
            "sandloom.shared.authz (shared atoms) or <module>/access/ (domain "
            "atoms), not a guards.py:\n  " + "\n  ".join(sorted(offenders)),
            pytrace=False,
        )


def _defines_requirement_subclass(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = (
                base.id
                if isinstance(base, ast.Name)
                else (base.attr if isinstance(base, ast.Attribute) else None)
            )
            if name == "Requirement":
                return True
    return False


def test_requirement_atoms_confined_to_authz_or_access() -> None:
    """``Requirement`` subclasses live only in shared/authz or <module>/access."""
    offenders: list[str] = []
    for path in _module_py_files():
        rel = _rel(path)
        if rel.startswith("shared/authz/"):
            continue
        parts = path.parts
        if "access" in parts:  # a module's own <module>/access/ package
            continue
        if _defines_requirement_subclass(path):
            offenders.append(rel)
    if offenders:
        pytest.fail(
            "Authorization atom(s) (Requirement subclasses) defined outside "
            "sandloom.shared.authz / <module>/access/:\n  "
            + "\n  ".join(sorted(offenders)),
            pytrace=False,
        )


def test_canonical_entry_points_exported() -> None:
    """The unified subsystem exposes its enforcement + result + fact surface."""
    import sandloom.shared.authz as authz

    for name in (
        "require",
        "authorize_resource",
        "enforce",
        "AccessDecision",
        "AccessDenied",
        "AccessFact",
        "Entitled",
        "OrgRole",
    ):
        assert name in authz.__all__, (
            f"{name} missing from sandloom.shared.authz.__all__"
        )
