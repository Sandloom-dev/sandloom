"""Assemble a fully configured :class:`InfraRegistry` for an application.

This is the composition root for infrastructure: it registers the built-in
drivers, loads any third-party driver plugins (``sandloom.infra`` entry points),
and applies the merged configuration. Called once from ``create_app``.
"""

from __future__ import annotations

import importlib

from sandloom.core.config.settings import Settings
from sandloom.core.config.settings import settings as _default_settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES
from sandloom.core.infra.config import InfraConfig, InstanceConfig, load_infra_config
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.registry import (
    DriverFactory,
    ExperimentalCapabilityRefused,
    InfraRegistry,
    load_selected_plugins,
)

#: The import name and the extra of the redis-py client.
_REDIS: tuple[str, ...] = ("redis",)
_REDIS_INSTALL = "pip install 'sandloom[redis]'"
_DRIVERS = "sandloom.core.drivers"


#: Every built-in driver row of this wave (spec 1, "Registry files"):
#: ``(capability, driver, "module:Class", requires, install, delivered_by)``.
#: Each is a lazy target, imported only when an instance selects it, and its
#: class is checked like an external driver's. ``delivered_by`` names the slice
#: that delivers a target this build does not have yet: selecting it is refused
#: (SANDLOOM-B022) with that name.
BUILTIN_DRIVER_ROWS: tuple[tuple[str, str, str, tuple[str, ...], str, str], ...] = (
    (
        "database",
        "sqlalchemy",
        f"{_DRIVERS}.sqlalchemy_database:SQLAlchemyDatabaseProvider",
        (),
        "",
        "",
    ),
    (
        "cache",
        "redis",
        f"{_DRIVERS}.redis_cache:RedisCacheProvider",
        _REDIS,
        _REDIS_INSTALL,
        "",
    ),
    (
        "cache",
        "memory",
        f"{_DRIVERS}.memory_cache:MemoryCacheProvider",
        (),
        "",
        "idk-cache-contract",
    ),
    (
        "objectstore",
        "local",
        f"{_DRIVERS}.local_objectstore:LocalObjectStoreProvider",
        (),
        "",
        "",
    ),
    (
        "taskqueue",
        "celery",
        f"{_DRIVERS}.celery_taskqueue:CeleryTaskQueueProvider",
        ("celery",),
        "pip install 'sandloom[celery]'",
        "",
    ),
    (
        "eventbus",
        "memory",
        f"{_DRIVERS}.event_bus_driver:MemoryEventBusProvider",
        (),
        "",
        "",
    ),
    (
        "eventbus",
        "redis",
        f"{_DRIVERS}.event_bus_driver:RedisEventBusProvider",
        _REDIS,
        _REDIS_INSTALL,
        "",
    ),
    (
        "eventbus",
        "streams",
        f"{_DRIVERS}.event_bus_driver:StreamsEventBusProvider",
        _REDIS,
        _REDIS_INSTALL,
        "",
    ),
    # The only non-relational driver of 1.0: in-memory, a projection target.
    (
        "document_store",
        "memory",
        f"{_DRIVERS}.memory_document_store:MemoryDocumentStoreProvider",
        (),
        "",
        "",
    ),
    ("notification", "log", f"{_DRIVERS}.log_notifier:LogNotifierProvider", (), "", ""),
    (
        "notification",
        "smtp",
        f"{_DRIVERS}.smtp_notifier:SmtpNotifierProvider",
        (),
        "",
        "",
    ),
    (
        "atomic_store",
        "redis",
        f"{_DRIVERS}.redis_atomic_store:RedisAtomicStoreProvider",
        _REDIS,
        _REDIS_INSTALL,
        "idk-atomic-store",
    ),
    (
        "atomic_store",
        "memory",
        f"{_DRIVERS}.memory_atomic_store:MemoryAtomicStoreProvider",
        (),
        "",
        "idk-atomic-store",
    ),
    (
        "atomic_store",
        "database",
        f"{_DRIVERS}.database_atomic_store:DatabaseAtomicStoreProvider",
        (),
        "",
        "idk-atomic-store-db",
    ),
    (
        "lease",
        "redis",
        f"{_DRIVERS}.redis_lease:RedisLeaseProvider",
        _REDIS,
        _REDIS_INSTALL,
        "idk-lease",
    ),
    (
        "lease",
        "memory",
        f"{_DRIVERS}.memory_lease:MemoryLeaseProvider",
        (),
        "",
        "idk-lease",
    ),
    (
        "lease",
        "database",
        f"{_DRIVERS}.database_lease:DatabaseLeaseProvider",
        (),
        "",
        "idk-lease",
    ),
    (
        "broadcast",
        "redis",
        f"{_DRIVERS}.redis_broadcast:RedisBroadcastProvider",
        _REDIS,
        _REDIS_INSTALL,
        "idk-broadcast",
    ),
    (
        "broadcast",
        "memory",
        f"{_DRIVERS}.memory_broadcast:MemoryBroadcastProvider",
        (),
        "",
        "idk-broadcast",
    ),
)

