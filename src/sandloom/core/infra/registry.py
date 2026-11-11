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
import importlib.util
import inspect
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Any, cast

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.capabilities import CapabilitySpec
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    KERNEL_INSTANCES,
    InfraConfig,
    InstanceConfig,
)
from sandloom.core.infra.contracts import InfraProvider

if TYPE_CHECKING:
    from sandloom.core.descriptors import CapabilityRequirement

logger = logging.getLogger("sandloom.core.infra")

#: Entry-point groups: ``"<capability>.<driver>" = "pkg.driver:Class"`` and
#: ``"<capability>" = "pkg.contracts:SPEC"``.
DRIVERS_GROUP = "sandloom.drivers"
CAPABILITIES_GROUP = "sandloom.capabilities"

#: A driver factory turns an instance config into a live provider.
DriverFactory = Callable[[InstanceConfig], InfraProvider]


def _major(version: str) -> int | None:
    """Return the MAJOR component of a capability version string, or None.

    Accepts ``"v1"``, ``"v1.2"`` or a bare integer (``"1"``); returns the
    leading integer after an optional ``v`` prefix. Returns ``None`` when the
    string is unparseable — callers then fall back to exact-string equality so
    an odd version scheme can never be silently treated as compatible.
    """
    head = version.split(".", 1)[0].strip()
    if head[:1] in ("v", "V"):
        head = head[1:]
    try:
        return int(head)
    except ValueError:
        return None


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
    req_major = _major(required)
    reg_major = _major(registered)
    if req_major is None or reg_major is None:
        return registered == required
    return req_major == reg_major


@dataclass(frozen=True)
class DriverRegistration:
    """A registered driver: its factory plus build-free metadata.

    ``features`` and ``config_schema`` are declared at registration so the
    registry can preflight a requirement (and validate options) WITHOUT building
    the provider — no engine/client/pool created, no network I/O. They are
    driver-level: two drivers of the same capability may support different
    features and take different config.
    """

    factory: DriverFactory
    features: frozenset[str] = field(default_factory=frozenset)
    config_schema: type[Any] | None = None
    # Optional per-instance narrowing: given an instance's options, the subset
    # of ``features`` that instance actually advertises (e.g. ``tenant_rls``
    # only when the option is on). Still build-free — options only, no I/O.
    instance_features: Callable[[Mapping[str, Any]], frozenset[str]] | None = None
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

    def features_for(self, options: Mapping[str, Any]) -> frozenset[str]:
        if self.instance_features is None:
            return self.features
        return frozenset(self.instance_features(options)) & self.features


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


class InfraError(RuntimeError):
    """Raised on registry misconfiguration (unknown driver/instance)."""


class InfraStartupError(InfraError):
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


class CapabilityError(InfraError):
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


class ExperimentalCapabilityRefused(CapabilityError):
    """An experimental capability configured in production (SANDLOOM-B023)."""

    sandloom_code = "SANDLOOM-B023"


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


