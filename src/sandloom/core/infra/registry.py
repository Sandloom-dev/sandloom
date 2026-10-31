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
import inspect
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, cast

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.capabilities import CapabilitySpec
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InfraConfig,
    InstanceConfig,
)
from sandloom.core.infra.contracts import InfraProvider

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

    def features_for(self, options: Mapping[str, Any]) -> frozenset[str]:
        if self.instance_features is None:
            return self.features
        return frozenset(self.instance_features(options)) & self.features


class InfraError(RuntimeError):
    """Raised on registry misconfiguration (unknown driver/instance)."""


class InfraStartupError(InfraError):
    """One or more configured infra instances failed to open at startup.

    Raised by :meth:`InfraRegistry.aopen_all` in strict mode (production) so a
    database/cache/object-store the process cannot reach aborts the lifespan
    instead of letting the app serve traffic that will fail on first use.
    """

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        self.fix = fix
        super().__init__(message if fix is None else f"{message}\nFix: {fix}")


class CapabilityError(InfraError):
    """Base for capability-resolution problems — carries an actionable hint.

    Diagnostic by design: AI agents and operators should be able to act on the
    message without reading kernel source. Subclasses pin the exact failure;
    ``fix`` lists concrete remedies.
    """

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        self.fix = fix
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


class DriverConflictError(CapabilityError):
    """A selected driver or capability name has two providers (SANDLOOM-B021)."""

    sandloom_code = "SANDLOOM-B021"


class DriverLoadError(CapabilityError):
    """A selected driver or capability cannot be used (SANDLOOM-B022)."""

    sandloom_code = "SANDLOOM-B022"


class ExperimentalCapabilityRefused(CapabilityError):
    """An experimental capability configured in production (SANDLOOM-B023)."""

    sandloom_code = "SANDLOOM-B023"


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
    ) -> None:
        """Register a driver for a capability (idempotent override).

        ``features``/``config_schema`` are build-free metadata (see
        :class:`DriverRegistration`). In strict mode the capability must already
        have a registered spec, and any declared features must be a subset of
        the spec's — so a driver cannot advertise a feature the contract does
        not define. ``capability_version`` is the contract version the driver
        was written for: another major version of the capability is refused.
        """
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
        )

    def has_driver(self, capability: str, driver: str) -> bool:
        return (capability, driver) in self._drivers

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

    def instances(self, capability: str) -> list[str]:
        """Return configured instance names for a capability."""
        return sorted(self._config.get(capability, {}).keys())

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
            provider = registration.factory(instance_cfg)
            self._check_contract(capability, name, provider)
            if self._strict:
                self._check_declared_features(
                    name, instance_cfg, registration, provider
                )
            self._providers[cache_key] = provider
            logger.info(
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
        registered, and the requested features are both defined by the spec and
        declared by the driver. Used by the module preflight so a bad plugin's
        ``factory`` can never run network code at startup.
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

        * the instance's driver is registered, and
        * its options satisfy the driver/capability config schema.

        Options are only validated for instances whose driver is registered and
        carries a schema; an instance without a schema is skipped (current
        behaviour) — :meth:`_validate_options` just instantiates the schema.
        """
        missing: list[str] = []
        for capability, instances in self._config.items():
            for name, cfg in instances.items():
                registration = self._drivers.get((capability, cfg.driver))
                if registration is None:
                    missing.append(f"{capability}/{name} (driver={cfg.driver!r})")
                    continue
                # Build-free: instantiates the config schema only — never the
                # provider. Raises ConfigSchemaError with an actionable fix.
                self._validate_options(capability, cfg, registration)
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
    async def health_all(self) -> dict[str, HealthResult]:
        """Health-probe every configured instance (builds them as needed).

        Keys are ``"<capability>:<name>"``. Every probe has its own bounded
        timeout; an unavailable instance cannot serially delay every probe.
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
                    for name in instances
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
        """
        failures: list[str] = []
        await self._close_retired()
        for capability, instances in self._config.items():
            if capability == "eventbus":
                continue
            for name in instances:
                try:
                    provider = self.provider(capability, name)
                    await provider.aopen()
                except Exception as exc:
                    # A security misconfiguration (e.g. an application role
                    # that could escape tenant RLS) aborts startup in every
                    # environment, not only under strict (production) startup.
                    if strict or getattr(exc, "fatal_at_startup", False):
                        logger.error(
                            "[infra] failed to open %s/%s",
                            capability,
                            name,
                            exc_info=True,
                        )
                        failures.append(f"{capability}/{name}: {exc}")
                    else:
                        logger.warning(
                            "[infra] error opening %s/%s",
                            capability,
                            name,
                            exc_info=True,
                        )
        if failures:
            raise InfraStartupError(
                "Infrastructure failed to open at startup: " + "; ".join(failures),
                fix="check the DSN/credentials/reachability of the failing "
                "instance(s); the process will not serve traffic until every "
                "configured instance opens (strict startup is on in production)",
            )

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
        """Return a JSON-serializable snapshot for diagnostics endpoints."""
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
    if inspect.isabstract(driver):
        missing = ", ".join(sorted(getattr(driver, "__abstractmethods__", ())))
        raise DriverLoadError(
            f"Driver {key!r} does not implement {missing}",
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
    registry.register_driver(
        cfg.capability,
        cfg.driver,
        provider_class.from_config,
        features=provider_class.supported_features,
        config_schema=provider_class.options_schema,
        instance_features=provider_class.features_for,
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
    "FeatureUnsupportedError",
    "InstanceNotConfiguredError",
    "DriverNotRegisteredError",
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
