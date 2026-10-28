"""Migration dependency ordering: deterministic + fail-fast.

The migration plan must not depend on set iteration order, and a broken
dependency graph (cycle, or a missing dependency of a required module) must
fail the deploy rather than silently skip.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sandloom.core.migrations.migrations import (
    MigrationDependencyError,
    _order_by_dependencies,
)


def _desc(name: str, *, depends_on: list[str] | None = None, required: bool = False):
    # _order_by_dependencies only reads .depends_on and .required via getattr.
    return SimpleNamespace(name=name, depends_on=depends_on or [], required=required)


def test_order_is_deterministic_and_respects_dependencies() -> None:
    dmap = {
        "billing": _desc("billing", depends_on=["identity"]),
        "identity": _desc("identity"),
        "tenancy": _desc("tenancy", depends_on=["identity"]),
    }
    candidates = set(dmap)
    first = _order_by_dependencies(dmap, candidates)
    # identity before its dependents; ties broken lexicographically.
    assert first == ["identity", "billing", "tenancy"]
    # Stable across repeated runs regardless of set ordering.
    for _ in range(5):
        assert _order_by_dependencies(dmap, set(dmap)) == first


def test_cycle_is_fatal() -> None:
    dmap = {
        "a": _desc("a", depends_on=["b"]),
        "b": _desc("b", depends_on=["a"]),
    }
    with pytest.raises(MigrationDependencyError, match="cyclic"):
        _order_by_dependencies(dmap, set(dmap))


def test_missing_dependency_of_required_module_is_fatal() -> None:
    dmap = {"billing": _desc("billing", depends_on=["ghost"], required=True)}
    with pytest.raises(MigrationDependencyError, match="ghost"):
        _order_by_dependencies(dmap, set(dmap))


def test_missing_dependency_of_optional_module_is_dropped() -> None:
    dmap = {"reports": _desc("reports", depends_on=["ghost"], required=False)}
    # Optional module: missing edge ignored, module still ordered.
    assert _order_by_dependencies(dmap, set(dmap)) == ["reports"]
