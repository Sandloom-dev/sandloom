"""Configuration model for infrastructure instances.

Two layers, merged at startup:

1. **Defaults**: the drivers the composition selects
   (``create_app(default_infra=...)``; the kernel names none of its own) with
   the options of the flat settings (``DATABASE_URL``, ``CACHE_URL``,
   ``BROKER_URL``, ``STORAGE_*``, ``EVENT_BUS_BACKEND``): one ``default``
   instance per capability the composition selects. A capability nothing
   selects has no instance, and startup refuses it when the app needs it
   (SANDLOOM-B037).

2. The optional ``INFRA`` setting (JSON) which declares additional named
   instances or overrides driver/options of existing ones. This is how an
   operator adds a second database, a separate analytics cache, an alternate
   queue, etc. — purely by configuration, no code change.

``INFRA`` shape::

    {
      "database": {
        "analytics": {"driver": "sqlalchemy",
                      "url": "postgresql+asyncpg://.../analytics"}
      },
      "cache":   {"sessions": {"driver": "redis", "url": "redis://.../3"}},
      "objectstore": {"exports": {"driver": "local", "path": "./exports"}}
    }

Within an instance object, ``driver`` selects the driver; every other key is
passed verbatim to that driver as an option.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import sandloom.core.config.settings as _settings_module
from sandloom.core.config.common import Environment
from sandloom.core.config.eventing import DEFAULT_EVENT_STREAM_RETENTION_SECONDS
from sandloom.core.config.settings import Settings

#: Capabilities whose ``default`` instance a composition selects (options from
#: the flat settings: DATABASE_URL, CACHE_URL, …). This is NOT the set of valid
#: capabilities — INFRA may declare instances for any capability a plugin
#: registers (workflow, graph_store, …). Whether a capability is valid is the
#: registry's job (it owns the specs); config.py only validates SHAPE.
DEFAULTED_CAPABILITIES: tuple[str, ...] = (
    "database",
    "cache",
    "broadcast",
    "objectstore",
    "taskqueue",
    "eventbus",
    "notification",
)


DEFAULT_INSTANCE = "default"

#: The synthesized instances the kernel itself uses in every composition, so
#: they count as required whether or not a module names them: the database
#: (outbox, audit, units), the cache (token revocation, user blocks, rate
#: limits, idempotency, the beat lease) and the event bus (the outbox relay).
#: Any other synthesized instance is opened, probed and listed as usable only
#: when a module's requirement names it (``InfraRegistry.in_use``).
KERNEL_INSTANCES: frozenset[tuple[str, str]] = frozenset(
    {
        ("database", DEFAULT_INSTANCE),
        ("cache", DEFAULT_INSTANCE),
        ("eventbus", DEFAULT_INSTANCE),
    }
)

#: Every instance a process refuses to start without (SANDLOOM-B037): the
#: kernel's three, and the store of revocation marks. Nothing selects them but
#: the composition or ``INFRA``: the kernel has no driver of its own to fall
#: back to, and never ``memory``.
REQUIRED_AT_STARTUP: frozenset[tuple[str, str]] = KERNEL_INSTANCES | {
    ("atomic_store", "security")
}


@dataclass(slots=True)
class InstanceConfig:
    """One configured infrastructure instance.

    ``implicit`` marks a ``default`` instance synthesized from the flat
    settings that ``INFRA`` does not name: nobody selected it, so nothing
    opens or probes it unless the kernel (:data:`KERNEL_INSTANCES`) or a
    module's requirement uses it. An instance ``INFRA`` names is explicit.
    """

    capability: str
    name: str
    driver: str
    options: dict[str, Any] = field(default_factory=dict)
    implicit: bool = False


# capability -> {instance_name -> InstanceConfig}
InfraConfig = dict[str, dict[str, InstanceConfig]]


def _is_prod(settings: Any) -> bool:
    environment = getattr(settings, "environment", None)
    return (
        environment == Environment.prod
        if environment is not None
        else bool(getattr(settings, "is_prod", False))
    )


#: ``{capability: {instance: {"driver": ..., <options>}}}``: what a composition
#: selects when ``INFRA`` names nothing (``create_app(default_infra=...)``).
DefaultInfra = Mapping[str, Mapping[str, Mapping[str, Any]]]

#: The composition's selection of this process. The kernel names no driver of
#: its own for database, cache, event bus, task queue, broadcast, lease or
#: atomic_store: a composition (``sandloom_standard``) supplies them, and one
#: that supplies none leaves startup to refuse with SANDLOOM-B037.
_COMPOSITION_DEFAULTS: InfraConfig = {}

#: Whether this process has its composition's selection (set by
#: :func:`use_default_infra`). Until then a reader adopts the root's own.
_COMPOSITION_ADOPTED = False


def use_default_infra(defaults: DefaultInfra | None) -> None:
    """Set the composition's driver selection for this process (``None``: none).

    Shape-checked like ``INFRA`` (:func:`_overlay`); every instance it names is
    a synthesized ``default``: ``INFRA`` overrides it, and nothing opens it
    unless the kernel or a module's requirement uses it.
    """
    global _COMPOSITION_ADOPTED
    checked: InfraConfig = {}
    _overlay(checked, defaults or {})
    _COMPOSITION_DEFAULTS.clear()
    _COMPOSITION_DEFAULTS.update(checked)
    _COMPOSITION_ADOPTED = True


def _adopt_root_selection() -> None:
    """A process that boots no app (``sandloom migrate``, ``worker check``, the
    backup tools) never ran ``create_app(default_infra=...)``: take the
    selection the composition root's package exports (``DEFAULT_INFRA``)."""
    # Reached by name: ``composition`` sits above the infra layer (ADR-0007).
    composition = importlib.import_module("sandloom.core.composition")
    use_default_infra(composition.root_default_infra())


