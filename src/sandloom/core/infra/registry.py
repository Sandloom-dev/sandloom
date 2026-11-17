"""Infrastructure registry: driver registration + config-driven resolution.

The registry is the single place that maps a ``(capability, instance name)``
to a live :class:`~sandloom.core.infra.contracts.InfraProvider`. Drivers register a
factory keyed by ``(capability, driver name)``; configuration declares which
driver backs each named instance. Providers are built lazily on first access
and cached for the lifetime of the registry.

Drivers come from two sources:

* built-in drivers (registered by :func:`register_builtin_drivers`)
* driver packages, named in the ``sandloom.drivers`` entry-point group and
  imported only when configuration selects them (:func:`load_selected_plugins`)

A business module never touches this class directly — it asks the
:class:`~sandloom.core.facade.CoreFacade` for a capability by logical name.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import importlib.util
import inspect
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from sandloom.core.config.removed_drivers import removed_driver_problem
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    KERNEL_INSTANCES,
    InfraConfig,
    InstanceConfig,
    SettingsFill,
    resolved_eventbus,
)
from sandloom.core.infra.ports._base import (
    DRIVER_TIERS,
    CapabilitySpec,
    DriverTier,
    InfraProvider,
    health_failure_details,
    version_major,
)

if TYPE_CHECKING:
    from sandloom.core.descriptors import CapabilityRequirement

logger = logging.getLogger("sandloom.core.infra")

#: Entry-point groups: ``"<capability>.<driver>" = "pkg.driver:Class"`` and
#: ``"<capability>" = "pkg.contracts:SPEC"``.
DRIVERS_GROUP = "sandloom.drivers"
CAPABILITIES_GROUP = "sandloom.capabilities"

#: A driver factory turns an instance config into a live provider.
DriverFactory = Callable[[InstanceConfig], InfraProvider]


def versions_compatible(registered: str, required: str) -> bool:
    """True if ``required`` is satisfied by the ``registered`` spec version.

    Compatible iff the MAJOR components match (``v1`` requirement satisfied by
    ``v1``/``v1.2``, but not ``v2``). If either version is unparseable we fall
    back to exact-string equality — the safe, conservative behaviour.

    The rule this relies on (ADR-0011, rules 4 and 5): every version of one
    major has the same required methods, pinned per version by a test. A
    minor version never adds an obligation; what only some drivers can do is
    a declared feature, checked on its own (``supported_features``, the
    requirement's ``features``), never inferred from a version number.
    """
    req_major = version_major(required)
    reg_major = version_major(registered)
    if req_major is None or reg_major is None:
        return registered == required
    return req_major == reg_major


@dataclass(frozen=True)
class DriverRegistration:
    """A registered driver: its factory plus build-free metadata.

    A driver is registered by its provider class (``provider_class``), or as a
    lazy ``"module:Class"`` ``target`` imported only when an instance selects
    it (the built-ins, as entry points are: ADR-0011 decision 2). The class
    declares its features once (``features_for(options)``), its options schema
    and its contract major; the registry reads them to preflight a requirement
    WITHOUT building the provider: no engine/client/pool created, no network
    I/O. A plain ``factory`` with no class (a test double) declares its
    ``features`` here instead.
    """

    factory: DriverFactory | None = None
    features: frozenset[str] = field(default_factory=frozenset)
    config_schema: type[Any] | None = None
    # The provider class, once known: features, schema and factory come from it.
    provider_class: type[InfraProvider] | None = None
    # ``"package.module:Class"``, imported when an instance selects the driver.
    target: str | None = None
    # The slice that delivers a pre-registered target not in this build yet.
    delivered_by: str = ""
    # The SQLAlchemy dialect names a database driver serves (``postgresql``):
    # startup checks that an instance's URL names one of them and that a SQL
    # dialect renderer exists for it (SANDLOOM-B024). Empty for non-SQL drivers.
    sql_dialects: frozenset[str] = field(default_factory=frozenset)
    # The top-level import names the driver needs at build time (``celery``,
    # ``redis``) and how to install them. The preflight checks them with
    # ``importlib.util.find_spec``: nothing imported, no network. A built-in
    # driver is registered whether or not its package is installed, so this
    # is how a registered driver is told from a usable one (SANDLOOM-B037).
    requires: tuple[str, ...] = ()
    install: str = ""
    # How far the driver has been proven (spec 2.5): the registry, doctor,
    # /diagnostics/infra and the production check read it here, never on the
    # class. A built-in states it in ``register_driver(..., tier=)``, an entry
    # point through its class attribute ``tier``.
    tier: DriverTier = "experimental"

    def features_for(self, options: Mapping[str, Any]) -> frozenset[str]:
        """The features an instance with ``options`` declares, build-free:
        what the class's ``features_for`` answers within its
        ``supported_features`` (itself within the spec's features, checked at
        load), so a driver never claims a feature it did not declare."""
        cls = self.provider_class
        if cls is not None:
            return frozenset(cls.features_for(options)) & cls.supported_features
        return self.features


def _install_fix(
    capability: str,
    name: str,
    registration: DriverRegistration,
    missing: tuple[str, ...],
    features: frozenset[str] | None,
) -> str:
    """The fix for a driver that cannot run here: install what it needs, or
    select another driver for the instance in ``INFRA``."""
    install = registration.install or (
        f"install {', '.join(missing)}" if missing else "reinstall the driver"
    )
    advertising = f" that advertises {sorted(features)}" if features else ""
    return (
        f"{install}, or select an installed {capability} driver{advertising} "
        f"in INFRA[{capability!r}][{name!r}]"
    )


class InfraRegistryError(RuntimeError):
    """Raised on registry misconfiguration (unknown driver/instance).

    A configuration problem found while selecting, loading or opening a
    driver. The errors a capability OPERATION raises are another hierarchy,
    :class:`sandloom.core.infra.errors.InfraError`.
    """


class InfraStartupError(InfraRegistryError):
    """One or more configured infra instances failed to open at startup.

    Raised by :meth:`InfraRegistry.aopen_all` in strict mode (production) so a
    database/cache/object-store the process cannot reach aborts the lifespan
    instead of letting the app serve traffic that will fail on first use.
    ``sandloom_code`` is SANDLOOM-B037 when an instance failed because its
    driver is not installed or cannot be imported, else unset.
    """

    def __init__(
        self, message: str, *, fix: str | None = None, code: str | None = None
    ) -> None:
        self.fix = fix
        if code is not None:
            self.sandloom_code = code
        super().__init__(message if fix is None else f"{message}\nFix: {fix}")


class CapabilityError(InfraRegistryError):
    """Base for capability-resolution problems — carries an actionable hint.

    Diagnostic by design: AI agents and operators should be able to act on the
    message without reading kernel source. Subclasses pin the exact failure;
    ``fix`` lists concrete remedies.
    """

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        self.fix = fix
        # The message without the fix, for diagnostics that compose several.
        self.reason = message
        super().__init__(message if fix is None else f"{message}\nFix: {fix}")


class UnknownCapabilityError(CapabilityError):
    """No :class:`CapabilitySpec` is registered for the requested capability."""


class CapabilityConflictError(CapabilityError):
    """A capability is re-registered with a different contract/version."""


class ProviderContractError(CapabilityError):
    """A driver returned a provider that does not implement the contract."""


class FeatureUnsupportedError(CapabilityError):
    """A required feature is unknown to the spec or unsupported by the driver."""


class InstanceNotConfiguredError(CapabilityError):
    """No instance with the requested name is configured for the capability."""


class DriverNotRegisteredError(CapabilityError):
    """The instance names a driver that no plugin registered."""


class DriverUnavailableError(CapabilityError):
    """An instance's driver is registered and cannot run here (SANDLOOM-B037).

    Its packages are not installed (found build-free, by ``find_spec``), or
    building it failed to import a dependency. Carries what a diagnostic
    names: the capability, the instance, the driver, the missing import names
    and the install hint.
    """

    sandloom_code = "SANDLOOM-B037"

    def __init__(
        self,
        message: str,
        *,
        fix: str | None = None,
        capability: str = "",
        instance: str = "",
        driver: str = "",
        missing: tuple[str, ...] = (),
        install: str = "",
    ) -> None:
        self.capability = capability
        self.instance = instance
        self.driver = driver
        self.missing = missing
        self.install = install
        # The text names its code wherever it surfaces (a module's load
        # failure, a log line); ``reason`` stays untagged for composition.
        super().__init__(f"{message} [SANDLOOM-B037]", fix=fix)
        self.reason = message


class CapabilityUnavailable(CapabilityError):
    """A module requires a capability its composition cannot serve
    (SANDLOOM-B037): every unmet required requirement of the module, each
    naming the capability, instance, driver, the missing feature or package,
    and the fix. The raised class also subclasses each underlying error's
    class (:func:`_unavailable_class`)."""

    sandloom_code = "SANDLOOM-B037"

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        # Not a cause's own __init__ (DriverUnavailableError's would tag twice).
        CapabilityError.__init__(self, message, fix=fix)


_UNAVAILABLE_CLASSES: dict[frozenset[type], type[CapabilityUnavailable]] = {}


def _unavailable_class(causes: tuple[type, ...]) -> type[CapabilityUnavailable]:
    """:class:`CapabilityUnavailable`, also a subclass of each cause's class
    (``FeatureUnsupportedError``, ``InstanceNotConfiguredError``, ...), so a
    handler of the precise error keeps matching the aggregated refusal. The
    code stays SANDLOOM-B037, first in the MRO."""
    distinct = {c for c in causes if not issubclass(CapabilityUnavailable, c)}
    # Keep the most derived: a base next to its subclass has no consistent MRO.
    most_derived = [
        c
        for c in distinct
        if not any(o is not c and issubclass(o, c) for o in distinct)
    ]
    bases = tuple(sorted(most_derived, key=lambda c: c.__qualname__))
    key = frozenset(bases)
    found = _UNAVAILABLE_CLASSES.get(key)
    if found is None:
        found = type(
            "CapabilityUnavailable",
            (CapabilityUnavailable, *bases),
            {"__module__": __name__, "sandloom_code": "SANDLOOM-B037"},
        )
        _UNAVAILABLE_CLASSES[key] = found
    return found


class DriverConflictError(CapabilityError):
    """A selected driver or capability name has two providers (SANDLOOM-B021)."""

    sandloom_code = "SANDLOOM-B021"


class DriverLoadError(CapabilityError):
    """A selected driver or capability cannot be used (SANDLOOM-B022)."""

    sandloom_code = "SANDLOOM-B022"


class DevelopmentOnlyDriverRefused(DriverLoadError):
    """A ``development_only`` driver selected in production (SANDLOOM-B022).

    No accepted risk lifts it: a ``memory`` driver keeps its state in one
    process, so nothing a deployment's guarantees rest on may run on it.
    """


class KernelOnlyCapabilityRefused(CapabilityError):
    """A module resolved or required a kernel-facing capability
    (``CapabilitySpec.kernel_only``: ``atomic_store``, ``lease``,
    ``broadcast``), which only the kernel reaches (SANDLOOM-B041)."""

    sandloom_code = "SANDLOOM-B041"


def _refuse_kernel_only(spec: CapabilitySpec | None, who: str = "module code") -> None:
    if spec is not None and spec.kernel_only:
        raise KernelOnlyCapabilityRefused(
            f"{who} cannot use capability {spec.name!r}: it is kernel-facing, "
            "reached only by the kernel [SANDLOOM-B041]",
            fix="use the module-facing API built on it (sandloom.sdk), or a "
            "module-facing capability; `sandloom explain SANDLOOM-B041`",
        )


class ExperimentalCapabilityRefused(CapabilityError):
    """An experimental capability configured in production (SANDLOOM-B023)."""

    sandloom_code = "SANDLOOM-B023"


class ExperimentalDriverRefused(ExperimentalCapabilityRefused):
    """An ``experimental`` driver selected in production (SANDLOOM-B023), unless
    the deployment accepts the risk ``experimental_driver`` (spec 2.5)."""


class DatabaseFeatureMissing(FeatureUnsupportedError):
    """A module uses a ``sandloom.sdk.data`` form, or declares a database
    feature, that the database instance it runs on lacks (SANDLOOM-B033)."""

    sandloom_code = "SANDLOOM-B033"


class ExperimentalCapabilityError(InstanceNotConfiguredError, DriverNotRegisteredError):
    """An EXPERIMENTAL capability was resolved but no driver is registered.

    Core ships contracts only for experimental capabilities (``document_store``,
    ``graph_store``, ``workflow``, ``search``, ``lock``, ``secrets``). Subclasses
    both :class:`InstanceNotConfiguredError` and
    :class:`DriverNotRegisteredError` so existing handlers keep matching.
    """


class ConfigSchemaError(CapabilityError):
    """An instance's options failed the capability's config schema."""


class TransactionalOutboxUnsupportedError(CapabilityError):
    """Publishing inside a UoW on a database whose driver has no outbox."""


@dataclass(frozen=True)
class InstanceSnapshot:
    """One instance's configuration + built provider (``snapshot_instance``)."""

    capability: str
    name: str
    config: InstanceConfig | None
    provider: InfraProvider | None


def _refuse_removed_drivers(config: Iterable[InstanceConfig]) -> None:
    """Refuse a configuration that names a driver that was removed, from every
    way the registry takes one (the loaders refuse what they read; a
    configuration built by hand reaches the registry here)."""
    for cfg in config:
        problem = removed_driver_problem(cfg.capability, cfg.name, cfg.driver)
        if problem is not None:
            raise InfraRegistryError(problem)


class InfraRegistry:
    """Holds drivers + configuration and resolves provider instances."""

    def __init__(
        self, config: InfraConfig | None = None, *, strict_capabilities: bool = False
    ) -> None:
        _refuse_removed_drivers(
            cfg for instances in (config or {}).values() for cfg in instances.values()
        )
        # (capability, driver) -> registration (factory + features + schema)
        self._drivers: dict[tuple[str, str], DriverRegistration] = {}
        # When strict, a driver cannot be registered for a capability that has
        # no spec (production default via build_infra_registry). Lenient by
        # default so unit tests can build bare registries without specs.
        self._strict = strict_capabilities
        # capability name -> spec (versioned contract). A capability with no
        # registered spec still resolves (a lenient registry built directly in
        # a test needs no specs) — the contract/feature checks are skipped
        # when the spec is absent.
        self._capabilities: dict[str, CapabilitySpec] = {}
        # capability -> {instance_name -> config}
        self._config: InfraConfig = config or {}
        # (capability, instance_name) -> built provider (lazy cache)
        self._providers: dict[tuple[str, str], InfraProvider] = {}
        self._retired_providers: list[InfraProvider] = []
        # Guards the check-then-build in provider() so two threads (sync test
        # clients, thread-pool callers) cannot each build a provider and leak
        # the loser's engine/pool (it would never be in _providers, so
        # aclose_all would miss it).
        self._build_lock = threading.Lock()
        # (capability, instance) a validated requirement named: with
        # KERNEL_INSTANCES and every explicit instance, what is in use.
        self._requested: set[tuple[str, str]] = set()
        # import name -> installed (find_spec answers once per process).
        self._installed: dict[str, bool] = {}
        # capability -> {constructor argument -> kernel policy} for the drivers
        # built from an entry point (see :meth:`set_policy`).
        self._policies: dict[str, dict[str, Callable[[], bool]]] = {}
        # (capability, driver) -> (distribution, install command) of a driver
        # package the kernel knows by name (see :meth:`register_driver_package`).
        self._driver_packages: dict[tuple[str, str], tuple[str, str]] = {}

    def set_policy(self, capability: str, **policy: Callable[[], bool]) -> None:
        """The kernel's policy for ``capability``'s drivers: each ``name=hook`` is
        passed as that constructor argument to a provider class that declares a
        parameter of the name (the Redis ``atomic_store`` and ``lease`` take
        ``assume_allowed``, spec 13 item 45), whether the class comes from an
        entry point or a lazy row. A class without the parameter is built by
        its ``from_config`` as before. The hook is called by the driver, when
        it needs the answer, never here."""
        self._policies.setdefault(capability, {}).update(policy)

    def register_driver_package(
        self, capability: str, driver: str, *, package: str, install: str
    ) -> None:
        """Name the package (its top-level import name) that ships
        ``capability/driver``, so an instance that selects the driver while the
        package is not installed is refused with the install hint
        (SANDLOOM-B037) instead of as an unknown driver. The package registers
        the driver itself (entry point ``sandloom.drivers``); this registers no
        driver."""
        if not package.isidentifier():
            raise ValueError(
                f"Driver {capability}/{driver}: package= is a top-level import "
                f"name, got {package!r}"
            )
        self._driver_packages[(capability, driver)] = (package, install)

    # ------------------------------------------------------------------ #
    # Capability registration
    # ------------------------------------------------------------------ #
    def register_capability(self, spec: CapabilitySpec) -> None:
        """Register (or idempotently re-register) a capability spec.

        Re-registering the SAME spec (same contract + version) is a no-op so
        bootstrap can run more than once. Re-registering with a DIFFERENT
        contract or version is a conflict (two plugins claim the same name).
        """
        existing = self._capabilities.get(spec.name)
        if existing is not None and (
            existing.provider_contract is not spec.provider_contract
            or existing.version != spec.version
            or existing.major != spec.major
            or existing.kernel_only != spec.kernel_only
        ):
            raise CapabilityConflictError(
                f"Capability {spec.name!r} already registered as "
                f"{existing.provider_contract.__name__} {existing.version}; "
                f"refusing to override with {spec.provider_contract.__name__} "
                f"{spec.version}",
                fix="rename the new capability, or ensure only one plugin registers it",
            )
        self._capabilities[spec.name] = spec

    def capability_spec(self, capability: str) -> CapabilitySpec | None:
        """Return the registered spec for a capability, or None."""
        return self._capabilities.get(capability)

    def _check_experimental_driver(
        self, capability: str, driver: str | None = None
    ) -> None:
        """Raise :class:`ExperimentalCapabilityError` for a driver-less experiment.

        ``driver`` None → the capability has no driver registered at all;
        otherwise the configured ``driver`` is not registered. A no-op for
        stable (or unknown) capabilities, which keep their precise errors.
        """
        spec = self._capabilities.get(capability)
        if spec is None or not spec.is_experimental:
            return
        if driver is None:
            if any(c == capability for (c, _d) in self._drivers):
                return
            detail = "no driver is registered for it"
        else:
            if (capability, driver) in self._drivers:
                return
            detail = f"its configured driver {driver!r} is not registered"
        raise ExperimentalCapabilityError(
            f"Capability {capability!r} is EXPERIMENTAL and {detail}: core ships "
            "only the contract (no driver), and the contract may still change",
            fix=f"install a driver package for {capability!r} (an entry point "
            f"'{capability}.<driver>' in the 'sandloom.drivers' group) and configure "
            f"INFRA[{capability!r}][<name>], or avoid depending on this "
            "experimental capability",
        )

    def capabilities(self) -> list[CapabilitySpec]:
        """Return all registered capability specs (sorted by name)."""
        return [self._capabilities[name] for name in sorted(self._capabilities)]

    # ------------------------------------------------------------------ #
    # Driver & config registration
    # ------------------------------------------------------------------ #
    def register_driver(
        self,
        capability: str,
        driver: str,
        factory: DriverFactory | type[InfraProvider] | str,
        *,
        features: frozenset[str] | set[str] | None = None,
        config_schema: type[Any] | None = None,
        capability_version: str | None = None,
        sql_dialects: frozenset[str] | set[str] | None = None,
        requires: Iterable[str] = (),
        install: str = "",
        build: DriverFactory | None = None,
        delivered_by: str = "",
        tier: DriverTier = "experimental",
    ) -> None:
        """Register a driver for a capability (idempotent override).

        ``factory`` is the driver's provider class, a lazy ``"module:Class"``
        target (imported when an instance selects the driver, so registering
        it imports nothing), or a plain factory. A class declares its features
        (``supported_features`` / ``features_for``), options schema and
        integer ``contract_version`` once, and the registry checks it like an
        external driver (SANDLOOM-B022); ``build`` replaces its
        ``from_config`` where the factory needs what the class cannot reach.
        A plain factory has no class: ``features``/``config_schema`` are its
        build-free metadata. In strict mode the capability must already have
        a registered spec, and a plain factory's features must be a subset of
        the spec's. ``capability_version`` is the contract version the driver
        was written for: another major version of the capability is refused.
        ``requires`` are the top-level import names the driver needs and
        ``install`` how to get them (``pip install 'sandloom[celery]'``).
        ``delivered_by`` names the slice that delivers a pre-registered target
        not in this build yet, for the refusal when it is selected. ``tier`` is
        how far the driver has been proven (``experimental``, ``beta`` or
        ``stable``, spec 2.5): production refuses an experimental one unless the
        risk ``experimental_driver`` is accepted.
        """
        if tier not in DRIVER_TIERS:
            raise ValueError(
                f"Driver {capability}/{driver}: tier= is one of "
                f"{', '.join(DRIVER_TIERS)}, got {tier!r}"
            )
        needed = tuple(requires)
        dotted = [name for name in needed if not name or "." in name]
        if dotted:
            # find_spec of a dotted name imports its parent: only top-level
            # names keep the availability check import-free.
            raise ValueError(
                f"Driver {capability}/{driver}: requires= takes top-level import "
                f"names, got {dotted}"
            )
        feats = frozenset(features or ())
        registered = self._capabilities.get(capability)
        if (
            capability_version is not None
            and registered is not None
            and not versions_compatible(registered.version, capability_version)
        ):
            raise CapabilityConflictError(
                f"Driver {capability}/{driver} was written for "
                f"{capability_version}, but the capability is {registered.version}",
                fix="update the driver to the capability's current contract",
            )
        if self._strict:
            spec = self._capabilities.get(capability)
            if spec is None:
                raise UnknownCapabilityError(
                    f"Cannot register driver {driver!r}: capability "
                    f"{capability!r} has no registered spec",
                    fix="register_capability(...) before its drivers",
                )
            unknown = feats - spec.features
            if unknown:
                raise FeatureUnsupportedError(
                    f"Driver {capability}/{driver} declares feature(s) "
                    f"{spec.unknown_features_hint(unknown)} not defined by "
                    f"capability {capability!r} (known: {sorted(spec.features)})",
                    fix="add the feature to the CapabilitySpec or drop it",
                )
        key = (capability, driver)
        if key in self._drivers:
            logger.debug("[infra] overriding driver %s/%s", capability, driver)
        registration = DriverRegistration(
            factory=build,
            features=feats,
            config_schema=config_schema,
            sql_dialects=frozenset(sql_dialects or ()),
            requires=needed,
            install=install,
            delivered_by=delivered_by,
            tier=tier,
        )
        if isinstance(factory, str):
            registration = dataclasses.replace(registration, target=factory)
        elif isinstance(factory, type):
            registration = self._with_class(capability, driver, registration, factory)
        else:
            registration = dataclasses.replace(registration, factory=factory)
        self._drivers[key] = registration

    def _with_class(
        self,
        capability: str,
        driver: str,
        registration: DriverRegistration,
        provider_class: object,
    ) -> DriverRegistration:
        """``registration`` completed from its checked provider class."""
        checked = check_driver_class(
            f"{capability}.{driver}", provider_class, self._capabilities.get(capability)
        )
        return dataclasses.replace(
            registration,
            provider_class=checked,
            factory=registration.factory or self._class_factory(capability, checked),
            config_schema=checked.options_schema or registration.config_schema,
        )

    def _class_factory(
        self, capability: str, provider_class: type[InfraProvider]
    ) -> DriverFactory:
        """The factory of a class without a ``build``: its ``from_config``, or,
        when the kernel has a policy for the capability (:meth:`set_policy`) the
        class takes as constructor arguments, the constructor with them."""

        def build(cfg: InstanceConfig) -> InfraProvider:
            policy = self._policies.get(capability, {})
            parameters = inspect.signature(provider_class).parameters
            hooks = {arg: hook for arg, hook in policy.items() if arg in parameters}
            if not hooks:
                return provider_class.from_config(cfg)
            constructor: Any = provider_class
            provider: InfraProvider = constructor(
                name=cfg.name, driver=cfg.driver, options=dict(cfg.options), **hooks
            )
            return provider

        return build

    def _registration(self, capability: str, driver: str) -> DriverRegistration | None:
        """The registration of ``capability/driver`` with its class loaded.

        A lazy target is imported here, the first time an instance selecting
        it needs the driver's declaration (options, features, a build), and
        checked like an entry point (SANDLOOM-B022). ``None`` when no such
        driver is registered.
        """
        registration = self._drivers.get((capability, driver))
        if registration is None or registration.target is None:
            return registration
        if registration.provider_class is not None:
            return registration
        loaded = self._with_class(
            capability,
            driver,
            registration,
            _import_target(capability, driver, registration),
        )
        self._drivers[(capability, driver)] = loaded
        return loaded

    def driver_class(self, capability: str, driver: str) -> type[InfraProvider] | None:
        """The provider class of a registered driver (importing a lazy target),
        ``None`` for an unknown driver or a plain factory."""
        registration = self._registration(capability, driver)
        return registration.provider_class if registration is not None else None

    def settings_fill(self, capability: str, driver: str) -> SettingsFill | None:
        """The flat-settings fill a registered driver's class declares
        (``InfraProvider.settings_fill``): what :func:`fill_driver_defaults`
        asks per instance. ``None`` for a driver that is not registered, a plain
        factory, a class declaring none, and a driver whose packages are not
        installed: its class is not imported, and the refusal of an instance
        that cannot run is :meth:`validate`'s (SANDLOOM-B037), not the fill's."""
        provider_class = self._installed_class(capability, driver)
        return provider_class.settings_fill if provider_class is not None else None

    def _installed_class(
        self, capability: str, driver: str
    ) -> type[InfraProvider] | None:
        """The class of a registered driver whose packages are installed, else
        ``None`` (a lazy target is imported only when it can run)."""
        registration = self._drivers.get((capability, driver))
        if registration is None or any(
            not self._is_installed(name) for name in registration.requires
        ):
            return None
        return self.driver_class(capability, driver)

    def _open_order(self) -> list[tuple[str, str]]:
        """Every configured instance in the order :meth:`aopen_all` opens
        them: ``INFRA``'s order, except that an instance whose driver declares
        ``opens_after`` waits until no instance of those capabilities is left
        to open. Rounds keep the listed order inside each one."""
        pending = [(c, n) for c, instances in self._config.items() for n in instances]
        needs: dict[tuple[str, str], tuple[str, ...]] = {}
        for capability, name in pending:
            needs[(capability, name)] = ()
            if self._unused(capability, name):
                continue
            try:
                provider_class = self._installed_class(
                    capability, self._config[capability][name].driver
                )
            except CapabilityError:
                # A driver class refused at load (SANDLOOM-B022: it does not
                # import, a feature or a declaration is invalid). Nothing to
                # order by: opening the instance reports the same failure in
                # its place, as it always did, and the healthy ones still open
                # under a non-strict startup.
                continue
            if provider_class is not None:
                needs[(capability, name)] = provider_class.opens_after
        ordered: list[tuple[str, str]] = []
        while pending:
            waiting = {capability for capability, _ in pending}
            ready = [
                key for key in pending if not any(c in waiting for c in needs[key])
            ]
            if not ready:
                raise InfraStartupError(
                    "Infrastructure drivers wait for each other to open: "
                    + ", ".join(f"{c}/{n}" for c, n in pending),
                    fix="remove the cycle in the drivers' opens_after",
                )
            ordered.extend(ready)
            pending = [key for key in pending if key not in ready]
        return ordered

    def has_driver(self, capability: str, driver: str) -> bool:
        return (capability, driver) in self._drivers

    def driver_target(self, capability: str, driver: str) -> str | None:
        """The lazy ``"module:Class"`` target a driver was pre-registered with
        (``None`` for a class or a plain factory, or an unknown driver)."""
        registration = self._drivers.get((capability, driver))
        return None if registration is None else registration.target

    def driver_tier(self, capability: str, driver: str) -> DriverTier | None:
        """The tier of a registered driver, read from its registration
        (build-free: no class is imported); ``None`` when none is registered."""
        registration = self._drivers.get((capability, driver))
        return None if registration is None else registration.tier

    # ------------------------------------------------------------------ #
    # Availability and use (build-free)
    # ------------------------------------------------------------------ #
    def _is_installed(self, name: str) -> bool:
        """Whether the top-level import ``name`` can be found, without
        importing it: ``find_spec`` reads the import path, it runs no package
        code and opens no connection. Answered once per name and registry."""
        known = self._installed.get(name)
        if known is not None:
            return known
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            # A finder that refuses the name, or a module in sys.modules
            # without a spec: either way it cannot be imported here.
            found = False
        self._installed[name] = found
        return found

    def missing_packages(
        self, capability: str, name: str = DEFAULT_INSTANCE
    ) -> tuple[str, ...]:
        """The import names instance ``capability/name``'s driver needs and
        this environment lacks, build-free; empty when the instance or its
        driver is not configured (other checks say so)."""
        cfg = self._config.get(capability, {}).get(name)
        if cfg is None:
            return ()
        registration = self._drivers.get((capability, cfg.driver))
        if registration is None:
            return ()
        return tuple(n for n in registration.requires if not self._is_installed(n))

    def _refuse_unless_implicit(
        self,
        capability: str,
        name: str,
        cfg: InstanceConfig,
        unavailable: list[DriverUnavailableError],
    ) -> None:
        """A driver of a package that is not installed is not imported: an
        instance INFRA names is refused (B037); a synthesized default is
        resolved when first used."""
        problem = None if cfg.implicit else self._unavailable(capability, name)
        if problem is not None:
            unavailable.append(problem)

    def _target_package_missing(self, capability: str, name: str) -> bool:
        """Whether instance ``capability/name``'s driver is a lazy target whose
        own package is not installed (a driver package such as ``sandloom-redis``
        the kernel pre-registers), so the target cannot be imported. Build-free."""
        cfg = self._config.get(capability, {}).get(name)
        registration = (
            None if cfg is None else self._drivers.get((capability, cfg.driver))
        )
        if registration is None or not registration.target:
            return False
        if registration.provider_class is not None:
            return False
        package = registration.target.partition(":")[0].split(".", 1)[0]
        return package in self.missing_packages(capability, name)

    def _unavailable(
        self,
        capability: str,
        name: str,
        *,
        features: frozenset[str] | None = None,
    ) -> DriverUnavailableError | None:
        """A :class:`DriverUnavailableError` when the instance's driver lacks
        installed packages, else ``None``. Build-free."""
        missing = self.missing_packages(capability, name)
        if not missing:
            return None
        cfg = self._config[capability][name]
        registration = self._drivers[(capability, cfg.driver)]
        packages = ", ".join(repr(n) for n in missing)
        verb = "is" if len(missing) == 1 else "are"
        return DriverUnavailableError(
            f"{capability}/{name}: driver {cfg.driver!r} needs the package"
            f"{'' if len(missing) == 1 else 's'} {packages}, which {verb} not "
            "installed",
            fix=_install_fix(capability, name, registration, missing, features),
            capability=capability,
            instance=name,
            driver=cfg.driver,
            missing=missing,
            install=registration.install,
        )

    def _not_runnable(
        self, capability: str, name: str, cfg: InstanceConfig
    ) -> DriverUnavailableError | None:
        """B037 for an instance whose driver package is not installed: a lazy
        row whose package is missing, or a driver the kernel knows a package
        for and no entry point registered. ``None`` otherwise."""
        if self._target_package_missing(capability, name):
            return self._unavailable(capability, name)
        return self._package_absent(capability, name, cfg)

    def _package_absent(
        self,
        capability: str,
        name: str,
        cfg: InstanceConfig,
        features: frozenset[str] | None = None,
    ) -> DriverUnavailableError | None:
        """A :class:`DriverUnavailableError` (SANDLOOM-B037) with the install
        hint when ``cfg``'s driver is not registered and belongs to a driver
        package the kernel knows (:meth:`register_driver_package`): its entry
        point is absent, so the package is not installed. ``None`` for any
        other unregistered driver (an unknown name is no installation fact)."""
        known = self._driver_packages.get((capability, cfg.driver))
        if known is None:
            return None
        package, install = known
        if (capability, cfg.driver) in self._drivers and self._is_installed(package):
            return None
        return DriverUnavailableError(
            f"{capability}/{name}: driver {cfg.driver!r} needs the package "
            f"{package!r}, which is not installed",
            fix=f"{install}, or select an installed {capability} driver"
            f"{f' that advertises {sorted(features)}' if features else ''} in "
            f"INFRA[{capability!r}][{name!r}]",
            capability=capability,
            instance=name,
            driver=cfg.driver,
            missing=(package,),
            install=install,
        )

    def require_instances(self, required: Iterable[tuple[str, str]]) -> None:
        """Refuse (SANDLOOM-B037) every instance the process cannot start
        without that nothing selects, or whose driver cannot run here: the
        composition or ``INFRA`` must name a driver, the kernel has none of its
        own and never falls back to ``memory``. One error names every instance,
        what to select and what to install. Build-free."""
        problems: list[DriverUnavailableError] = []
        for capability, name in sorted(required):
            cfg = self._config.get(capability, {}).get(name)
            if cfg is None:
                problems.append(
                    DriverUnavailableError(
                        f"{capability}/{name}: no driver is selected",
                        fix=f"install a composition that selects one "
                        f"(`pip install 'sandloom[standard]'`), or name a "
                        f"driver in INFRA[{capability!r}][{name!r}], e.g. "
                        f'INFRA={{"{capability}": {{"{name}": {{"driver": '
                        '"<driver>"}}}}',
                        capability=capability,
                        instance=name,
                    )
                )
                continue
            absent = self._package_absent(capability, name, cfg)
            problem = absent or self._unavailable(capability, name)
            if problem is not None:
                problems.append(problem)
        if problems:
            first = problems[0]
            raise DriverUnavailableError(
                "The application cannot start: "
                + "; ".join(f"{p.reason} (fix: {p.fix})" for p in problems),
                fix="select a driver for each instance named and install what it "
                "needs; `sandloom explain SANDLOOM-B037`",
                capability=first.capability,
                instance=first.instance,
                driver=first.driver,
                missing=tuple(m for p in problems for m in p.missing),
                install=first.install,
            )

    def _import_failure(
        self,
        capability: str,
        name: str,
        cfg: InstanceConfig,
        registration: DriverRegistration,
        exc: ImportError,
    ) -> DriverUnavailableError:
        """Building the driver failed to import a dependency: the packages it
        names were found, a transitive one was not (or is broken)."""
        missing = (exc.name.split(".", 1)[0],) if exc.name else ()
        return DriverUnavailableError(
            f"{capability}/{name}: driver {cfg.driver!r} cannot be built, an "
            f"import failed: {exc}",
            fix=_install_fix(capability, name, registration, missing, None),
            capability=capability,
            instance=name,
            driver=cfg.driver,
            missing=missing,
            install=registration.install,
        )

    def use_kernel_instance(self, capability: str, name: str) -> None:
        """Mark a configured instance as used by the kernel of this process, so
        :meth:`aopen_all` and :meth:`health_all` open and probe it like a module
        requirement's (a module cannot name a kernel-only capability). An
        instance that is not configured is left alone."""
        if self._config.get(capability, {}).get(name) is not None:
            self._requested.add((capability, name))

    def in_use(self, capability: str, name: str = DEFAULT_INSTANCE) -> bool:
        """Whether something requires instance ``capability/name``.

        An instance ``INFRA`` names is in use (the operator selected it); so
        is a synthesized one the kernel uses (:data:`KERNEL_INSTANCES`) or a
        validated module requirement named. Any other synthesized instance is
        not opened, probed or listed as failing: nothing would use it. An
        instance that is not configured is not in use.
        """
        if self._config.get(capability, {}).get(name) is None:
            return False
        return not self._unused(capability, name)

    def _unused(self, capability: str, name: str) -> bool:
        """A configured, synthesized instance nothing requires: the one kind
        :meth:`aopen_all` and :meth:`health_all` skip."""
        cfg = self._config.get(capability, {}).get(name)
        return (
            cfg is not None
            and cfg.implicit
            and (capability, name) not in KERNEL_INSTANCES
            and (capability, name) not in self._requested
        )

    def set_config(self, config: InfraConfig) -> None:
        """Replace the configuration (clears any built providers).

        A configuration that names a removed driver is refused whole: the
        registry keeps the one it had."""
        _refuse_removed_drivers(
            cfg for instances in config.values() for cfg in instances.values()
        )
        self._config = {}
        for capability, instances in config.items():
            bucket = self._config.setdefault(capability, {})
            for name, cfg in instances.items():
                bucket[name] = cfg
        self._retired_providers.extend(self._providers.values())
        self._providers.clear()

    def configure_instance(self, config: InstanceConfig) -> None:
        """Add/override a single named instance (clears its cached provider)."""
        _refuse_removed_drivers([config])
        self._config.setdefault(config.capability, {})[config.name] = config
        retired = self._providers.pop((config.capability, config.name), None)
        if retired is not None:
            self._retired_providers.append(retired)

    def instance_config(
        self, capability: str, name: str = DEFAULT_INSTANCE
    ) -> InstanceConfig | None:
        """The configuration of one named instance, or ``None``."""
        return self._config.get(capability, {}).get(name)

    def snapshot_instance(
        self, capability: str, name: str = DEFAULT_INSTANCE
    ) -> InstanceSnapshot:
        """Capture one instance's configuration AND built provider (if any).

        Pair with :meth:`restore_instance` to reconfigure an instance
        temporarily (tests) and put back exactly what was there — including a
        lazily built provider, so its pools/clients survive.
        """
        return InstanceSnapshot(
            capability=capability,
            name=name,
            config=self.instance_config(capability, name),
            provider=self._providers.get((capability, name)),
        )

    def restore_instance(self, snapshot: InstanceSnapshot) -> InfraProvider | None:
        """Put back a :meth:`snapshot_instance`; returns the provider it displaced.

        The displaced provider (built while the instance was reconfigured) is
        NOT closed — the caller owns it (``await displaced.aclose()``) unless it
        is the snapshot's own provider.
        """
        key = (snapshot.capability, snapshot.name)
        displaced = self._providers.pop(key, None)
        if snapshot.config is not None:
            self._config.setdefault(snapshot.capability, {})[snapshot.name] = (
                snapshot.config
            )
        else:
            self._config.get(snapshot.capability, {}).pop(snapshot.name, None)
        if snapshot.provider is not None:
            self._providers[key] = snapshot.provider
        return None if displaced is snapshot.provider else displaced

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    def advertised_features(
        self, capability: str, name: str = DEFAULT_INSTANCE
    ) -> frozenset[str] | None:
        """The features instance ``capability/name`` advertises, build-free
        (driver declaration narrowed by the instance options); ``None`` when
        the instance or its driver is not configured (other checks say so)."""
        cfg = self._config.get(capability, {}).get(name)
        if cfg is None:
            return None
        registration = self._registration(capability, cfg.driver)
        if registration is None:
            return None
        return registration.features_for(cfg.options)

    def sql_dialects(
        self, capability: str, name: str = DEFAULT_INSTANCE
    ) -> tuple[str, frozenset[str]] | None:
        """``(driver, the SQL dialects it serves)`` for instance
        ``capability/name``, build-free; ``None`` when not configured."""
        cfg = self._config.get(capability, {}).get(name)
        if cfg is None:
            return None
        registration = self._drivers.get((capability, cfg.driver))
        if registration is None:
            return None
        return cfg.driver, registration.sql_dialects

    def instances(self, capability: str) -> list[str]:
        """Return configured instance names for a capability."""
        return sorted(self._config.get(capability, {}).keys())

    def declared_features(
        self, capability: str, instance: str = DEFAULT_INSTANCE
    ) -> frozenset[str] | None:
        """The features the configured ``instance`` advertises, build-free
        (its driver's declaration for its options, as the preflight checks
        them); ``None`` when the instance or its driver is not configured."""
        cfg = self._config.get(capability, {}).get(instance)
        if cfg is None:
            return None
        registration = self._registration(capability, cfg.driver)
        if registration is None:
            return None
        return registration.features_for(cfg.options)

    def provider(self, capability: str, name: str = DEFAULT_INSTANCE) -> InfraProvider:
        """Return (building+caching on first use) a provider instance."""
        cache_key = (capability, name)
        cached = self._providers.get(cache_key)
        if cached is not None:
            return cached

        with self._build_lock:
            # Re-check under the lock: another thread may have built it while we
            # waited.
            cached = self._providers.get(cache_key)
            if cached is not None:
                return cached

            instance_cfg = self._config.get(capability, {}).get(name)
            if instance_cfg is None:
                self._check_experimental_driver(capability)
                available = self.instances(capability) or ["<none>"]
                raise InstanceNotConfiguredError(
                    f"No '{capability}' instance named {name!r} is configured "
                    f"(available: {', '.join(available)})",
                    fix=f"add INFRA[{capability!r}][{name!r}] or request a "
                    "configured instance",
                )
            problem = self._not_runnable(capability, name, instance_cfg)
            if problem is not None:
                # A driver whose package is not installed: B037 with the
                # install hint, before a target is imported (which would only
                # fail with a generic load error).
                raise problem
            registration = self._registration(capability, instance_cfg.driver)
            if registration is None:
                self._check_experimental_driver(capability, instance_cfg.driver)
                known = sorted(d for (c, d) in self._drivers if c == capability)
                raise DriverNotRegisteredError(
                    f"No driver {instance_cfg.driver!r} registered for capability "
                    f"'{capability}' (known: {', '.join(known) or '<none>'})",
                    fix="install a driver package (an entry point "
                    f"'{capability}.{instance_cfg.driver}' in the 'sandloom.drivers' "
                    "group) or pick a known driver",
                )
            self._refuse_development_only(instance_cfg, registration)
            self._refuse_experimental_driver(instance_cfg, registration)
            self._validate_options(capability, instance_cfg, registration)
            factory = registration.factory
            if factory is None:  # pragma: no cover - every row ends with one
                raise DriverLoadError(
                    f"Driver {capability}/{instance_cfg.driver} has no factory",
                    fix="register the driver with its provider class",
                )
            try:
                provider = factory(instance_cfg)
            except ImportError as exc:
                raise self._import_failure(
                    capability, name, instance_cfg, registration, exc
                ) from exc
            # "Kernel-synthesized" is a development fact only: in production a
            # synthesized instance (the beat lease, the streams bus, both on the
            # composition's default driver) that shares the default cache's
            # Redis is as real a risk as a declared one, so its drivers word
            # the shared-endpoint message as a WARNING there.
            provider.implicit = instance_cfg.implicit and not settings.is_prod
            self._check_contract(capability, name, provider)
            if self._strict:
                self._check_declared_features(
                    name, instance_cfg, registration, provider
                )
            self._providers[cache_key] = provider
            logger.debug(
                "[infra] built %s/%s via driver %s",
                capability,
                name,
                instance_cfg.driver,
            )
            return provider

    def resolve(
        self,
        capability: str,
        name: str = DEFAULT_INSTANCE,
        *,
        features: frozenset[str] | None = None,
        contract: type | None = None,
        version: str | None = None,
    ) -> InfraProvider:
        """Resolve a provider, enforcing the contract and required features.

        Like :meth:`provider` but feature-aware: ``features`` must be both known
        to the capability spec and advertised by the resolved driver, else a
        :class:`FeatureUnsupportedError` with a concrete fix is raised. An
        explicit ``contract`` (beyond the spec's) is also checked. This is the
        path behind ``CoreFacade.require_capability`` and the module preflight.
        A ``version`` (from a ``CapabilityRef``) must match the spec's major.
        A kernel-only capability is refused (SANDLOOM-B041): the kernel
        reaches one through :meth:`provider`.
        """
        spec = self._capabilities.get(capability)
        _refuse_kernel_only(spec)
        if (
            version is not None
            and spec is not None
            and not versions_compatible(spec.version, version)
        ):
            raise FeatureUnsupportedError(
                f"Capability {capability!r} is {spec.version}, but this code was "
                f"written for {version}",
                fix="align the CapabilityRef with the capability's major version",
            )
        provider = self.provider(capability, name)
        if contract is not None and not isinstance(provider, contract):
            raise ProviderContractError(
                f"{capability}/{name} provider {type(provider).__name__} does "
                f"not implement {contract.__name__}",
                fix="use a driver whose provider implements the requested contract",
            )
        if features:
            self._check_features(capability, name, provider, features)
        return provider

    def validate_requirement(
        self,
        capability: str,
        instance: str = DEFAULT_INSTANCE,
        *,
        features: frozenset[str] | None = None,
        version: str | None = None,
    ) -> None:
        """Validate that a requirement CAN be satisfied — without building.

        Checks (no provider instantiated, no I/O): the capability has a spec,
        the version matches, an instance is configured, its driver is
        registered, the requested features are both defined by the spec and
        declared by the driver, and the packages the driver needs are
        installed (``find_spec``: nothing imported; a missing one raises
        :class:`DriverUnavailableError`, SANDLOOM-B037). Used by the module
        preflight so a bad plugin's ``factory`` can never run network code at
        startup. A requirement that passes marks its instance in use
        (:meth:`in_use`).
        """
        spec = self._capabilities.get(capability)
        if spec is None:
            raise UnknownCapabilityError(
                f"Capability {capability!r} is not registered",
                fix="install the package that declares it (the 'sandloom.capabilities' "
                "entry-point group) and configure it in INFRA",
            )
        _refuse_kernel_only(spec)
        if version is not None and not versions_compatible(spec.version, version):
            raise FeatureUnsupportedError(
                f"Capability {capability!r} is {spec.version}, but incompatible "
                f"version {version} was required (major versions differ)",
                fix="align the module's required version with the registered "
                "capability's MAJOR (same major is compatible)",
            )
        cfg = self._config.get(capability, {}).get(instance)
        if cfg is None:
            self._check_experimental_driver(capability)
            available = self.instances(capability) or ["<none>"]
            raise InstanceNotConfiguredError(
                f"No '{capability}' instance named {instance!r} is configured "
                f"(available: {', '.join(available)})",
                fix=f"add INFRA[{capability!r}][{instance!r}]",
            )
        absent = self._package_absent(capability, instance, cfg, features)
        if absent is not None:
            raise absent
        registration = self._drivers.get((capability, cfg.driver))
        if registration is None:
            self._check_experimental_driver(capability, cfg.driver)
            raise DriverNotRegisteredError(
                f"No driver {cfg.driver!r} registered for capability {capability!r}",
                fix="register the driver or pick a configured one",
            )
        # Before the features: advertising them loads the driver's class, and a
        # driver of a package that is not installed (a lazy install-hint row)
        # must be refused with the install hint, not a failed import.
        unavailable = self._unavailable(capability, instance, features=features)
        if unavailable is not None:
            raise unavailable
        if features:
            unknown = features - spec.features
            if unknown:
                raise FeatureUnsupportedError(
                    f"Capability {capability!r} (v{spec.version}) defines no "
                    f"feature(s) {spec.unknown_features_hint(unknown)}; known: "
                    f"{sorted(spec.features)}",
                    fix="drop the unknown feature(s) or upgrade the contract",
                )
            loaded = self._registration(capability, cfg.driver)
            advertised = (loaded or registration).features_for(cfg.options)
            missing = features - advertised
            if missing:
                raise FeatureUnsupportedError(
                    f"{capability}/{instance} driver {cfg.driver!r} declares "
                    f"{sorted(advertised)} but feature(s) "
                    f"{sorted(missing)} are required",
                    fix=f"configure {capability}/{instance} with a driver that "
                    f"supports {sorted(missing)}",
                )
        self._requested.add((capability, instance))

    def check_module_requirements(
        self, module: str, requirements: Iterable[CapabilityRequirement]
    ) -> list[tuple[CapabilityRequirement, Exception]]:
        """Validate a module's capability requirements, build-free.

        Every unmet REQUIRED requirement is refused at once with one
        :class:`CapabilityUnavailable` (SANDLOOM-B037) naming the module and,
        for each, the capability, the instance, its driver, the features asked
        for, what is missing and the fix. Optional requirements that cannot
        be met are returned for the caller to report; an unexpected error of
        a required one propagates as it is.
        """
        unmet: list[tuple[str, CapabilityError]] = []
        optional: list[tuple[CapabilityRequirement, Exception]] = []
        requirements = list(requirements)
        for req in requirements:
            # Required or optional: asking for one is the refusal (B041).
            _refuse_kernel_only(
                self._capabilities.get(req.capability), f"module {module!r}"
            )
        for req in requirements:
            try:
                self.validate_requirement(
                    req.capability,
                    req.instance,
                    features=req.features or None,
                    version=req.version,
                )
            except CapabilityError as exc:
                if req.required:
                    unmet.append((self._describe_unmet(req, exc), exc))
                else:
                    optional.append((req, exc))
            except Exception as exc:
                if req.required:
                    raise
                optional.append((req, exc))
        if unmet:
            error = _unavailable_class(tuple(type(exc) for _text, exc in unmet))
            raise error(
                f"module {module!r} requires infrastructure this composition "
                f"cannot serve [SANDLOOM-B037]: {'; '.join(t for t, _e in unmet)}",
                fix="install or select what each requirement names; "
                "`sandloom explain SANDLOOM-B037`",
            )
        return optional

    def _describe_unmet(self, req: CapabilityRequirement, exc: CapabilityError) -> str:
        cfg = self._config.get(req.capability, {}).get(req.instance)
        where = [f"instance {req.instance!r}"]
        if cfg is not None:
            where.append(f"driver {cfg.driver!r}")
        if req.features:
            where.append(f"features {sorted(req.features)}")
        fix = f"; fix: {exc.fix}" if exc.fix else ""
        return f"capability {req.capability!r} ({', '.join(where)}): {exc.reason}{fix}"

    # ------------------------------------------------------------------ #
    # Contract / feature / config validation
    # ------------------------------------------------------------------ #
    def _check_contract(
        self, capability: str, name: str, provider: InfraProvider
    ) -> None:
        spec = self._capabilities.get(capability)
        if spec is None:
            return  # lenient registry: no spec registered → no contract check
        if not isinstance(provider, spec.provider_contract):
            raise ProviderContractError(
                f"Driver for {capability}/{name} returned "
                f"{type(provider).__name__}, which does not implement the "
                f"{spec.provider_contract.__name__} contract for capability "
                f"{capability!r}",
                fix="fix the driver to subclass the capability's provider contract",
            )

    @staticmethod
    def _check_declared_features(
        name: str,
        cfg: InstanceConfig,
        registration: DriverRegistration,
        provider: InfraProvider,
    ) -> None:
        """The preflight reads the declaration, a request reads the instance: they
        must agree, or a requirement passes at startup and fails on first use."""
        declared = registration.features_for(cfg.options)
        actual = provider.features()
        if declared != actual:
            raise ProviderContractError(
                f"Driver {cfg.capability}/{cfg.driver} declares features "
                f"{sorted(declared)} but instance {name!r} reports {sorted(actual)}",
                fix="declare features once, in supported_features (or features_for "
                "when they depend on an option), and do not override features()",
            )

    def _check_features(
        self,
        capability: str,
        name: str,
        provider: InfraProvider,
        required: frozenset[str],
    ) -> None:
        spec = self._capabilities.get(capability)
        if spec is not None:
            unknown = required - spec.features
            if unknown:
                raise FeatureUnsupportedError(
                    f"Capability {capability!r} (v{spec.version}) defines no "
                    f"feature(s) {spec.unknown_features_hint(unknown)}; known: "
                    f"{sorted(spec.features)}",
                    fix="drop the unknown feature(s) or upgrade the capability "
                    "contract",
                )
        supported = provider.features()
        missing = required - supported
        if missing:
            raise FeatureUnsupportedError(
                f"{capability}/{name} driver {provider.driver!r} supports "
                f"{sorted(supported)} but feature(s) {sorted(missing)} are "
                "required",
                fix=f"configure {capability}/{name} with a driver that supports "
                f"{sorted(missing)}",
            )

    @staticmethod
    def _refuse_development_only(
        cfg: InstanceConfig, registration: DriverRegistration
    ) -> None:
        """A ``development_only`` driver is refused in production, with no
        accepted risk to lift it (spec 2.3, SANDLOOM-B022)."""
        provider_class = registration.provider_class
        if (
            provider_class is None
            or not provider_class.development_only
            or not settings.is_prod
        ):
            return
        raise DevelopmentOnlyDriverRefused(
            f"{cfg.capability}/{cfg.name}: driver {cfg.driver!r} is for development "
            "and tests only, and this is production",
            fix=f"select a production {cfg.capability} driver in "
            f"INFRA[{cfg.capability!r}][{cfg.name!r}]",
        )

    @staticmethod
    def _refuse_experimental_driver(
        cfg: InstanceConfig, registration: DriverRegistration
    ) -> None:
        """An ``experimental`` driver is refused in production unless the risk
        ``experimental_driver`` is accepted (spec 2.5, SANDLOOM-B023). The boot
        check covers what is configured at startup; this one covers a driver
        registered or an instance configured after it. Runs after
        :meth:`_refuse_development_only`, so a development-only driver always
        gets B022."""
        if (
            registration.tier != "experimental"
            or not settings.is_prod
            or settings.accepts_risk("experimental_driver")
        ):
            return
        raise ExperimentalDriverRefused(
            "Experimental drivers are configured in production: "
            f"{cfg.capability}/{cfg.name} ({cfg.driver}) "
            "(sandloom explain SANDLOOM-B023)",
            fix="select a beta or stable driver in INFRA, or accept the risk with "
            'SANDLOOM_ACCEPTED_RISKS=["experimental_driver"]',
        )

    def _validate_options(
        self, capability: str, cfg: InstanceConfig, registration: DriverRegistration
    ) -> None:
        # Driver-level schema wins (drivers of one capability take different
        # config); the capability schema covers only common fields.
        spec = self._capabilities.get(capability)
        schema = registration.config_schema or (
            spec.config_schema if spec is not None else None
        )
        if schema is None:
            return
        try:
            schema(**cfg.options)
        except Exception as exc:  # normalize to a diagnostic
            raise ConfigSchemaError(
                f"Invalid options for {capability}/{cfg.name} "
                f"(driver={cfg.driver!r}): {exc}",
                fix=f"correct INFRA[{capability!r}][{cfg.name!r}] to match the "
                f"{schema.__name__} schema",
            ) from exc

    def all_built(self) -> list[InfraProvider]:
        """Return providers that have actually been instantiated."""
        return list(self._providers.values())

    def validate(self) -> None:
        """Fail fast at startup on any unbuildable infrastructure instance.

        Called at startup so a misconfigured INFRA entry aborts the boot
        instead of surfacing only when that instance is first resolved (perhaps
        mid-request in prod). Two checks, both build-free (no provider built, no
        engine/client/pool created, no I/O):

        * the instance's driver is registered,
        * its options satisfy the driver/capability config schema, and
        * for an instance ``INFRA`` names, the packages its driver needs are
          installed (``find_spec``, nothing imported): the operator selected
          it, so a driver that cannot run is refused (SANDLOOM-B037). A
          synthesized default is checked when a requirement names it.

        Options are only validated for instances whose driver is registered and
        carries a schema; an instance without a schema is skipped (current
        behaviour) — :meth:`_validate_options` just instantiates the schema.
        """
        missing: list[str] = []
        unavailable: list[DriverUnavailableError] = []
        for capability, instances in self._config.items():
            for name, cfg in instances.items():
                self._validate_instance(capability, name, cfg, missing, unavailable)
        if unavailable:
            first = unavailable[0]
            raise DriverUnavailableError(
                "An instance selected in INFRA has a driver that cannot run here: "
                + "; ".join(f"{u.reason} (fix: {u.fix})" for u in unavailable),
                fix="install what the driver needs, or select an installed "
                "driver in INFRA; `sandloom explain SANDLOOM-B037`",
                capability=first.capability,
                instance=first.instance,
                driver=first.driver,
                missing=tuple(n for u in unavailable for n in u.missing),
                install=first.install,
            )
        if missing:
            raise DriverNotRegisteredError(
                "Configured infrastructure instances have no registered driver: "
                + ", ".join(sorted(missing)),
                fix="install the driver package (an entry point "
                "'<capability>.<driver>' in the 'sandloom.drivers' group) or pick "
                "a known driver in INFRA",
            )

    def _validate_instance(
        self,
        capability: str,
        name: str,
        cfg: InstanceConfig,
        missing: list[str],
        unavailable: list[DriverUnavailableError],
    ) -> None:
        """One instance's build-free checks for :meth:`validate`. A driver of a
        package that is not installed is not imported: an instance INFRA names
        is refused (B037); a synthesized default is resolved when first used."""
        if self._target_package_missing(capability, name):
            self._refuse_unless_implicit(capability, name, cfg, unavailable)
            return
        registration = self._registration(capability, cfg.driver)
        if registration is None:
            absent = self._package_absent(capability, name, cfg)
            if absent is None:
                missing.append(f"{capability}/{name} (driver={cfg.driver!r})")
            elif not cfg.implicit:
                unavailable.append(absent)
            return
        # Build-free: instantiates the config schema only — never the
        # provider. Raises ConfigSchemaError with an actionable fix.
        if not cfg.implicit:
            self._refuse_development_only(cfg, registration)
        self._validate_options(capability, cfg, registration)
        if not cfg.implicit:
            problem = self._unavailable(capability, name)
            if problem is not None:
                unavailable.append(problem)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def health_all(
        self, *, skip: frozenset[str] = frozenset()
    ) -> dict[str, HealthResult]:
        """Health-probe every instance in use (builds them as needed).

        Keys are ``"<capability>:<name>"``. Every probe has its own bounded
        timeout; an unavailable instance cannot serially delay every probe.
        A synthesized instance nothing requires (:meth:`in_use`) is neither
        built nor probed; :meth:`describe` lists it as ``unused``. The
        capabilities in ``skip`` are not probed at all (``/health`` skips the
        taskqueue, whose reachability only readiness reads).
        """

        await self._close_retired()

        async def probe(capability: str, name: str) -> tuple[str, HealthResult]:
            try:
                provider = self.provider(capability, name)
                result = await asyncio.wait_for(
                    provider.health(), timeout=settings.health_check_timeout_seconds
                )
                result = _vouched_health(result)
            except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
                # The type only: an exception text can carry a URL with
                # credentials or a key (spec 2.4, health redaction).
                result = HealthResult(
                    status="fail", details=health_failure_details(exc)
                )
            return f"{capability}:{name}", result

        return dict(
            await asyncio.gather(
                *(
                    probe(capability, name)
                    for capability, instances in self._config.items()
                    if capability not in skip
                    for name in instances
                    if not self._unused(capability, name)
                )
            )
        )

    async def aopen_all(self, *, strict: bool = False) -> None:
        """Eagerly build and ``aopen()`` every configured instance.

        Symmetric to :meth:`aclose_all`, invoked at startup so a driver that
        must warm a connection pool / handshake a client up front does so before
        the process serves traffic, instead of lazily on the first request. The
        event bus is opened like every other capability.

        ``strict`` (the production default, wired in ``create_app``) turns a
        per-instance open failure into an aborted startup: a configured database
        / cache / object store the process cannot reach raises
        :class:`InfraStartupError` rather than being logged and skipped, so the
        app never starts serving traffic it will only fail on first use. With
        ``strict=False`` (dev) failures are logged and skipped (the lazy build
        path retries on first use) — except an exception carrying
        ``fatal_at_startup = True`` (a security misconfiguration, e.g. an
        application role that could escape tenant RLS), which aborts startup
        in every environment.

        A synthesized instance nothing requires (:meth:`in_use`) is skipped:
        it is not built, so a driver nobody selected (Celery for an app
        without task modules) can neither fail startup nor open a connection.
        A driver whose packages are missing, or whose build fails to import a
        dependency, fails as SANDLOOM-B037 naming the instance, the driver,
        the missing package and the install hint.
        """
        failures: list[str] = []
        unavailable = False
        await self._close_retired()
        # The order is INFRA's, except that a driver declaring ``opens_after``
        # (a url-less durable bus runs on the default cache client, which the
        # cache instance configures when it is built) opens after those
        # capabilities, whatever order INFRA lists them in.
        for capability, name in self._open_order():
            if self._unused(capability, name):
                logger.debug(
                    "[infra] %s/%s is a synthesized default nothing requires: "
                    "not opened",
                    capability,
                    name,
                )
                continue
            failure = await self._open_one(capability, name, strict=strict)
            if failure is not None:
                failures.append(failure[0])
                unavailable = unavailable or failure[1]
        if failures:
            raise InfraStartupError(
                "Infrastructure failed to open at startup: " + "; ".join(failures),
                fix=(
                    "install what each SANDLOOM-B037 instance names; "
                    if unavailable
                    else ""
                )
                + "check the DSN/credentials/reachability of the failing "
                "instance(s); the process will not serve traffic until every "
                "configured instance opens (strict startup is on in production)",
                code="SANDLOOM-B037" if unavailable else None,
            )

    async def _open_one(
        self, capability: str, name: str, *, strict: bool
    ) -> tuple[str, bool] | None:
        """Build and open one instance for :meth:`aopen_all`: ``None`` when
        it opened or its failure is only logged, else ``(the failure, whether
        the driver cannot run here)``."""
        try:
            problem = self._unavailable(capability, name)
            if problem is not None:
                raise problem
            provider = self.provider(capability, name)
            await provider.aopen()
        except DriverUnavailableError as exc:
            # An installation fact, not reachability: the message says what
            # to install, a traceback adds nothing.
            if not strict:
                logger.warning("[infra] %s", exc)
                return None
            logger.error("[infra] %s", exc)
            return f"{exc.reason} [SANDLOOM-B037] ({exc.fix})", True
        except Exception as exc:
            # A security misconfiguration (e.g. an application role that could
            # escape tenant RLS) aborts startup in every environment, not only
            # under strict (production) startup.
            if not (strict or getattr(exc, "fatal_at_startup", False)):
                logger.warning(
                    "[infra] error opening %s/%s", capability, name, exc_info=True
                )
                return None
            logger.error(
                "[infra] failed to open %s/%s", capability, name, exc_info=True
            )
            return f"{capability}/{name}: {exc}", False
        return None

    async def aclose_all(self) -> None:
        """Close every built provider (best-effort, never raises)."""
        providers = list(self._providers.values())
        self._providers.clear()
        self._retired_providers.extend(providers)
        await self._close_retired()

    async def _close_retired(self) -> None:
        retired, self._retired_providers = self._retired_providers, []
        for provider in reversed(retired):
            try:
                await provider.aclose()
            except Exception:
                logger.warning(
                    "[infra] error closing %s/%s",
                    provider.capability,
                    provider.name,
                    exc_info=True,
                )

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for diagnostics endpoints.

        Each instance says whether it was synthesized (``implicit``), whether
        something requires it (``in_use``; a synthesized one nothing requires
        carries ``status: "unused"`` and is never probed) and, build-free,
        which packages its driver lacks (``missing_packages``).
        """
        return {
            "capabilities": {
                spec.name: {
                    "version": spec.version,
                    "major": spec.major,
                    "contract": spec.provider_contract.__name__,
                    "features": sorted(spec.features),
                    "description": spec.description,
                    "stability": spec.stability,
                    "drivers": sorted(d for (c, d) in self._drivers if c == spec.name),
                }
                for spec in self.capabilities()
            },
            "drivers": sorted(f"{c}/{d}" for (c, d) in self._drivers),
            "driver_tiers": {
                f"{c}/{d}": registration.tier
                for (c, d), registration in sorted(self._drivers.items())
            },
            "installed_driver_packages": installed_plugin_names(),
            "instances": {
                cap: {
                    name: {
                        "driver": cfg.driver,
                        "tier": self.driver_tier(cap, cfg.driver),
                        "built": (cap, name) in self._providers,
                        "implicit": cfg.implicit,
                        "in_use": self.in_use(cap, name),
                        **({} if self.in_use(cap, name) else {"status": "unused"}),
                        **(
                            {"missing_packages": list(missing)}
                            if (missing := self.missing_packages(cap, name))
                            else {}
                        ),
                        **(
                            {
                                "features": sorted(
                                    self._providers[(cap, name)].features()
                                )
                            }
                            if (cap, name) in self._providers
                            else {}
                        ),
                    }
                    for name, cfg in insts.items()
                }
                for cap, insts in self._config.items()
            },
        }


def _plugins(group: str, name: str) -> list[metadata.EntryPoint]:
    return list(metadata.entry_points().select(group=group, name=name))


def _load_plugin(entry: metadata.EntryPoint, what: str) -> Any:
    try:
        return entry.load()
    except Exception as exc:
        raise DriverLoadError(
            f"{what} ({entry.value}) failed to import: {exc!r}",
            fix="fix or reinstall the package that publishes it, or select another "
            "one in INFRA",
        ) from exc


def _one_plugin(group: str, name: str) -> metadata.EntryPoint | None:
    found = _plugins(group, name)
    if len(found) > 1:
        owners = sorted(e.dist.name if e.dist else e.value for e in found)
        raise DriverConflictError(
            f"{name!r} in the {group!r} entry-point group has several providers: "
            f"{', '.join(owners)}",
            fix="uninstall all but one of these packages",
        )
    return found[0] if found else None


def _load_capability(registry: InfraRegistry, capability: str) -> None:
    entry = _one_plugin(CAPABILITIES_GROUP, capability)
    if entry is None:
        return
    if registry.capability_spec(capability) is not None:
        raise DriverConflictError(
            f"Capability {capability!r} is built in, and {entry.value} declares it too",
            fix="rename the capability in that package",
        )
    spec = _load_plugin(entry, f"Capability {capability!r}")
    if not isinstance(spec, CapabilitySpec) or spec.name != capability:
        raise DriverLoadError(
            f"{entry.value} is not the CapabilitySpec of {capability!r}",
            fix="point the entry point at a CapabilitySpec with this name",
        )
    registry.register_capability(spec)


def _load_driver(registry: InfraRegistry, cfg: InstanceConfig) -> None:
    key = f"{cfg.capability}.{cfg.driver}"
    entry = _one_plugin(DRIVERS_GROUP, key)
    if entry is None:
        return
    if registry.has_driver(cfg.capability, cfg.driver):
        if registry.driver_target(cfg.capability, cfg.driver) == entry.value:
            # The kernel's pre-registered row names this very entry point (a
            # driver package it knows): one driver, registered lazily already.
            return
        raise DriverConflictError(
            f"Driver {key!r} is built in, and {entry.value} publishes it too",
            fix="rename the driver in that package: another behaviour is another "
            "driver name, selected in INFRA",
        )
    driver = _load_plugin(entry, f"Driver {key!r}")
    provider_class = check_driver_class(
        key, driver, registry.capability_spec(cfg.capability)
    )
    registry.register_driver(
        cfg.capability,
        cfg.driver,
        provider_class,
        requires=provider_class.required_imports,
        install=provider_class.install_hint,
        tier=provider_class.tier,
    )


#: A health detail string the registry passes on: one token (an error type
#: name, a driver name, ``no-pong``, ``redis:<run_id>``). Anything else is
#: free text that may carry a URL, a host with credentials or a key.
_HEALTH_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
_REDACTED = "<redacted>"


def _vouched_detail(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if _HEALTH_TOKEN.fullmatch(value) else _REDACTED
    if isinstance(value, Mapping):
        return _vouched_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_vouched_detail(v) for v in value]
    return _REDACTED


def _vouched_mapping(value: Mapping[Any, Any]) -> dict[str, object]:
    """A details mapping whose keys pass as its values do: an entry whose key
    is not one token (a URL, a host with credentials) becomes
    ``"<redacted>": "<redacted>"``, its value unread."""
    vouched: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str) and _HEALTH_TOKEN.fullmatch(key):
            vouched[key] = _vouched_detail(item)
        else:
            vouched[_REDACTED] = _REDACTED
    return vouched


def _vouched_health(result: HealthResult) -> HealthResult:
    """A driver's health result with only what the registry can vouch for in
    its ``details`` (spec 2.4): numbers, booleans and single-token strings
    pass, under single-token keys; any other value becomes ``"<redacted>"``,
    any other key's entry ``"<redacted>": "<redacted>"``."""
    if not result.details:
        return result
    return dataclasses.replace(result, details=_vouched_mapping(result.details))


def check_driver_class(
    key: str, driver: object, spec: CapabilitySpec | None
) -> type[InfraProvider]:
    """``driver`` as a provider class, checked as every driver is, built-ins
    included (SANDLOOM-B022): a subclass of the capability's contract that
    implements every abstract method, states the integer ``contract_version``
    of the capability's major, declares only the spec's features and valid
    dependency metadata."""
    contract = spec.provider_contract if spec else InfraProvider
    if not (isinstance(driver, type) and issubclass(driver, contract)):
        raise DriverLoadError(
            f"Driver {key!r} ({driver!r}) is not a subclass of {contract.__name__}",
            fix=f"point the entry point at a {contract.__name__} subclass",
        )
    missing = sorted(
        name
        for name in inspect.getattr_static(contract, "__abstractmethods__", ())
        if not callable(getattr(driver, name, None))
        or name in getattr(driver, "__abstractmethods__", ())
    )
    if missing:
        raise DriverLoadError(
            f"Driver {key!r} does not implement {', '.join(missing)}",
            fix="implement every abstract method of the contract",
        )
    major = spec.major if spec is not None else None
    if spec is not None and major is None:
        raise DriverLoadError(
            f"Driver {key!r} cannot load: capability {spec.name!r} has no integer "
            f"major (version {spec.version!r}, no major stated), so no "
            "contract_version can be checked against it",
            fix="give the CapabilitySpec a version such as 'v1' or an explicit major",
        )
    written_for = getattr(driver, "contract_version", None)
    example = major if major is not None else 1
    if written_for is None:
        raise DriverLoadError(
            f"Driver {key!r} declares no contract_version",
            fix=f"add a class attribute, for example contract_version = {example}",
        )
    if not isinstance(written_for, int) or isinstance(written_for, bool):
        raise DriverLoadError(
            f"Driver {key!r} declares contract_version {written_for!r}: it is the "
            "integer major of the capability contract (a string such as 'v1' is "
            "a driver written before contract majors were integers)",
            fix=f"install a version of the driver written for contract_version = "
            f"{example}",
        )
    if spec is not None and major is not None and written_for != major:
        raise DriverLoadError(
            f"Driver {key!r} was written for contract_version {written_for}, and "
            f"the capability {spec.name!r} is {major}",
            fix="install a version of the driver for this contract",
        )
    provider_class = cast("type[InfraProvider]", driver)
    required_imports = provider_class.required_imports
    install_hint = provider_class.install_hint
    if (
        not isinstance(required_imports, tuple)
        or any(
            not isinstance(name, str) or not name.isidentifier()
            for name in required_imports
        )
        or not isinstance(install_hint, str)
        or not isinstance(provider_class.development_only, bool)
        or provider_class.tier not in DRIVER_TIERS
    ):
        raise DriverLoadError(
            f"Driver {key!r} declares invalid dependency metadata",
            fix="set required_imports to a tuple of top-level import names, "
            "install_hint to a string, development_only to a bool and tier to one "
            f"of {', '.join(DRIVER_TIERS)}",
        )
    _check_opens_after(key, provider_class)
    if spec is not None:
        unknown = frozenset(provider_class.supported_features) - spec.features
        if unknown:
            raise FeatureUnsupportedError(
                f"Driver {key!r} declares feature(s) "
                f"{spec.unknown_features_hint(unknown)} not defined by capability "
                f"{spec.name!r} (known: {sorted(spec.features)})",
                fix="add the feature to the CapabilitySpec or drop it",
            )
    return provider_class


def _check_opens_after(key: str, provider_class: type[InfraProvider]) -> None:
    """``opens_after`` is a tuple of capability names other than the driver's
    own (SANDLOOM-B022): a bare string iterates by character and would order
    nothing, silently, and a capability waiting for itself would be dropped."""
    opens_after = provider_class.opens_after
    if not isinstance(opens_after, tuple) or any(
        not isinstance(name, str) or not name.isidentifier() for name in opens_after
    ):
        raise DriverLoadError(
            f"Driver {key!r} declares opens_after {opens_after!r}: it is a tuple "
            "of capability names",
            fix='set opens_after to a tuple such as ("cache",), never a bare string',
        )
    if key.partition(".")[0] in opens_after:
        raise DriverLoadError(
            f"Driver {key!r} declares opens_after {opens_after!r}, which names its "
            "own capability: instances of one capability open in INFRA's order",
            fix="name the other capabilities this driver reads state from",
        )


def _import_target(
    capability: str, driver: str, registration: DriverRegistration
) -> object:
    """Import a pre-registered ``"module:Class"`` target (SANDLOOM-B022 when
    it cannot be: a slice not delivered yet names itself)."""
    target = registration.target or ""
    module_name, _, attribute = target.partition(":")
    key = f"{capability}.{driver}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name and registration.delivered_by:
            raise DriverLoadError(
                f"Driver {key!r} is pre-registered for {registration.delivered_by}, "
                f"which delivers {module_name}; this build does not have it yet",
                fix=f"select another {capability} driver in INFRA[{capability!r}] "
                f"until {registration.delivered_by} lands",
            ) from exc
        raise DriverLoadError(
            f"Driver {key!r} ({target}) failed to import: {exc!r}",
            fix="fix the installation, or select another driver in INFRA",
        ) from exc
    except Exception as exc:
        raise DriverLoadError(
            f"Driver {key!r} ({target}) failed to import: {exc!r}",
            fix="fix the installation, or select another driver in INFRA",
        ) from exc
    found = getattr(module, attribute, None)
    if found is None and registration.delivered_by:
        raise DriverLoadError(
            f"Driver {key!r} is pre-registered for {registration.delivered_by}, "
            f"which delivers {target}; this build does not have it yet",
            fix=f"select another {capability} driver in INFRA[{capability!r}] "
            f"until {registration.delivered_by} lands",
        )
    return found


def load_selected_plugins(registry: InfraRegistry, config: InfraConfig) -> None:
    """Import only the driver packages ``config`` selects, by name.

    No order to depend on, and an unselected package runs no code. A selected
    name with two providers, or one that cannot be used, stops startup: it could
    not serve anyway, and the operator sees the cause.
    """
    for capability, instances in config.items():
        _load_capability(registry, capability)
        # One driver can back several instances: load each name once.
        for driver in {cfg.driver: cfg for cfg in instances.values()}.values():
            _load_driver(registry, driver)


def installed_plugin_names() -> list[str]:
    """Every driver name installed, read from metadata without importing it."""
    return sorted(e.name for e in metadata.entry_points().select(group=DRIVERS_GROUP))


def _declared_class(capability: str, driver: str) -> type[InfraProvider] | None:
    """The provider class an installed package publishes for ``capability`` /
    ``driver`` through its entry point, without a registry: for the readers of a
    configuration that run in a process which builds none. ``None`` when no
    package publishes it (a driver of the kernel itself included); a package
    that cannot be imported, or publishes it twice, raises as the registry does."""
    key = f"{capability}.{driver}"
    entry = _one_plugin(DRIVERS_GROUP, key)
    if entry is None:
        return None
    return check_driver_class(key, _load_plugin(entry, f"Driver {key!r}"), None)


def declared_url_options(
    selection: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, str]:
    """``{"<capability>.<driver>": url option}`` for every driver ``selection``
    (``{capability: {instance: {"driver": ...}}}``) names whose installed package
    declares one (``InfraProvider.url_option``): which instances connect to a
    server by url, read from the drivers and never from a table of the kernel.
    A driver with no package (a kernel driver, or one not installed) and one that
    declares none are left out; a package that fails to load raises as the
    registry does."""
    found: dict[str, str] = {}
    for capability, instances in selection.items():
        for name, spec in instances.items():
            if not isinstance(spec, Mapping):
                raise TypeError(
                    f"the selection of {capability}.{name} must be a mapping of "
                    f"options, not {type(spec).__name__}"
                )
            driver = spec.get("driver")
            key = f"{capability}.{driver}"
            if not isinstance(driver, str) or key in found:
                continue
            provider_class = _declared_class(capability, driver)
            if provider_class is not None and provider_class.url_option is not None:
                found[key] = provider_class.url_option
    return found


@dataclass(frozen=True, slots=True)
class ConfiguredRetention:
    """What :func:`configured_stream_retention` found: ``seconds`` (``0``:
    never trimmed by age), or ``None`` with the ``reason`` it cannot be
    determined, worded for the operator (it names what to install or change)."""

    seconds: int | None
    reason: str | None = None


def _known_driver_install(capability: str, driver: str) -> str | None:
    """The install command of a driver of a package the kernel knows by name
    (``KNOWN_DRIVER_PACKAGES``), ``None`` for any other driver. Reached by name:
    the bootstrap sits above the infra layer (ADR-0007), and only the failure
    path of a configuration read needs it."""
    bootstrap = importlib.import_module("sandloom.core.drivers.bootstrap")
    known = bootstrap.KNOWN_DRIVER_PACKAGES.get((capability, driver))
    return None if known is None else str(known[1])


def configured_stream_retention(
    settings: Any, instance: str = DEFAULT_INSTANCE
) -> ConfiguredRetention:
    """The age bound the eventbus ``instance`` is CONFIGURED with
    (``seconds``; ``0``: entries are never trimmed by age): its
    ``retention_seconds`` option, else the default its driver's
    ``options_schema`` gives that option (a ``streams`` bus that leaves it out
    has seven days).

    A pure configuration read: no bus is built and no process's live bus is
    asked, so a tool running in another process (a CLI with a volatile bus of
    its own) learns the retention of the deployment's bus. ``seconds`` is
    ``None``, with the ``reason``, when it cannot be determined: the instance
    is not configured; it states an invalid value; its driver's package is not
    installed in this process (the reason names the package to install) or is
    installed but fails to load; or the driver has no ``retention_seconds``
    option with a default (a volatile ``memory`` / ``broadcast`` bus keeps no
    stream). A driver package that is missing or fails to load never raises
    from here (a read-only probe, ``check``, ``doctor`` or purge, must still say
    why); a malformed ``INFRA`` still raises ``InfraConfigError``, which
    :func:`resolved_eventbus` refuses with its own message.
    """
    found = resolved_eventbus(settings).get(instance)
    if found is None:
        return ConfiguredRetention(
            None, f"no eventbus instance {instance!r} is configured"
        )
    value = found.options.get("retention_seconds")
    if value is None:
        driver = found.driver
        try:
            provider_class = _declared_class("eventbus", driver)
        except (DriverLoadError, DriverConflictError) as exc:
            return ConfiguredRetention(
                None,
                f"the {driver!r} event bus driver is installed but cannot be "
                f"loaded ({exc}): fix the installation",
            )
        if provider_class is None:
            install = _known_driver_install("eventbus", driver)
            if install is not None:
                return ConfiguredRetention(
                    None,
                    f"the {driver!r} event bus is configured but its driver "
                    f"package is not installed in this process ({install}): "
                    "install it where this command runs",
                )
        schema = provider_class.options_schema if provider_class else None
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            declared = schema.model_fields.get("retention_seconds")
            value = None if declared is None else declared.get_default()
        if value is None or not isinstance(value, int):
            return ConfiguredRetention(
                None,
                f"the {driver!r} event bus has no retention_seconds option with "
                "a default (a volatile bus keeps no stream): select the streams "
                "bus (EVENT_BUS_BACKEND=streams) and set "
                "INFRA.eventbus.default.retention_seconds",
            )
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return ConfiguredRetention(
            None,
            f"retention_seconds of eventbus {instance!r} is {value!r}: it must "
            "be a non-negative integer",
        )
    return ConfiguredRetention(value)


def configured_stream_retention_seconds(
    settings: Any, instance: str = DEFAULT_INSTANCE
) -> int | None:
    """:func:`configured_stream_retention`'s ``seconds``: ``None`` when the
    retention cannot be determined (it does not say why)."""
    return configured_stream_retention(settings, instance).seconds


__all__ = [
    "ConfiguredRetention",
    "configured_stream_retention",
    "configured_stream_retention_seconds",
    "versions_compatible",
    "check_driver_class",
    "InfraRegistry",
    "InstanceSnapshot",
    "InfraRegistryError",
    "CapabilityError",
    "UnknownCapabilityError",
    "CapabilityConflictError",
    "ProviderContractError",
    "DatabaseFeatureMissing",
    "FeatureUnsupportedError",
    "InstanceNotConfiguredError",
    "DriverNotRegisteredError",
    "DriverUnavailableError",
    "CapabilityUnavailable",
    "ConfigSchemaError",
    "ExperimentalCapabilityError",
    "TransactionalOutboxUnsupportedError",
    "DriverFactory",
    "DriverRegistration",
    "CAPABILITIES_GROUP",
    "DRIVERS_GROUP",
    "DriverConflictError",
    "DriverLoadError",
    "DevelopmentOnlyDriverRefused",
    "ExperimentalCapabilityRefused",
    "ExperimentalDriverRefused",
    "installed_plugin_names",
    "load_selected_plugins",
]
