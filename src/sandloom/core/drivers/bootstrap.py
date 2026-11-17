"""Assemble a fully configured :class:`InfraRegistry` for an application.

This is the composition root for infrastructure: it registers the built-in
drivers, loads any third-party driver plugins (``sandloom.infra`` entry points),
and applies the merged configuration. Called once from ``create_app``.
"""

from __future__ import annotations

import importlib
import logging
import sys

from sandloom.core.config.settings import Settings
from sandloom.core.config.settings import settings as _default_settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES
from sandloom.core.infra.config import (
    REQUIRED_AT_STARTUP,
    InfraConfig,
    InstanceConfig,
    fill_driver_defaults,
    load_infra_config,
    resolve_driver_aliases,
)
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.ports._base import DriverTier, InfraProvider
from sandloom.core.infra.registry import (
    DevelopmentOnlyDriverRefused,
    DriverFactory,
    ExperimentalCapabilityRefused,
    ExperimentalDriverRefused,
    InfraRegistry,
    load_selected_plugins,
)

#: The drivers of the packages the kernel knows by name, ``(capability, driver)
#: -> (import name, install command)``: the package registers each through its
#: ``sandloom.drivers`` entry point, and the kernel keeps no row for it. An
#: instance that selects one while the package is absent is refused with this
#: install hint (SANDLOOM-B037, ``InfraRegistry.register_driver_package``). A
#: test keeps the names equal to the packages' entry points.
KNOWN_DRIVER_PACKAGES: dict[tuple[str, str], tuple[str, str]] = {
    ("atomic_store", "redis"): ("sandloom_redis", "pip install sandloom-redis"),
    ("lease", "redis"): ("sandloom_redis", "pip install sandloom-redis"),
    ("broadcast", "redis"): ("sandloom_redis", "pip install sandloom-redis"),
    ("cache", "redis"): ("sandloom_redis", "pip install sandloom-redis"),
    ("eventbus", "streams"): ("sandloom_redis", "pip install sandloom-redis"),
    ("taskqueue", "celery"): ("sandloom_celery", "pip install sandloom-celery"),
}
#: The module of the ``sandloom-redis`` cache driver, whose default client
#: exists once it was imported (:func:`close_default_cache`).
_REDIS_CACHE_MODULE = "sandloom_redis.cache"
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
        "eventbus",
        "memory",
        "sandloom.core.eventing.volatile_provider:MemoryEventBusProvider",
        (),
        "",
        "",
    ),
    (
        "eventbus",
        "broadcast",
        "sandloom.core.eventing.volatile_provider:BroadcastEventBusProvider",
        (),
        "",
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
    ("cache", "memory"): "beta",
    ("objectstore", "local"): "beta",
    ("eventbus", "memory"): "beta",
    ("eventbus", "broadcast"): "beta",
    ("document_store", "memory"): "beta",
    ("notification", "log"): "beta",
    ("notification", "smtp"): "beta",
    ("atomic_store", "memory"): "beta",
    ("atomic_store", "database"): "beta",
    ("lease", "memory"): "beta",
    ("lease", "database"): "beta",
    ("broadcast", "memory"): "beta",
}

#: The SQLAlchemy dialect names the ``sqlalchemy`` database driver serves
#: (``sqlalchemy_database.SQLALCHEMY_SQL_DIALECTS``; a test keeps them equal).
#: Written here so the startup check (SANDLOOM-B024) imports no driver.
SQLALCHEMY_SQL_DIALECTS = frozenset({"postgresql", "sqlite"})


def volatile_event_bus_allowed(settings: Settings | None = None) -> bool:
    """Whether a volatile event bus (``memory``, ``broadcast``) may be built: not
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
    """The build of a volatile event bus driver: ``memory`` and ``broadcast`` are the
    kernel's volatile buses, which need the application's schema registry
    (``from_config`` cannot reach it) and the policy of the registry's own
    ``settings``. ``streams`` has no build here: it is a transport of the
    ``sandloom-redis`` package, which the kernel builds its bus over, and its
    row's class builds itself (``from_config``)."""

    def build(cfg: InstanceConfig) -> InfraProvider:
        # Imported when an instance selects the driver, never at bootstrap.
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