class InfraRegistry:
    """Holds drivers + configuration and resolves provider instances."""

    def __init__(
        self, config: InfraConfig | None = None, *, strict_capabilities: bool = False
    ) -> None:
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
        factory: DriverFactory,
        *,
        features: frozenset[str] | set[str] | None = None,
        config_schema: type[Any] | None = None,
        instance_features: Callable[[Mapping[str, Any]], frozenset[str]] | None = None,
        capability_version: str | None = None,
        sql_dialects: frozenset[str] | set[str] | None = None,
        requires: Iterable[str] = (),
        install: str = "",
    ) -> None:
        """Register a driver for a capability (idempotent override).

        ``features``/``config_schema`` are build-free metadata (see
        :class:`DriverRegistration`). In strict mode the capability must already
        have a registered spec, and any declared features must be a subset of
        the spec's — so a driver cannot advertise a feature the contract does
        not define. ``capability_version`` is the contract version the driver
        was written for: another major version of the capability is refused.
        ``requires`` are the top-level import names the driver needs and
        ``install`` how to get them (``pip install 'sandloom[celery]'``).
        """
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
                    f"{sorted(unknown)} not defined by capability {capability!r} "
                    f"(known: {sorted(spec.features)})",
                    fix="add the feature to the CapabilitySpec or drop it",
                )
        key = (capability, driver)
        if key in self._drivers:
            logger.debug("[infra] overriding driver %s/%s", capability, driver)
        self._drivers[key] = DriverRegistration(
            factory=factory,
            features=feats,
            config_schema=config_schema,
            instance_features=instance_features,
            sql_dialects=frozenset(sql_dialects or ()),
            requires=needed,
            install=install,
        )

    def has_driver(self, capability: str, driver: str) -> bool:
        return (capability, driver) in self._drivers

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
        """Replace the configuration (clears any built providers)."""
        self._config = {}
        for capability, instances in config.items():
            bucket = self._config.setdefault(capability, {})
            for name, cfg in instances.items():
                bucket[name] = cfg
        self._retired_providers.extend(self._providers.values())
        self._providers.clear()

    def configure_instance(self, config: InstanceConfig) -> None:
        """Add/override a single named instance (clears its cached provider)."""
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
        registration = self._drivers.get((capability, cfg.driver))
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
        registration = self._drivers.get((capability, cfg.driver))
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
            registration = self._drivers.get((capability, instance_cfg.driver))
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
            self._validate_options(capability, instance_cfg, registration)
            try:
                provider = registration.factory(instance_cfg)
            except ImportError as exc:
                raise self._import_failure(
                    capability, name, instance_cfg, registration, exc
                ) from exc
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
        """
        spec = self._capabilities.get(capability)
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
        registration = self._drivers.get((capability, cfg.driver))
        if registration is None:
            self._check_experimental_driver(capability, cfg.driver)
            raise DriverNotRegisteredError(
                f"No driver {cfg.driver!r} registered for capability {capability!r}",
                fix="register the driver or pick a configured one",
            )
        if features:
            unknown = features - spec.features
            if unknown:
                raise FeatureUnsupportedError(
                    f"Capability {capability!r} (v{spec.version}) defines no "
                    f"feature(s) {sorted(unknown)}; known: {sorted(spec.features)}",
                    fix="drop the unknown feature(s) or upgrade the contract",
                )
            advertised = registration.features_for(cfg.options)
            missing = features - advertised
            if missing:
                raise FeatureUnsupportedError(
                    f"{capability}/{instance} driver {cfg.driver!r} declares "
                    f"{sorted(advertised)} but feature(s) "
                    f"{sorted(missing)} are required",
                    fix=f"configure {capability}/{instance} with a driver that "
                    f"supports {sorted(missing)}",
                )
        unavailable = self._unavailable(capability, instance, features=features)
        if unavailable is not None:
            raise unavailable
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
                    f"feature(s) {sorted(unknown)}; known: {sorted(spec.features)}",
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
                registration = self._drivers.get((capability, cfg.driver))
                if registration is None:
                    missing.append(f"{capability}/{name} (driver={cfg.driver!r})")
                    continue
                # Build-free: instantiates the config schema only — never the
                # provider. Raises ConfigSchemaError with an actionable fix.
                self._validate_options(capability, cfg, registration)
                if not cfg.implicit:
                    problem = self._unavailable(capability, name)
                    if problem is not None:
                        unavailable.append(problem)
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
            except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
                result = HealthResult(
                    status="fail", details={"error": str(exc) or type(exc).__name__}
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
        event bus is excluded — it owns its own connect lifecycle.

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
        for capability, instances in self._config.items():
            if capability == "eventbus":
                continue
            for name in instances:
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
                    "contract": spec.provider_contract.__name__,
                    "features": sorted(spec.features),
                    "description": spec.description,
                    "stability": spec.stability,
                    "drivers": sorted(d for (c, d) in self._drivers if c == spec.name),
                }
                for spec in self.capabilities()
            },
            "drivers": sorted(f"{c}/{d}" for (c, d) in self._drivers),
            "installed_driver_packages": installed_plugin_names(),
            "instances": {
                cap: {
                    name: {
                        "driver": cfg.driver,
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
        raise DriverConflictError(
            f"Driver {key!r} is built in, and {entry.value} publishes it too",
            fix="rename the driver in that package: another behaviour is another "
            "driver name, selected in INFRA",
        )
    driver = _load_plugin(entry, f"Driver {key!r}")
    spec = registry.capability_spec(cfg.capability)
    contract = spec.provider_contract if spec else InfraProvider
    if not (isinstance(driver, type) and issubclass(driver, contract)):
        raise DriverLoadError(
            f"{entry.value} is not a subclass of {contract.__name__}",
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
    written_for = getattr(driver, "contract_version", None)
    if written_for is None:
        raise DriverLoadError(
            f"Driver {key!r} declares no contract_version",
            fix='add a class attribute, for example contract_version = "v1"',
        )
    if spec is not None and not versions_compatible(spec.version, written_for):
        raise DriverLoadError(
            f"Driver {key!r} was written for {written_for}, and the capability is "
            f"{spec.version}",
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
    ):
        raise DriverLoadError(
            f"Driver {key!r} declares invalid dependency metadata",
            fix="set required_imports to a tuple of top-level import names "
            "and install_hint to a string",
        )
    registry.register_driver(
        cfg.capability,
        cfg.driver,
        provider_class.from_config,
        features=provider_class.supported_features,
        config_schema=provider_class.options_schema,
        instance_features=provider_class.features_for,
        requires=required_imports,
        install=install_hint,
    )


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


__all__ = [
    "versions_compatible",
    "InfraRegistry",
    "InstanceSnapshot",
    "InfraError",
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
    "ExperimentalCapabilityRefused",
    "installed_plugin_names",
    "load_selected_plugins",
]
