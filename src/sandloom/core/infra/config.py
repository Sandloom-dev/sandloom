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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from sandloom.core.config.cache import CacheConfig
from sandloom.core.config.common import Environment
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

_NO_SETTINGS: Mapping[str, str] = MappingProxyType({})

#: The settings a driver may take an option from: the flat infrastructure
#: settings (``CACHE_URL``, ``BROKER_URL``, the ``CACHE_*`` client bounds) and
#: the readiness probe's bound. Any other setting (a secret among them) is
#: refused when a :class:`SettingsFill` is built (ADR-0016: a driver reads no
#: kernel setting beyond these flat ones).
FILLABLE_SETTINGS: frozenset[str] = frozenset(CacheConfig.model_fields) | {
    "health_check_timeout_seconds"
}


@dataclass(frozen=True, slots=True)
class SettingsFill:
    """The options a driver takes from the flat settings when its instance
    leaves them out (``InfraProvider.settings_fill``).

    A driver reads no kernel setting (ADR-0016): it declares, on its class,
    which flat setting fills which option, and the kernel applies the
    declaration without knowing the driver (:func:`fill_driver_defaults`). An
    option an instance states always wins: ``INFRA`` over the flat setting.
    Every key of the three tables is an option name, every value one of
    :data:`FILLABLE_SETTINGS` (anything else is refused, a typo included).

    - ``urls``: an unset (or empty) option takes the setting's value when it is
      set. Which instances take it is ``urls_for``: ``"all"``, only the
      ``default`` instance (a named instance without one shares the default's
      transport), or only an instance the kernel synthesized (``implicit``: one
      ``INFRA`` declares has its own url, never the cache's).
    - ``follows``: the capability whose ``default`` instance the ``urls`` follow.
      When that instance, as resolved (``INFRA`` and its own fill applied), runs
      the same driver, its option is the value; otherwise the setting is. An
      instance that follows is filled after the ones that do not, and what it
      follows follows nothing.
    - ``bounds``: an option left out takes the setting's value when it is set
      (a client timeout, a pool size).
    - ``caps``: a numeric option above the setting is cut to it (a probe the
      readiness check stops waiting for); a value that is not a number stays
      for the driver's options to refuse.
    """

    urls: Mapping[str, str] = field(default_factory=lambda: _NO_SETTINGS)
    urls_for: Literal["all", "default", "implicit"] = "all"
    follows: str | None = None
    bounds: Mapping[str, str] = field(default_factory=lambda: _NO_SETTINGS)
    caps: Mapping[str, str] = field(default_factory=lambda: _NO_SETTINGS)

    def __post_init__(self) -> None:
        if self.urls_for not in ("all", "default", "implicit"):
            raise ValueError(f"SettingsFill.urls_for is {self.urls_for!r}")
        if self.follows is not None and not self.urls:
            raise ValueError("SettingsFill.follows needs urls to follow with")
        for name in ("urls", "bounds", "caps"):
            table: Mapping[str, str] = getattr(self, name)
            if not all(
                isinstance(option, str) and isinstance(setting, str)
                for option, setting in table.items()
            ):
                raise ValueError(
                    "SettingsFill tables map option names to setting names"
                )
            unknown = sorted(set(table.values()) - FILLABLE_SETTINGS)
            if unknown:
                raise ValueError(
                    f"SettingsFill.{name} names {', '.join(unknown)}: a driver "
                    f"takes options only from {', '.join(sorted(FILLABLE_SETTINGS))}"
                )
            # A copy nobody can change after the class declared it.
            object.__setattr__(self, name, MappingProxyType(dict(table)))


#: ``(capability, driver) -> the fill its class declares``, ``None`` for a driver
#: that is not registered, whose package is missing or that declares none: no
#: fill then, and the refusal of an unusable driver is the registry's.
FillLookup = Callable[[str, str], SettingsFill | None]


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

#: The composition's driver selection of this process. The kernel names no
#: driver of its own for database, cache, event bus, task queue, broadcast,
#: lease or atomic_store: a composition (``sandloom_standard``) supplies them,
#: and one that supplies none leaves startup to refuse with SANDLOOM-B037.
#:
#: Three sources, ONE precedence (the first that is set wins, none merges):
#:
#: 1. the argument of the latest ``create_app(default_infra=...)`` (``_APP``);
#: 2. a pin a tool or a test suite set with :func:`use_default_infra` (``_PIN``);
#: 3. the composition root's own ``DEFAULT_INFRA`` (read when asked).
#:
#: ``create_app`` only ever writes slot 1, so an app never overwrites a pin;
#: an app that passes none clears slot 1 and the pin (or the root) is back.
#: A new :func:`use_default_infra` pin clears slot 1 too: it is the later word.
_APP: InfraConfig | None = None
_PIN: InfraConfig | None = None