def _composition_instances() -> InfraConfig:
    """A copy of the composition's selection, safe to change."""
    if not _COMPOSITION_ADOPTED:
        _adopt_root_selection()
    return {
        capability: {
            name: InstanceConfig(
                capability=cfg.capability,
                name=cfg.name,
                driver=cfg.driver,
                options=dict(cfg.options),
            )
            for name, cfg in instances.items()
        }
        for capability, instances in _COMPOSITION_DEFAULTS.items()
    }


def _select(
    config: InfraConfig,
    capability: str,
    driver: str,
    options: Mapping[str, Any],
) -> None:
    """The ``default`` instance of ``capability`` with ``driver``: the options of
    a composition default that selects the same driver are kept (flat settings
    below them), another driver starts from the flat options alone."""
    existing = config.get(capability, {}).get(DEFAULT_INSTANCE)
    merged = dict(options)
    if existing is not None and existing.driver == driver:
        merged.update(existing.options)
    config.setdefault(capability, {})[DEFAULT_INSTANCE] = InstanceConfig(
        capability=capability, name=DEFAULT_INSTANCE, driver=driver, options=merged
    )


def _flat_options(
    config: InfraConfig, capability: str, flat: Mapping[str, Any]
) -> None:
    """Under the options of the composition's ``default`` instance of
    ``capability`` (when it selects one), the options the flat settings give."""
    existing = config.get(capability, {}).get(DEFAULT_INSTANCE)
    if existing is not None:
        existing.options = {**flat, **existing.options}


def _eventbus_selector(settings: Any) -> str | None:
    """The driver ``EVENT_BUS_BACKEND`` names, ``None`` for ``auto`` (the
    composition's ``eventbus.default`` decides: no kernel default, in any
    environment)."""
    driver = str(getattr(settings, "event_bus_backend", "auto") or "auto")
    return None if driver == "auto" else driver


