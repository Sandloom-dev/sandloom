"""Assemble a fully configured :class:`InfraRegistry` for an application.

This is the composition root for infrastructure: it registers the built-in
drivers, loads any third-party driver plugins (``sandloom.infra`` entry points),
and applies the merged configuration. Called once from ``create_app``.
"""

from __future__ import annotations

import sandloom.core.drivers.redis_cache as _redis_cache
import sandloom.core.drivers.sqlalchemy_database as _sqlalchemy_database
from sandloom.core.config.settings import Settings
from sandloom.core.config.settings import settings as _default_settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.drivers.celery_taskqueue import (
    CELERY_TASKQUEUE_FEATURES,
    CeleryTaskQueueOptions,
    create_celery_taskqueue,
)
from sandloom.core.drivers.event_bus_driver import (
    EVENTBUS_DRIVER_FEATURES,
    EventBusOptions,
    make_event_bus_drivers,
)
from sandloom.core.drivers.local_objectstore import (
    OBJECTSTORE_FEATURES,
    LocalObjectStoreOptions,
    create_local_objectstore,
)
from sandloom.core.drivers.log_notifier import (
    create_log_notifier,
    log_notifier_features,
)
from sandloom.core.drivers.memory_document_store import (
    MEMORY_DOCUMENT_STORE_FEATURES,
    create_memory_document_store,
)
from sandloom.core.drivers.redis_cache import (
    REDIS_CACHE_FEATURES,
    RedisCacheOptions,
    create_redis_cache,
)
from sandloom.core.drivers.smtp_notifier import (
    NOTIFIER_SMTP_FEATURES,
    NotifierOptions,
    SmtpNotifierOptions,
    create_smtp_notifier,
)
from sandloom.core.drivers.sqlalchemy_database import (
    SQLALCHEMY_DB_FEATURES,
    SQLAlchemyDatabaseOptions,
    create_sqlalchemy_database,
    sqlalchemy_db_features,
)
from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES
from sandloom.core.infra.config import InfraConfig, load_infra_config
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.registry import (
    ExperimentalCapabilityRefused,
    InfraRegistry,
    load_selected_plugins,
)


def register_builtin_drivers(
    registry: InfraRegistry, schema_registry: SchemaRegistryContract
) -> None:
    """Register every capability spec and driver shipped with core.

    The built-in five come with drivers; the EXTRA capabilities (document_store,
    graph_store, workflow, search, lock, secrets) are registered as known specs
    and ship no production driver — a plugin supplies one (document_store has
    the in-memory ``memory`` driver). This makes them discoverable and
    gives precise diagnostics, while keeping them unusable until configured.
    """
    for spec in (*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES):
        registry.register_capability(spec)
    # "database" is the single canonical name for the SQL/transactional database
    # capability — used throughout the public API (DbSession, Database(name),
    # CapabilityRequirement("database"), migrations). There is no second name.
    registry.register_driver(
        "database",
        "sqlalchemy",
        create_sqlalchemy_database,
        features=SQLALCHEMY_DB_FEATURES,
        config_schema=SQLAlchemyDatabaseOptions,
        instance_features=sqlalchemy_db_features,
    )
    registry.register_driver(
        "cache",
        "redis",
        create_redis_cache,
        features=REDIS_CACHE_FEATURES,
        config_schema=RedisCacheOptions,
    )
    registry.register_driver(
        "objectstore",
        "local",
        create_local_objectstore,
        features=OBJECTSTORE_FEATURES,
        config_schema=LocalObjectStoreOptions,
    )
    registry.register_driver(
        "taskqueue",
        "celery",
        create_celery_taskqueue,
        features=CELERY_TASKQUEUE_FEATURES,
        config_schema=CeleryTaskQueueOptions,
    )
    for driver_name, factory in make_event_bus_drivers(schema_registry).items():
        registry.register_driver(
            "eventbus",
            driver_name,
            factory,
            features=EVENTBUS_DRIVER_FEATURES.get(driver_name),
            config_schema=EventBusOptions,
        )
    # The only non-relational driver of 1.0: in-memory, a projection target.
    registry.register_driver(
        "document_store",
        "memory",
        create_memory_document_store,
        features=MEMORY_DOCUMENT_STORE_FEATURES,
    )
    registry.register_driver(
        "notification",
        "log",
        create_log_notifier,
        features=log_notifier_features(),
        config_schema=NotifierOptions,
    )
    registry.register_driver(
        "notification",
        "smtp",
        create_smtp_notifier,
        features=NOTIFIER_SMTP_FEATURES,
        config_schema=SmtpNotifierOptions,
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

    await _sqlalchemy_database.close_default_engine()


async def close_default_cache() -> None:
    """Close the process-wide default cache client (driver-owned)."""

    await _redis_cache.close_default_client()


__all__ = [
    "register_builtin_drivers",
    "build_infra_registry",
    "close_default_cache",
    "close_default_database",
]