def _checked(defaults: DefaultInfra | None) -> InfraConfig:
    """``defaults`` shape-checked like ``INFRA`` (:func:`_overlay`)."""
    checked: InfraConfig = {}
    _overlay(checked, defaults or {})
    return checked


def use_default_infra(defaults: DefaultInfra | None, *, from_app: bool = False) -> None:
    """Set the composition's driver selection for this process (``None``: none).

    Shape-checked like ``INFRA``; every instance it names is a synthesized
    ``default``: ``INFRA`` overrides it, and nothing opens it unless the kernel
    or a module's requirement uses it. Without ``from_app`` it is a pin (a tool
    or a test suite): it survives every ``create_app`` of the process, whatever
    selection those pass. ``from_app`` is ``create_app``'s: the argument of
    that app, dropped by :func:`forget_app_default_infra`.
    """
    global _APP, _PIN
    checked = _checked(defaults)
    if from_app:
        _APP = checked
    else:
        _PIN = checked
        _APP = None


def forget_app_default_infra() -> None:
    """Drop the selection a previous ``create_app`` of this process passed, so
    an app that passes none never inherits another's: the next reader gets the
    pin of :func:`use_default_infra`, else the composition root's own
    (``DEFAULT_INFRA``), as in a fresh process."""
    global _APP
    _APP = None


def _root_selection() -> InfraConfig:
    """A process that boots no app (``sandloom migrate``, ``worker check``, the
    backup tools) never ran ``create_app(default_infra=...)``: it reads the
    selection the composition root's package exports (``DEFAULT_INFRA``)."""
    # Reached by name: ``composition`` sits above the infra layer (ADR-0007).
    composition = importlib.import_module("sandloom.core.composition")
    return _checked(composition.root_default_infra())


def _composition_instances() -> InfraConfig:
    """A copy of the composition's selection (precedence above), safe to change."""
    if _APP is not None:
        selection = _APP
    elif _PIN is not None:
        selection = _PIN
    else:
        selection = _root_selection()
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
        for capability, instances in selection.items()
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


def _eventbus_selector(settings: Settings) -> str | None:
    """The driver ``EVENT_BUS_BACKEND`` names, ``None`` when it is unset (the
    composition's ``eventbus.default`` decides: no kernel default, in any
    environment)."""
    return settings.event_bus_backend