def register_kernel_policies(
    registry: InfraRegistry, settings: Settings | None = None
) -> None:
    """Hand the kernel's policy to the drivers that take it, whatever builds
    them (an entry point or a row): the ``atomic_store`` and ``lease`` drivers
    whose correctness needs a server that never evicts get ``assume_allowed``
    (:func:`unverified_noeviction_allowed`), so ``never_evicts_check=assume``
    runs where the policy allows it (spec 13 item 45)."""
    for capability in ("atomic_store", "lease"):
        registry.set_policy(
            capability,
            assume_allowed=lambda: unverified_noeviction_allowed(settings),
        )


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
                else None
            ),
            delivered_by=delivered_by,
            tier=BUILTIN_DRIVER_TIERS[(capability, driver)],
        )
    register_kernel_policies(registry, settings)
    for (capability, driver), (package, install) in KNOWN_DRIVER_PACKAGES.items():
        registry.register_driver_package(
            capability, driver, package=package, install=install
        )


def build_infra_registry(
    schema_registry: SchemaRegistryContract,
    *,
    settings: Settings | None = None,
    config: InfraConfig | None = None,
    load_plugins: bool = True,
    fill_from_settings: bool = False,
) -> InfraRegistry:
    """Build the registry: drivers + plugins + configuration.

    Built from the settings (``config`` omitted), the process refuses to start
    without a driver for each instance of ``REQUIRED_AT_STARTUP`` that the
    composition or ``INFRA`` selects (SANDLOOM-B037): the kernel names none and
    never falls back to ``memory``. An explicit ``config`` (a tool process, a
    test) is built as given.

    The options a driver takes from the flat settings (the readiness probe's
    bound, ``HEALTH_CHECK_TIMEOUT_SECONDS``) are filled once the drivers are loaded,
    as each driver's class declares (``InfraProvider.settings_fill``): for a
    configuration read from the settings, and for an explicit ``config`` only
    when ``fill_from_settings`` asks (a tool process, which builds its
    configuration from the settings itself)."""
    resolved_settings = settings or _default_settings
    cfg = config if config is not None else load_infra_config(resolved_settings)
    resolve_driver_aliases(cfg)
    # Strict in production: a driver cannot be registered for a capability with
    # no spec, and a built instance must report the features it declared.
    registry = InfraRegistry(strict_capabilities=True)
    # Register capabilities + drivers BEFORE the config so that set_config has
    # the specs to validate INFRA against.
    register_builtin_drivers(registry, schema_registry, settings=resolved_settings)
    if load_plugins:
        load_selected_plugins(registry, cfg)
    if config is None or fill_from_settings:
        fill_driver_defaults(cfg, resolved_settings, registry.settings_fill)
    registry.set_config(cfg)
    # Fail fast on a configured instance whose driver was never registered.
    registry.validate()
    if config is None:
        registry.require_instances(REQUIRED_AT_STARTUP)
    _refuse_experimental_in_production(registry, resolved_settings)
    _check_driver_tiers_in_production(registry, resolved_settings)
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
    """Close the process-wide default cache client (driver-owned).

    The client exists only once the ``sandloom_redis`` package's cache module
    was imported, so a build without the package has nothing to close.
    """
    driver = sys.modules.get(_REDIS_CACHE_MODULE)
    if driver is not None:
        await driver.close_default_client()


__all__ = [
    "BUILTIN_DRIVER_ROWS",
    "SQLALCHEMY_SQL_DIALECTS",
    "KNOWN_DRIVER_PACKAGES",
    "register_builtin_drivers",
    "register_kernel_policies",
    "build_infra_registry",
    "unverified_noeviction_allowed",
    "volatile_event_bus_allowed",
    "close_default_cache",
    "close_default_database",
]