#: The SQLAlchemy dialect names the ``sqlalchemy`` database driver serves
#: (``sqlalchemy_database.SQLALCHEMY_SQL_DIALECTS``; a test keeps them equal).
#: Written here so the startup check (SANDLOOM-B024) imports no driver.
SQLALCHEMY_SQL_DIALECTS = frozenset({"postgresql", "sqlite"})


def _event_bus_build(
    schema_registry: SchemaRegistryContract, driver: str
) -> DriverFactory:
    """The build of an event bus driver: the bus needs the application's
    schema registry, which ``from_config`` cannot reach."""

    def build(cfg: InstanceConfig) -> InfraProvider:
        # Imported when an instance selects the driver, never at bootstrap.
        event_bus_driver = importlib.import_module(
            "sandloom.core.drivers.event_bus_driver"
        )
        factory: DriverFactory = event_bus_driver.make_event_bus_drivers(
            schema_registry
        )[driver]
        return factory(cfg)

    return build


def register_builtin_drivers(
    registry: InfraRegistry, schema_registry: SchemaRegistryContract
) -> None:
    """Register every capability spec and driver shipped with core.

    Every row of :data:`BUILTIN_DRIVER_ROWS` is a lazy ``"module:Class"``
    target: registering imports no driver module, an instance that selects a
    driver imports its class, and the class declares its features, options
    schema and contract major once (SANDLOOM-B022 on a mismatch). The EXTRA
    capabilities (document_store, graph_store, workflow, search, lock, secrets)
    are registered as known specs and ship no production driver — a plugin
    supplies one (document_store has the in-memory ``memory`` driver).
    """
    for spec in (*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES):
        registry.register_capability(spec)
    # "database" is the single canonical name for the SQL/transactional database
    # capability — used throughout the public API (DbSession, Database(name),
    # CapabilityRequirement("database"), migrations). There is no second name.
    for (
        capability,
        driver,
        target,
        requires,
        install,
        delivered_by,
    ) in BUILTIN_DRIVER_ROWS:
        registry.register_driver(
            capability,
            driver,
            target,
            # A driver whose client is an extra declares it: the preflight
            # tells a registered driver from a usable one, build-free
            # (SANDLOOM-B037).
            requires=requires,
            install=install,
            sql_dialects=SQLALCHEMY_SQL_DIALECTS if driver == "sqlalchemy" else None,
            build=(
                _event_bus_build(schema_registry, driver)
                if capability == "eventbus"
                else None
            ),
            delivered_by=delivered_by,
        )


def build_infra_registry(
    schema_registry: SchemaRegistryContract,
    *,
    settings: Settings | None = None,
    config: InfraConfig | None = None,
    load_plugins: bool = True,
) -> InfraRegistry:
    """Build the registry: drivers + plugins + configuration."""
    resolved_settings = settings or _default_settings
    cfg = config or load_infra_config(resolved_settings)
    # Strict in production: a driver cannot be registered for a capability with
    # no spec, and a built instance must report the features it declared.
    registry = InfraRegistry(strict_capabilities=True)
    # Register capabilities + drivers BEFORE the config so that set_config has
    # the specs to validate INFRA against.
    register_builtin_drivers(registry, schema_registry)
    if load_plugins:
        load_selected_plugins(registry, cfg)
    registry.set_config(cfg)
    # Fail fast on a configured instance whose driver was never registered.
    registry.validate()
    _refuse_experimental_in_production(registry, resolved_settings)
    return registry


def _refuse_experimental_in_production(
    registry: InfraRegistry, settings: Settings
) -> None:
    """An experimental contract may change in any release: running one in
    production is a weakening the deployment must accept by name (ADR-0006)."""
    if not settings.is_prod or settings.accepts_risk("experimental_capability"):
        return
    configured = sorted(
        spec.name
        for spec in registry.capabilities()
        if spec.is_experimental and registry.instances(spec.name)
    )
    if configured:
        raise ExperimentalCapabilityRefused(
            f"Experimental capabilities are configured in production: "
            f"{', '.join(configured)} (sandloom explain SANDLOOM-B023)",
            fix="remove them from INFRA, or accept the risk with "
            'SANDLOOM_ACCEPTED_RISKS=["experimental_capability"]',
        )


async def close_default_database() -> None:
    """Dispose the process-wide default database engine (driver-owned).

    Lets a composition root tear the default down at shutdown without
    importing a concrete driver itself.
    """
    driver = importlib.import_module("sandloom.core.drivers.sqlalchemy_database")
    await driver.close_default_engine()


async def close_default_cache() -> None:
    """Close the process-wide default cache client (driver-owned)."""
    driver = importlib.import_module("sandloom.core.drivers.redis_cache")
    await driver.close_default_client()


__all__ = [
    "BUILTIN_DRIVER_ROWS",
    "SQLALCHEMY_SQL_DIALECTS",
    "register_builtin_drivers",
    "build_infra_registry",
    "close_default_cache",
    "close_default_database",
]