def _synthesize_defaults(settings: Settings) -> InfraConfig:
    """Build the implicit ``default`` instance for each capability.

    The composition's selection (:func:`use_default_infra`) is the base, the
    flat settings give its options, ``STORAGE_TYPE``, ``NOTIFIER_BACKEND`` and
    an explicit ``EVENT_BUS_BACKEND`` select their drivers. Every instance
    built here is marked ``implicit``; :func:`_overlay` makes it explicit when
    ``INFRA`` names it. A capability nothing selects has no instance.
    """
    objectstore_options: dict[str, Any] = {
        "max_upload_size_bytes": settings.max_upload_size_bytes,
    }
    if settings.storage_type == "s3":
        objectstore_options.update(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    else:
        objectstore_options["path"] = settings.storage_path

    synthesized = _composition_instances()
    _flat_options(synthesized, "database", {"url": settings.database_url})
    _flat_options(
        synthesized,
        "taskqueue",
        {"broker": settings.broker_url, "backend": settings.cache_url},
    )
    # The in-tree objectstore and notifier drivers are selected by their own
    # flat settings (``local`` and ``log`` by default).
    _select(synthesized, "objectstore", settings.storage_type, objectstore_options)
    _select(
        synthesized,
        "notification",
        settings.notifier_backend,
        {
            "host": settings.smtp_host,
            "port": settings.smtp_port,
            "username": settings.smtp_username,
            "password": settings.smtp_password,
            "from_addr": settings.smtp_from,
            "use_tls": settings.smtp_use_tls,
        },
    )
    selected_bus = _eventbus_selector(settings)
    if selected_bus is not None:
        _select(synthesized, "eventbus", selected_bus, {})
    if _is_prod(settings):
        # Never in production: an absent INFRA.atomic_store.security is a startup
        # refusal (production_profile.config_problems), the store of revocation
        # marks is never built from a composition's development default.
        synthesized.get("atomic_store", {}).pop("security", None)
        if not synthesized.get("atomic_store", True):
            del synthesized["atomic_store"]
    for instances in synthesized.values():
        for cfg in instances.values():
            cfg.implicit = True
    return synthesized


class InfraConfigError(ValueError):
    """Raised when the INFRA declaration is structurally invalid."""


def _overlay(base: InfraConfig, raw: Mapping[str, Any]) -> None:
    """Merge the raw ``INFRA`` mapping into ``base`` in place.

    Validates SHAPE only — that the structure is
    ``{capability: {instance: {"driver": ..., ...}}}`` with a driver per
    instance. It does NOT judge whether a capability name is valid: a plugin may
    register ``workflow`` / ``graph_store`` / any capability, and the registry
    (which owns the specs) is the single authority that accepts or rejects it.
    """
    for capability, instances in raw.items():
        if not isinstance(instances, Mapping):
            raise InfraConfigError(
                f"INFRA[{capability!r}] must be an object of named instances"
            )
        bucket = base.setdefault(capability, {})
        for inst_name, spec in instances.items():
            if not isinstance(spec, Mapping):
                raise InfraConfigError(
                    f"INFRA[{capability!r}][{inst_name!r}] must be an object"
                )
            existing = bucket.get(inst_name)
            options = {k: v for k, v in spec.items() if k != "driver"}
            driver = spec.get("driver") or (existing.driver if existing else None)
            if not driver:
                raise InfraConfigError(
                    f"INFRA[{capability!r}][{inst_name!r}] needs a 'driver'"
                )
            if existing is not None and existing.driver == driver:
                # Override: keep prior options not re-specified, layer new ones.
                # A different driver starts empty: the prior options are another
                # driver's, and they can hold its secrets (SMTP_PASSWORD).
                merged = dict(existing.options)
                merged.update(options)
                options = merged
            bucket[inst_name] = InstanceConfig(
                capability=capability,
                name=inst_name,
                driver=str(driver),
                options=options,
            )


def database_instance_url(options: Any, settings: Any) -> str | None:
    """The URL of a database instance: its ``url`` option, else the
    process's ``DATABASE_URL``; ``None`` without either."""
    url = (options.get("url") if options else None) or getattr(
        settings, "database_url", None
    )
    return str(url) if url else None


def eventbus_driver(settings: Any, instance: str = DEFAULT_INSTANCE) -> str | None:
    """The driver the eventbus ``instance`` resolves to, ``INFRA`` applied.

    The same merge :func:`load_infra_config` makes (``EVENT_BUS_BACKEND``
    with ``auto`` resolved, then the ``INFRA`` overlay), limited to the
    eventbus so it reads nothing else and runs before the registry exists.
    ``None`` when ``instance`` is not configured. A malformed ``INFRA``
    raises :class:`InfraConfigError`, as loading the whole config would.
    """
    found = _resolved_eventbus(settings).get(instance)
    return found.driver if found is not None else None


def _resolved_eventbus(settings: Any) -> dict[str, InstanceConfig]:
    """The eventbus instances: ``EVENT_BUS_BACKEND`` resolved, ``INFRA`` applied
    (options not yet filled from the settings)."""
    config: InfraConfig = {
        "eventbus": _composition_instances().get("eventbus", {}),
    }
    selected = _eventbus_selector(settings)
    if selected is not None:
        _select(config, "eventbus", selected, {})
    raw = getattr(settings, "infra", None)
    if raw:
        if not isinstance(raw, Mapping):
            raise InfraConfigError("INFRA must be a JSON object")
        if "eventbus" in raw:
            _overlay(config, {"eventbus": raw["eventbus"]})
    _fill_eventbus_defaults(config, settings, getattr(settings, "cache_url", None))
    return config["eventbus"]


#: The kernel's volatile event buses: no transport, so no stream retention.
_VOLATILE_EVENTBUS_DRIVERS: frozenset[str] = frozenset({"memory", "redis"})


def configured_stream_retention_seconds(
    settings: Any, instance: str = DEFAULT_INSTANCE
) -> int | None:
    """The age bound, in seconds, the eventbus ``instance`` is CONFIGURED with
    (``0``: entries are never trimmed by age): the ``retention_seconds`` option
    of a transport-backed driver, as the kernel fills it from
    ``EVENT_STREAM_RETENTION_SECONDS``.

    A pure configuration read: no bus is built and no process's live bus is
    asked, so a tool running in another process (a CLI with a volatile bus of
    its own) learns the retention of the deployment's bus. ``None`` when it
    cannot be determined: the instance is not configured, its driver is a
    volatile one (``memory``, ``redis``) or its options carry no
    ``retention_seconds``.
    """
    found = _resolved_eventbus(settings).get(instance)
    if found is None or found.driver in _VOLATILE_EVENTBUS_DRIVERS:
        return None
    value = found.options.get("retention_seconds")
    if value is None and found.driver == "streams":
        value = DEFAULT_EVENT_STREAM_RETENTION_SECONDS
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


#: ``option -> setting``: the bounds of a ``redis`` cache client, filled from
#: the ``CACHE_*`` settings when an instance leaves them out.
_REDIS_CLIENT_SETTINGS: dict[str, str] = {
    "socket_timeout_seconds": "cache_socket_timeout_seconds",
    "socket_connect_timeout_seconds": "cache_socket_connect_timeout_seconds",
    "health_check_interval_seconds": "cache_health_check_interval_seconds",
    "max_connections": "cache_max_connections",
    "pool_timeout_seconds": "cache_pool_timeout_seconds",
    "retry_attempts": "cache_retry_attempts",
}

#: ``option -> setting``: what a ``celery`` task queue's broker probe is
#: bounded by.
_CELERY_SETTINGS: dict[str, str] = {
    "health_timeout_seconds": "health_check_timeout_seconds",
}

#: ``option -> setting``: the bounds of a ``redis`` / ``streams`` event bus
#: client (its read timeout is the bus's own).
_TRANSPORT_CLIENT_SETTINGS: dict[str, str] = {
    "socket_connect_timeout_seconds": "cache_socket_connect_timeout_seconds",
    "health_check_interval_seconds": "cache_health_check_interval_seconds",
}

#: ``option -> setting``: the retention and key namespace of a ``streams`` event
#: bus's streams (until ``idk-config-defaults`` turns the settings into options).
_STREAMS_RETENTION_SETTINGS: dict[str, str] = {
    "retention_seconds": "event_stream_retention_seconds",
    "max_length": "event_stream_maxlen",
    "prefix": "event_stream_prefix",
}


def _fill(options: dict[str, Any], settings: Any, table: Mapping[str, str]) -> None:
    for option, setting in table.items():
        value = getattr(settings, setting, None)
        if value is not None:
            options.setdefault(option, value)


def _cap_probe_bound(options: dict[str, Any], settings: Any) -> None:
    """A ``celery`` probe bound above ``HEALTH_CHECK_TIMEOUT_SECONDS`` is cut to
    it: the readiness probe stops waiting there (``InfraRegistry.health_all``),
    so a longer connect would only keep a thread behind an answered probe. A
    value that is not a number stays for the driver to refuse."""
    bound = getattr(settings, "health_check_timeout_seconds", None)
    stated = options.get("health_timeout_seconds")
    if (
        isinstance(bound, int | float)
        and isinstance(stated, int | float)
        and not isinstance(stated, bool)
        and stated > bound
    ):
        options["health_timeout_seconds"] = bound


def _fill_url(options: dict[str, Any], key: str, value: Any) -> None:
    if not options.get(key) and value:
        options[key] = value


def _fill_atomic_store_defaults(
    config: InfraConfig, settings: Any, default_url: Any
) -> None:
    """The ``redis`` atomic_store instances get the client bounds only: the url
    of an instance INFRA declares is its own (never the cache's); the
    synthesized development one follows the resolved cache.default."""
    for cfg in (config.get("atomic_store") or {}).values():
        if cfg.driver == "redis":
            if cfg.implicit:
                _fill_url(cfg.options, "url", default_url)
            _fill(cfg.options, settings, _REDIS_CLIENT_SETTINGS)


def _fill_eventbus_defaults(config: InfraConfig, settings: Any, cache_url: Any) -> None:
    for cfg in (config.get("eventbus") or {}).values():
        if cfg.driver in ("redis", "streams"):
            # The volatile ``redis`` bus has no url: it runs over a broadcast
            # instance (option ``broadcast``).
            if cfg.name == DEFAULT_INSTANCE and cfg.driver == "streams":
                _fill_url(cfg.options, "url", cache_url)
            _fill(cfg.options, settings, _TRANSPORT_CLIENT_SETTINGS)
            if cfg.driver == "streams":
                _fill(cfg.options, settings, _STREAMS_RETENTION_SETTINGS)


def fill_streams_override_defaults(options: dict[str, Any]) -> None:
    """Fill the key namespace (``prefix``) of a ``streams`` event-bus override.

    The test seam ``override_instance`` configures an instance without the
    kernel's fill from the flat settings; this makes the one fill that keeps a
    streams override namespaced like the instance it replaces
    (``EVENT_STREAM_PREFIX``). An option the override sets is left alone.
    """
    _fill(
        options,
        _settings_module.settings,
        {"prefix": _STREAMS_RETENTION_SETTINGS["prefix"]},
    )


def fill_driver_defaults(config: InfraConfig, settings: Any) -> None:
    """Fill the options the built-in drivers read from the flat settings.

    A driver reads no kernel setting (ADR-0016): what it used to take from
    ``CACHE_URL``, ``BROKER_URL`` and the ``CACHE_*`` client bounds, the
    kernel puts into its instance's options when the configuration leaves
    them out, so the drivers behave as before. ``redis`` caches get ``url``
    and the client bounds; ``redis`` / ``streams`` event buses the connect
    timeout and health-check interval, and the ``default`` one ``url`` too
    (a named bus without one shares the default transport); ``streams`` buses
    their ``retention_seconds``, ``max_length`` and ``prefix``; ``celery`` task
    queues ``broker``, ``backend`` and the probe bound ``health_timeout_seconds``.
    """
    cache_url = getattr(settings, "cache_url", None)
    for cfg in (config.get("cache") or {}).values():
        if cfg.driver == "redis":
            _fill_url(cfg.options, "url", cache_url)
            _fill(cfg.options, settings, _REDIS_CLIENT_SETTINGS)
    # A broadcast instance without a url follows the RESOLVED cache.default (an
    # INFRA override of its url included), not the raw CACHE_URL.
    cache_default = (config.get("cache") or {}).get(DEFAULT_INSTANCE)
    broadcast_url = (
        cache_default.options.get("url")
        if cache_default is not None and cache_default.driver == "redis"
        else None
    ) or cache_url
    for cfg in (config.get("broadcast") or {}).values():
        if cfg.driver == "redis":
            _fill_url(cfg.options, "url", broadcast_url)
            _fill(cfg.options, settings, _TRANSPORT_CLIENT_SETTINGS)
    _fill_atomic_store_defaults(config, settings, broadcast_url)
    _fill_eventbus_defaults(config, settings, cache_url)
    for cfg in (config.get("taskqueue") or {}).values():
        if cfg.driver == "celery":
            _fill_url(cfg.options, "broker", getattr(settings, "broker_url", None))
            _fill_url(cfg.options, "backend", cache_url)
            _fill(cfg.options, settings, _CELERY_SETTINGS)
            _cap_probe_bound(cfg.options, settings)


def load_infra_config(settings: Settings) -> InfraConfig:
    """Return the fully merged infrastructure configuration, the built-in
    drivers' options filled from the flat settings (:func:`fill_driver_defaults`)."""
    config = _synthesize_defaults(settings)
    raw = getattr(settings, "infra", None)
    if raw:
        if not isinstance(raw, Mapping):
            raise InfraConfigError("INFRA must be a JSON object")
        _overlay(config, raw)
    fill_driver_defaults(config, settings)
    return config


__all__ = [
    "DEFAULT_EVENT_STREAM_RETENTION_SECONDS",
    "DEFAULT_INSTANCE",
    "KERNEL_INSTANCES",
    "REQUIRED_AT_STARTUP",
    "DefaultInfra",
    "use_default_infra",
    "configured_stream_retention_seconds",
    "database_instance_url",
    "eventbus_driver",
    "fill_driver_defaults",
    "InstanceConfig",
    "InfraConfig",
    "InfraConfigError",
    "load_infra_config",
]
