"""Assemble a fully configured :class:`InfraRegistry` for an application.

This is the composition root for infrastructure: it registers the built-in
drivers, loads any third-party driver plugins (``sandloom.infra`` entry points),
and applies the merged configuration. Called once from ``create_app``.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from sandloom.core.config.settings import Settings
from sandloom.core.config.settings import settings as _default_settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES
from sandloom.core.infra.config import InfraConfig, InstanceConfig, load_infra_config
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.fenced_lease import BEAT_INSTANCE
from sandloom.core.infra.ports._base import DriverTier, InfraProvider
from sandloom.core.infra.registry import (
    DevelopmentOnlyDriverRefused,
    DriverFactory,
    ExperimentalCapabilityRefused,
    ExperimentalDriverRefused,
    InfraRegistry,
    load_selected_plugins,
)

#: The import name and the extra of the redis-py client.
_REDIS: tuple[str, ...] = ("redis",)
_REDIS_INSTALL = "pip install 'sandloom[redis]'"
#: The Redis drivers of the ``sandloom-redis`` package: the same ``module:Class``
#: targets its ``sandloom.drivers`` entry points name (a test keeps them equal),
#: so a row and its entry point are one driver, never a conflict. A build
#: without the package is refused when an instance selects the driver (B037).
_REDIS_PACKAGE: tuple[str, ...] = ("sandloom_redis", "redis")
_REDIS_DRIVERS = {
    "atomic_store": "sandloom_redis.atomic_store:RedisAtomicStoreProvider",
    "lease": "sandloom_redis.lease:RedisLeaseProvider",
    "broadcast": "sandloom_redis.broadcast:RedisBroadcastProvider",
}
#: The Celery taskqueue driver of the ``sandloom-celery`` package: the same
#: ``module:Class`` target its ``taskqueue.celery`` entry point names (a test
#: keeps them equal), refused with the install hint when the package is absent.
_CELERY_PACKAGE: tuple[str, ...] = ("sandloom_celery", "celery")
_CELERY_DRIVER = "sandloom_celery.taskqueue:CeleryTaskQueueProvider"
_DRIVERS = "sandloom.core.drivers"

logger = logging.getLogger("sandloom.core.infra")


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
        "",
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
        _CELERY_DRIVER,
        _CELERY_PACKAGE,
        "pip install 'sandloom[celery]'",
        "",
    ),
    (
        "eventbus",
        "memory",
        "sandloom.core.eventing.volatile_provider:MemoryEventBusProvider",
        (),
        "",
        "",
    ),
    (
        "eventbus",
        "redis",
        "sandloom.core.eventing.volatile_provider:RedisEventBusProvider",
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
        _REDIS_DRIVERS["atomic_store"],
        _REDIS_PACKAGE,
        _REDIS_INSTALL,
        "",
    ),
    (
        "atomic_store",
        "memory",
        f"{_DRIVERS}.memory_atomic_store:MemoryAtomicStoreProvider",
        (),
        "",
        "",
    ),
    (
        "atomic_store",
        "database",
        f"{_DRIVERS}.database_atomic_store:DatabaseAtomicStoreProvider",
        (),
        "",
        "",
    ),
    (
        "lease",
        "redis",
        _REDIS_DRIVERS["lease"],
        _REDIS_PACKAGE,
        _REDIS_INSTALL,
        "",
    ),
    (
        "lease",
        "memory",
        f"{_DRIVERS}.memory_lease:MemoryLeaseProvider",
        (),
        "",
        "",
    ),
    (
        "lease",
        "database",
        f"{_DRIVERS}.database_lease:DatabaseLeaseProvider",
        (),
        "",
        "",
    ),
    (
        "broadcast",
        "redis",
        _REDIS_DRIVERS["broadcast"],
        _REDIS_PACKAGE,
        _REDIS_INSTALL,
        "",
    ),
    (
        "broadcast",
        "memory",
        f"{_DRIVERS}.memory_broadcast:MemoryBroadcastProvider",
        (),
        "",
        "",
    ),
)

#: The tier of every built-in driver (spec 2.5): ``register_driver(..., tier=)``
#: takes it from here, and a row of :data:`BUILTIN_DRIVER_ROWS` without an entry
#: fails at bootstrap. This repository's drivers are at least ``beta``: a driver
#: here is ``beta`` because its kit or conformance tests run in CI against the
#: real service, ``stable`` once it also has the kit's reconnect-after-outage
#: group and its auth options (none yet). ``smtp`` is ``beta`` since
#: ``.github/workflows/notification-smtp.yml`` (``NotifierConformance`` against a
#: mailpit container) is green on main.
BUILTIN_DRIVER_TIERS: dict[tuple[str, str], DriverTier] = {
    ("database", "sqlalchemy"): "beta",
    ("cache", "redis"): "beta",
    ("cache", "memory"): "beta",
    ("objectstore", "local"): "beta",
    ("taskqueue", "celery"): "beta",
    ("eventbus", "memory"): "beta",
    ("eventbus", "redis"): "beta",
    ("eventbus", "streams"): "beta",
    ("document_store", "memory"): "beta",
    ("notification", "log"): "beta",
    ("notification", "smtp"): "beta",
    ("atomic_store", "redis"): "beta",
    ("atomic_store", "memory"): "beta",
    ("atomic_store", "database"): "beta",
    ("lease", "redis"): "beta",
    ("lease", "memory"): "beta",
    ("lease", "database"): "beta",
    ("broadcast", "redis"): "beta",
    ("broadcast", "memory"): "beta",
}

#: The SQLAlchemy dialect names the ``sqlalchemy`` database driver serves
#: (``sqlalchemy_database.SQLALCHEMY_SQL_DIALECTS``; a test keeps them equal).
#: Written here so the startup check (SANDLOOM-B024) imports no driver.
SQLALCHEMY_SQL_DIALECTS = frozenset({"postgresql", "sqlite"})


def volatile_event_bus_allowed(settings: Settings | None = None) -> bool:
    """Whether a volatile event bus (``memory``, ``redis``) may be built: not
    in production unless the risk ``volatile_event_bus`` is accepted. The
    kernel's policy, handed to the event bus drivers (they read no setting).
    ``settings`` are the ones the registry was built with (the process
    settings when omitted)."""
    resolved = settings or _default_settings
    return not resolved.is_prod or resolved.accepts_risk("volatile_event_bus")


def _event_bus_build(
    schema_registry: SchemaRegistryContract,
    driver: str,
    settings: Settings | None = None,
) -> DriverFactory:
    """The build of an event bus driver. ``streams`` is a transport the kernel
    builds its bus over; ``memory`` and ``redis`` are the kernel's volatile
    buses, which need the application's schema registry (``from_config``
    cannot reach it) and the policy of the registry's own ``settings``."""

    def build(cfg: InstanceConfig) -> InfraProvider:
        # Imported when an instance selects the driver, never at bootstrap.
        if driver == "streams":
            event_bus_driver = importlib.import_module(
                "sandloom.core.drivers.event_bus_driver"
            )
            streams: DriverFactory = event_bus_driver.make_streams_driver()
            return streams(cfg)
        volatile = importlib.import_module("sandloom.core.eventing.volatile_provider")
        factory: DriverFactory = volatile.make_volatile_bus_drivers(
            schema_registry,
            volatile_allowed=lambda: volatile_event_bus_allowed(settings),
        )[driver]
        return factory(cfg)

    return build


