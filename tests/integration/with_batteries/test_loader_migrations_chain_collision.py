"""Loader dependency handling + migration targeting/locking/CLI resolution.

Moved from ``sandloom/core/tests/test_loader_migrations_hardening.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import pytest

from sandloom.core.descriptors import (
    ModuleDescriptor,
    ModuleLoadSummary,
)
from sandloom.core.loader import plugin_loader
from sandloom.core.migrations.migrations import (
    MigrationDependencyError,
    check_chain_collisions,
    version_table_for,
)


def test_chain_collision_detected() -> None:
    a = ModuleDescriptor(name="sandloom_billing", version="1", migrations_path="/x")
    b = ModuleDescriptor(
        name="myapp.modules.billing", version="1", migrations_path="/y"
    )
    with pytest.raises(MigrationDependencyError, match="alembic_version_billing"):
        check_chain_collisions([a, b])
    b.migrations_chain = "myapp_billing"
    check_chain_collisions([a, b])
    assert version_table_for(a) == "alembic_version_billing"  # back-compat
    assert version_table_for(b) == "alembic_version_myapp_billing"


def test_loader_rejects_colliding_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = ModuleLoadSummary()
    summary.loaded.append(
        ModuleDescriptor(name="sandloom_billing", version="1", migrations_path="/x")
    )
    desc = ModuleDescriptor(
        name="myapp.modules.billing", version="1", migrations_path="/y"
    )
    with pytest.raises(plugin_loader.ModuleRegistrationError, match="migration"):
        plugin_loader._check_chain_collision(desc, summary)
