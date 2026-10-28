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
    create_unimplemented_objectstore,
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
    InfraError,
    InfraRegistry,
    load_entrypoint_drivers,
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
    for cloud in ("s3", "gcs", "yc"):
        # Unimplemented cloud stores advertise no features (cannot satisfy any
        # requirement) but are known so config validation gives a clear error.
        # No config_schema: their option shape is the future driver's to define.
        registry.register_driver("objectstore", cloud, create_unimplemented_objectstore)
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
    cfg = config or load_infra_config(settings or _default_settings)
    # Strict in production: a driver cannot be registered for a capability with
    # no spec, so a plugin must register its CapabilitySpec first.
    registry = InfraRegistry(strict_capabilities=True)
    # Register capabilities + drivers BEFORE the config so that set_config has
    # the specs to validate INFRA against.
    register_builtin_drivers(registry, schema_registry)
    if load_plugins:
        load_entrypoint_drivers(registry)
    registry.set_config(cfg)
    # Fail fast on a configured instance whose driver was never registered.
    registry.validate()
    _reject_placeholder_drivers(registry)
    return registry


def _reject_placeholder_drivers(registry: InfraRegistry) -> None:
    """Abort startup when a configured instance uses a placeholder driver.

    ``s3`` / ``gcs`` / ``yc`` object stores are registered only so config
    validation recognises the names; core ships no implementation. Selecting
    one (``STORAGE_TYPE=s3``) used to boot fine and then fail every
    ``/ready`` probe forever. Unless a plugin replaced the placeholder (an
    ``sandloom.infra`` entry point re-registering the driver), refuse to start with
    an actionable error instead.
    """
    offenders: list[str] = []
    for name in registry.instances("objectstore"):
        cfg = registry._config["objectstore"][name]  # same package
        registration = registry._drivers.get(("objectstore", cfg.driver))
        if (
            registration is not None
            and registration.factory is create_unimplemented_objectstore
        ):
            offenders.append(f"objectstore/{name} (driver={cfg.driver!r})")
    if offenders:
        raise InfraError(
            "Object store driver not available: "
            + ", ".join(offenders)
            + ". Core implements only the 'local' object store; install a "
            "plugin that registers this driver under the 'sandloom.infra' "
            "entry-point group, or set STORAGE_TYPE=local."
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