def unverified_noeviction_allowed(settings: Settings | None = None) -> bool:
    """Whether ``never_evicts_check=assume`` may run: not in production unless
    the risk ``unverified_noeviction`` is accepted. The kernel's policy, handed to
    the Redis drivers whose correctness needs a server that never evicts (they
    read no setting). ``settings`` are the ones the registry was built with (the
    process settings when omitted)."""
    resolved = settings or _default_settings
    return not resolved.is_prod or resolved.accepts_risk("unverified_noeviction")


#: The drivers that take the kernel's ``never_evicts_check=assume`` policy.
_NOEVICTION_POLICY_DRIVERS = frozenset({("atomic_store", "redis"), ("lease", "redis")})


def _policy_build(
    registry: InfraRegistry,
    capability: str,
    driver: str,
    settings: Settings | None = None,
) -> DriverFactory:
    """The build of a Redis driver whose correctness needs a server that never
    evicts (``atomic_store``, ``lease``): it constructs the provider class the
    registry resolves from the driver's row with ``assume_allowed`` stating the
    kernel's policy on ``never_evicts_check=assume``
    (:func:`unverified_noeviction_allowed`)."""

    def build(cfg: InstanceConfig) -> InfraProvider:
        # The row's lazy target is imported by the registry when an instance
        # selects the driver, never at bootstrap.
        constructor: Any = registry.driver_class(capability, driver)
        provider: InfraProvider = constructor(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            assume_allowed=lambda: unverified_noeviction_allowed(settings),
        )
        return provider

    return build


def with_default_beat_lease(config: InfraConfig) -> InfraConfig:
    """``config`` with the instance ``lease.beat`` the kernel needs: when
    ``INFRA`` names none, a ``redis`` lease on ``CACHE_URL`` (the shared Redis
    of every deploy profile), marked implicit like the other defaults. It is
    built on first use, never opened or probed for a process that holds no
    lease."""
    if BEAT_INSTANCE in (config.get("lease") or {}):
        return config
    default = InstanceConfig(
        capability="lease", name=BEAT_INSTANCE, driver="redis", options={}
    )
    default.implicit = True
    config.setdefault("lease", {})[BEAT_INSTANCE] = default
    return config


