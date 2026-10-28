"""The ``tenant_rls`` database feature is advertised only when the option is on.

A module can declare ``CapabilityRequirement("database",
features=frozenset({"tenant_rls"}))``; resolving the instance then succeeds only
when that instance was configured with ``tenant_rls: true`` (the driver
installed the RLS session hooks). Building a provider creates a lazy engine — no
connection is opened.
"""

from __future__ import annotations

import pytest

import sandloom.core.infra as infra_pkg
from sandloom.core.drivers.sqlalchemy_database import (
    SQLALCHEMY_DB_FEATURES,
    SQLAlchemyDatabaseOptions,
    create_sqlalchemy_database,
    sqlalchemy_db_features,
)
from sandloom.core.infra.capabilities import DATABASE_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import (
    ExperimentalCapabilityError,
    FeatureUnsupportedError,
    InfraRegistry,
)

# Never connected to: engines are lazy and no query is issued.
_URL = "postgresql+asyncpg://u:p@localhost:5433/sandloom_rls_feature_unused"


def _cfg(name: str, **options: object) -> InstanceConfig:
    return InstanceConfig(
        capability="database",
        name=name,
        driver="sqlalchemy",
        options={"url": _URL, **options},
    )


def _registry(*cfgs: InstanceConfig) -> InfraRegistry:
    reg = InfraRegistry(
        config={"database": {c.name: c for c in cfgs}},
        strict_capabilities=True,
    )
    reg.register_capability(DATABASE_CAPABILITY)
    # Mirrors sandloom.core.drivers.bootstrap.register_builtin_drivers.
    reg.register_driver(
        "database",
        "sqlalchemy",
        create_sqlalchemy_database,
        features=SQLALCHEMY_DB_FEATURES,
        config_schema=SQLAlchemyDatabaseOptions,
        instance_features=sqlalchemy_db_features,
    )
    return reg


def test_database_capability_defines_tenant_rls() -> None:
    assert "tenant_rls" in DATABASE_CAPABILITY.features


def test_feature_set_follows_option() -> None:
    assert "tenant_rls" not in sqlalchemy_db_features({})
    assert "tenant_rls" not in sqlalchemy_db_features({"tenant_rls": False})
    assert "tenant_rls" in sqlalchemy_db_features({"tenant_rls": True})
    # Base features are unconditional either way.
    for opts in ({}, {"tenant_rls": True}):
        assert {"transactions", "savepoints", "transactional_outbox", "audit"} <= (
            sqlalchemy_db_features(opts)
        )


@pytest.mark.asyncio
async def test_provider_advertises_tenant_rls_only_when_enabled() -> None:
    reg = _registry(_cfg("plain"), _cfg("rls", tenant_rls=True))
    try:
        assert "tenant_rls" not in reg.resolve("database", "plain").features()
        assert "tenant_rls" in reg.resolve("database", "rls").features()

        ok = reg.resolve("database", "rls", features=frozenset({"tenant_rls"}))
        assert ok.name == "rls"
        with pytest.raises(FeatureUnsupportedError):
            reg.resolve("database", "plain", features=frozenset({"tenant_rls"}))
    finally:
        await reg.aclose_all()


def test_strict_registration_accepts_tenant_rls_driver_feature() -> None:
    # Would raise FeatureUnsupportedError if the spec did not define it.
    reg = _registry(_cfg("rls", tenant_rls=True))
    reg.validate_requirement("database", "rls", features=frozenset({"tenant_rls"}))


def test_experimental_capability_error_is_reexported() -> None:
    assert infra_pkg.ExperimentalCapabilityError is ExperimentalCapabilityError
    assert "ExperimentalCapabilityError" in infra_pkg.__all__


def test_startup_preflight_checks_instance_options() -> None:
    """``validate_requirement`` (the build-free startup preflight) must see the
    instance's options, not just the driver-level feature set."""
    reg = _registry(_cfg("rls", tenant_rls=True), _cfg("plain"))
    reg.validate_requirement("database", "rls", features=frozenset({"tenant_rls"}))
    with pytest.raises(FeatureUnsupportedError):
        reg.validate_requirement(
            "database", "plain", features=frozenset({"tenant_rls"})
        )
    # Base features stay satisfiable on both instances.
    reg.validate_requirement("database", "plain", features=frozenset())


def test_builtin_bootstrap_wires_instance_features() -> None:
    from unittest.mock import MagicMock

    from sandloom.core.drivers.bootstrap import register_builtin_drivers

    reg = InfraRegistry(
        config={"database": {"plain": _cfg("plain")}}, strict_capabilities=False
    )
    register_builtin_drivers(reg, MagicMock())
    with pytest.raises(FeatureUnsupportedError):
        reg.validate_requirement(
            "database", "plain", features=frozenset({"tenant_rls"})
        )