def _synthesize_defaults(settings: Settings) -> InfraConfig:
    """Build the implicit ``default`` instance for each capability.

    The composition's selection (:func:`use_default_infra`) is the base, the
    flat settings give its options, ``STORAGE_TYPE``, ``NOTIFIER_BACKEND`` and
    an explicit ``EVENT_BUS_BACKEND`` select their drivers. Every instance
    built here is marked ``implicit``; :func:`_overlay` makes it explicit when
    ``INFRA`` names it. A capability nothing selects has no instance.
    """
    # The bucket, region and keys of an ``s3`` store are options of its INFRA
    # instance (INFRA.objectstore.default); the flat settings give the size cap
    # and the local path only.
    objectstore_options: dict[str, Any] = {
        "max_upload_size_bytes": settings.max_upload_size_bytes,
    }
    if settings.storage_type != "s3":
        objectstore_options["path"] = settings.storage_path

    synthesized = _composition_instances()
    _flat_options(synthesized, "database", {"url": settings.database_url})
    _flat_options(
        synthesized,
        "taskqueue",
        {"broker": settings.broker_url, "backend": settings.cache_url},
    )
    # The in-tree objectstore and notifier drivers are selected by their own
    # flat settings (``local`` and ``log`` by default); the options of ``smtp``
    # (host, port, credentials, ``use_tls``) come from INFRA.notification.
    _select(synthesized, "objectstore", settings.storage_type, objectstore_options)
    _select(synthesized, "notification", settings.notifier_backend, {})
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
                # driver's, and they can hold its secrets (an SMTP password).
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
    over the composition's selection, then the ``INFRA`` overlay), limited to the
    eventbus so it reads nothing else and runs before the registry exists.
    ``None`` when ``instance`` is not configured. A malformed ``INFRA``
    raises :class:`InfraConfigError`, as loading the whole config would.
    """
    found = resolved_eventbus(settings).get(instance)
    return found.driver if found is not None else None


def resolved_eventbus(settings: Any) -> dict[str, InstanceConfig]:
    """The eventbus instances: ``EVENT_BUS_BACKEND`` resolved, ``INFRA`` applied
    (options not filled from the flat settings: a driver's declaration does
    that, once the registry has its class)."""
    return _resolved_capability(settings, "eventbus", _eventbus_selector(settings))


def resolved_instances(settings: Any, capability: str) -> dict[str, InstanceConfig]:
    """The instances of ``capability`` as the application resolves them: the
    composition's selection, the flat selector of its ``default`` driver
    (``EVENT_BUS_BACKEND``, ``NOTIFIER_BACKEND``) and ``INFRA`` applied. Reads
    nothing else (options are not filled from the flat settings), so a check of
    the configuration (the override report, the production profile) sees what a
    ``DEFAULT_INFRA`` pin or the composition root selects, not only what
    ``INFRA`` writes."""
    selector = {
        "eventbus": "event_bus_backend",
        "notification": "notifier_backend",
    }.get(capability)
    selected = getattr(settings, selector, None) if selector else None
    return _resolved_capability(settings, capability, selected)


def _resolved_capability(
    settings: Any, capability: str, selected: str | None
) -> dict[str, InstanceConfig]:
    config: InfraConfig = {
        capability: _composition_instances().get(capability, {}),
    }
    if selected is not None:
        _select(config, capability, selected, {})
    raw = getattr(settings, "infra", None)
    if raw:
        if not isinstance(raw, Mapping):
            raise InfraConfigError("INFRA must be a JSON object")
        if capability in raw:
            _overlay(config, {capability: raw[capability]})
    return config[capability]


def _gets_url(cfg: InstanceConfig, fill: SettingsFill) -> bool:
    if fill.urls_for == "default":
        return cfg.name == DEFAULT_INSTANCE
    if fill.urls_for == "implicit":
        return cfg.implicit
    return True


def _followed_option(
    config: InfraConfig, cfg: InstanceConfig, fill: SettingsFill, option: str
) -> Any:
    """What ``option`` of the ``default`` instance ``fill`` follows is, as
    resolved, when that instance runs the same driver as ``cfg``."""
    if fill.follows is None:
        return None
    followed = (config.get(fill.follows) or {}).get(DEFAULT_INSTANCE)
    if followed is None or followed.driver != cfg.driver:
        return None
    return followed.options.get(option)


def _apply_fill(
    config: InfraConfig, cfg: InstanceConfig, fill: SettingsFill, settings: Any
) -> None:
    options = cfg.options
    if _gets_url(cfg, fill):
        for option, setting in fill.urls.items():
            value = _followed_option(config, cfg, fill, option) or getattr(
                settings, setting, None
            )
            if not options.get(option) and value:
                options[option] = value
    for option, setting in fill.bounds.items():
        value = getattr(settings, setting, None)
        if value is not None:
            options.setdefault(option, value)
    for option, setting in fill.caps.items():
        bound = getattr(settings, setting, None)
        stated = options.get(option)
        if (
            isinstance(bound, int | float)
            and isinstance(stated, int | float)
            and not isinstance(stated, bool)
            and stated > bound
        ):
            options[option] = bound


def fill_driver_defaults(
    config: InfraConfig, settings: Any, fill_for: FillLookup
) -> None:
    """Fill each instance's options from the flat settings, as its driver
    declares (:class:`SettingsFill`, ``InfraProvider.settings_fill``).

    A driver reads no kernel setting (ADR-0016): what it takes from
    ``CACHE_URL``, ``BROKER_URL`` and the ``CACHE_*`` client bounds, the kernel
    puts into the options of an instance that leaves them out, and a stated
    option always wins. The kernel names no driver here: ``fill_for`` answers,
    per ``(capability, driver)``, the declaration of the driver's class, and
    ``None`` leaves the instance as it is.
    """
    pending = [
        (cfg, fill)
        for capability, instances in config.items()
        for cfg in instances.values()
        if (fill := fill_for(capability, cfg.driver)) is not None
    ]
    # An instance that follows another (a broadcast on the cache's Redis) reads
    # the resolved option of the one it follows: after it. The sort is stable.
    pending.sort(key=lambda item: item[1].follows is not None)
    for cfg, fill in pending:
        _apply_fill(config, cfg, fill, settings)


def load_infra_config(settings: Settings) -> InfraConfig:
    """Return the merged infrastructure configuration: the composition's
    selection, the flat settings that select a driver or give an option the
    kernel owns, and ``INFRA`` over them. The options a driver takes from the
    flat settings are not filled here (no driver class is loaded):
    ``build_infra_registry`` fills them once the registry has the classes
    (:func:`fill_driver_defaults`)."""
    config = _synthesize_defaults(settings)
    raw = getattr(settings, "infra", None)
    if raw:
        if not isinstance(raw, Mapping):
            raise InfraConfigError("INFRA must be a JSON object")
        _overlay(config, raw)
    return config


__all__ = [
    "DEFAULT_INSTANCE",
    "KERNEL_INSTANCES",
    "REQUIRED_AT_STARTUP",
    "DefaultInfra",
    "FillLookup",
    "SettingsFill",
    "use_default_infra",
    "database_instance_url",
    "eventbus_driver",
    "fill_driver_defaults",
    "InstanceConfig",
    "InfraConfig",
    "InfraConfigError",
    "load_infra_config",
    "resolved_eventbus",
    "resolved_instances",
]