def register_builtin_drivers(
    registry: InfraRegistry,
    schema_registry: SchemaRegistryContract,
    *,
    settings: Settings | None = None,
) -> None:
    """Register every capability spec and driver shipped with core.

    Every row of :data:`BUILTIN_DRIVER_ROWS` is a lazy ``"module:Class"``
    target: registering imports no driver module, an instance that selects a
    driver imports its class, and the class declares its features, options
    schema and contract major once (SANDLOOM-B022 on a mismatch). The EXTRA
    capabilities (document_store, graph_store, workflow, search, lock, secrets)
    are registered as known specs and ship no production driver — a plugin
    supplies one (document_store has the in-memory ``memory`` driver).
    ``settings`` decide the event bus drivers' volatile policy (the process
    settings when omitted).
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
                _event_bus_build(schema_registry, driver, settings)
                if capability == "eventbus"
                else _policy_build(registry, capability, driver, settings)
                if (capability, driver) in _NOEVICTION_POLICY_DRIVERS
                else None
            ),
            delivered_by=delivered_by,
            tier=BUILTIN_DRIVER_TIERS[(capability, driver)],
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
    cfg = with_default_beat_lease(config or load_infra_config(resolved_settings))
    _fill_beat_lease_url(cfg, resolved_settings)
    # Strict in production: a driver cannot be registered for a capability with
    # no spec, and a built instance must report the features it declared.
    registry = InfraRegistry(strict_capabilities=True)
    # Register capabilities + drivers BEFORE the config so that set_config has
    # the specs to validate INFRA against.
    register_builtin_drivers(registry, schema_registry, settings=resolved_settings)
    if load_plugins:
        load_selected_plugins(registry, cfg)
    registry.set_config(cfg)
    # Fail fast on a configured instance whose driver was never registered.
    registry.validate()
    _refuse_experimental_in_production(registry, resolved_settings)
    _check_driver_tiers_in_production(registry, resolved_settings)
    return registry


def _fill_beat_lease_url(config: InfraConfig, settings: Settings) -> None:
    """A ``redis`` lease without a ``url`` uses ``CACHE_URL`` (as the cache)."""
    cache_url = getattr(settings, "cache_url", None)
    for cfg in (config.get("lease") or {}).values():
        if cfg.driver == "redis" and not cfg.options.get("url") and cache_url:
            cfg.options["url"] = cache_url


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


def _is_development_only(registry: InfraRegistry, capability: str, driver: str) -> bool:
    provider_class = registry.driver_class(capability, driver)
    return provider_class is not None and bool(provider_class.development_only)


def _check_driver_tiers_in_production(
    registry: InfraRegistry, settings: Settings
) -> None:
    """A driver's tier decides what production does with it (spec 2.5): an
    ``experimental`` one is refused unless the risk ``experimental_driver`` is
    accepted (SANDLOOM-B023), a ``beta`` one logs one startup warning, a
    ``stable`` one is silent; a ``development_only`` one is refused first
    (SANDLOOM-B022), whatever its tier. Every configured instance counts, a synthesized
    ``default`` too: its driver was selected by the flat settings."""
    if not settings.is_prod:
        return
    experimental: list[str] = []
    beta: list[str] = []
    for spec in registry.capabilities():
        for name in registry.instances(spec.name):
            cfg = registry.instance_config(spec.name, name)
            if cfg is None:
                continue
            if _is_development_only(registry, spec.name, cfg.driver):
                # B022 whatever its tier (stable and untiered too), a
                # synthesized ``default`` included (``validate`` skips an
                # implicit instance's B022): never B023.
                raise DevelopmentOnlyDriverRefused(
                    f"{spec.name}/{name}: driver {cfg.driver!r} is for development "
                    "and tests only, and this is production",
                    fix=f"select a production {spec.name} driver in "
                    f"INFRA[{spec.name!r}][{name!r}]",
                )
            tier = registry.driver_tier(spec.name, cfg.driver)
            if tier is None or tier == "stable":
                continue
            row = f"{spec.name}/{name} ({cfg.driver})"
            (experimental if tier == "experimental" else beta).append(row)
    if experimental and not settings.accepts_risk("experimental_driver"):
        raise ExperimentalDriverRefused(
            "Experimental drivers are configured in production: "
            f"{', '.join(experimental)} (sandloom explain SANDLOOM-B023)",
            fix="select a beta or stable driver in INFRA, or accept the risk with "
            'SANDLOOM_ACCEPTED_RISKS=["experimental_driver"]',
        )
    if experimental or beta:
        logger.warning(
            "[infra] drivers below stable run in production: %s",
            ", ".join(
                [f"{row} experimental" for row in experimental]
                + [f"{row} beta" for row in beta]
            ),
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
    "with_default_beat_lease",
    "build_infra_registry",
    "unverified_noeviction_allowed",
    "volatile_event_bus_allowed",
    "close_default_cache",
    "close_default_database",
]
