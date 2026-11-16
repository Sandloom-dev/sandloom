"""Production requires ``shared`` and ``never_evicts`` of the ``security`` and
``idempotency`` atomic_store instances, on every profile (spec section 4).

The kernel judges the features the driver declares for the instance's options,
never its name: a database store is ``shared`` on PostgreSQL and not on a SQLite
file, whatever profile is active.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.config.common import INVALID_CONFIGURATION
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.drivers.memory_atomic_store import MemoryAtomicStoreProvider
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.production_profile import (
    REQUIRED_STORE_FEATURES,
    atomic_store_feature_problems,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tests._root_support import lean_root_infra
from sandloom.core.tests.test_production_profile import _BASE as _STRICT_BASE

_POSTGRES = "postgresql+asyncpg://u:p@db-host:5432/security"
_SQLITE = "sqlite+aiosqlite:////var/lib/app/security.db"


def _registry(**stores: tuple[str, dict[str, Any]]) -> InfraRegistry:
    """A registry with the given atomic_store instances (role -> driver, options)."""
    config = {
        "atomic_store": {
            role: InstanceConfig("atomic_store", role, driver, options)
            for role, (driver, options) in stores.items()
        }
    }
    return build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=False
    )


def test_the_required_features_are_shared_and_never_evicts() -> None:
    assert set(REQUIRED_STORE_FEATURES) == {"shared", "never_evicts"}


def test_a_store_that_is_shared_and_never_evicts_passes() -> None:
    registry = _registry(
        security=("database", {"url": _POSTGRES}),
        idempotency=("database", {"url": _POSTGRES}),
    )
    assert atomic_store_feature_problems(registry) == []


@pytest.mark.parametrize("role", ["security", "idempotency"])
def test_a_store_that_is_not_shared_is_refused_by_the_features_it_declares(
    role: str,
) -> None:
    other = "idempotency" if role == "security" else "security"
    registry = _registry(
        **{
            role: ("database", {"url": _SQLITE}),
            other: ("database", {"url": _POSTGRES}),
        }
    )
    (problem,) = atomic_store_feature_problems(registry)
    assert f'INFRA["atomic_store"]["{role}"]' in problem
    assert "['shared']" in problem  # never_evicts is declared, shared is not
    assert "never_evicts" not in problem.split("does not declare")[1].split(":")[0]


class _Vendor(MemoryAtomicStoreProvider):
    """A third-party store that declares no feature at all."""

    supported_features = frozenset[str]()


def test_the_memory_driver_is_not_shared() -> None:
    (problem,) = atomic_store_feature_problems(_registry(security=("memory", {})))
    assert "'memory' does not declare ['shared']" in problem


def test_a_third_party_driver_declaring_neither_feature_is_refused_for_both() -> None:
    """The kernel names no driver: a registered one is judged by what it
    declares."""
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config={}, load_plugins=False
    )
    registry.register_driver("atomic_store", "vendor", _Vendor)
    registry.set_config(
        {
            "atomic_store": {
                "security": InstanceConfig("atomic_store", "security", "vendor")
            }
        }
    )
    (problem,) = atomic_store_feature_problems(registry)
    assert "'vendor' does not declare ['shared', 'never_evicts']" in problem


def test_an_unconfigured_idempotency_role_is_not_a_finding() -> None:
    registry = _registry(security=("database", {"url": _POSTGRES}))
    assert atomic_store_feature_problems(registry) == []


def test_a_driver_whose_features_cannot_be_read_is_refused_not_skipped() -> None:
    """``declared_features`` answers ``None`` for an unregistered driver: that is
    a refusal, never a pass."""
    infra: Any = SimpleNamespace(
        instance_config=lambda capability, role: SimpleNamespace(driver="vanished"),
        declared_features=lambda capability, role: None,
    )
    problems = atomic_store_feature_problems(infra)
    assert len(problems) == 2  # security and idempotency, both configured
    assert all("'vanished' is not registered" in p for p in problems)


def _boot(tmp_path: Path, profile: str, security: dict[str, Any]) -> str:
    """The last stderr line of ``import sandloom.main`` in a prod process whose
    security store is ``security``."""
    values: dict[str, Any] = {
        **_STRICT_BASE,
        "PRODUCTION_PROFILE": profile,
        "INFRA": {
            "database": {"default": {"tenant_rls": True}},
            "atomic_store": {"security": security},
        },
    }
    env = {
        key: value if isinstance(value, str) else json.dumps(value)
        for key, value in values.items()
    }
    env["INFRA"] = lean_root_infra(values)
    proc = subprocess.run(
        [sys.executable, "-c", "import sandloom.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.stderr.strip().splitlines()[-1] if proc.returncode else ""


@pytest.mark.slow
@pytest.mark.subprocess_probe
@pytest.mark.parametrize("profile", ["standard", "multi_tenant_strict"])
def test_boot_refuses_a_security_store_that_is_not_shared(
    profile: str, tmp_path: Path
) -> None:
    last = _boot(tmp_path, profile, {"driver": "database", "url": _SQLITE})
    assert last.startswith(INVALID_CONFIGURATION), last
    assert 'INFRA["atomic_store"]["security"]' in last
    assert "['shared']" in last
