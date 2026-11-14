"""The provider base, the lifecycle port and the capability spec (ADR-0016).

Every capability's contract (``ports/<capability>.py``) builds on these three:

- :class:`InfraProvider`, the base class of every driver's provider: the
  declaration a driver makes once, on its class (spec 2.3);
- :class:`ProviderLifecycle`, the one Protocol through which the kernel reads a
  provider's lifecycle (spec 2.4), so it reads no undeclared attribute;
- :class:`CapabilitySpec`, a capability's versioned description: its major, its
  features and the kind of each (spec 2.2).
"""

from __future__ import annotations

import abc
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, Self

from sandloom.core.descriptors import HealthResult

if TYPE_CHECKING:
    from sandloom.core.infra.config import InstanceConfig

#: What a declared feature is (spec 2.2): a ``behaviour`` is an optional
#: behaviour of a call every driver has (without it the call raises
#: ``Unsupported(feature)``); a ``guarantee`` is a property no call shows
#: (durability, sharing, redelivery, scheduling).
FeatureKind = Literal["behaviour", "guarantee"]
FEATURE_KINDS: frozenset[str] = frozenset({"behaviour", "guarantee"})

#: Maturity of a capability contract (see :attr:`CapabilitySpec.stability`).
CapabilityStability = Literal["stable", "experimental"]
_STABILITIES: frozenset[str] = frozenset({"stable", "experimental"})
_VERSION = re.compile(r"v(\d+)(?:\.\d+)?")


@dataclass(frozen=True)
class CapabilitySpec:
    """A versioned description of one infrastructure capability.

    * ``name`` — capability key (``"cache"``, ``"graph_store"``, …).
    * ``version`` — the module-facing contract version (``"v1"``) a module
      may pin in a requirement or a ``CapabilityRef`` (same major compatible).
    * ``major`` — the driver contract's integer major: what a driver states as
      ``contract_version``; a driver written for another major is refused at
      load (SANDLOOM-B022). Default: the major of ``version``. It moves on its
      own when the driver obligations change and the module-facing shape does
      not (``objectstore`` and ``notification`` are 2 at ``version`` ``"v1"``:
      their error obligations changed).
    * ``provider_contract`` — the abstract base every driver of this capability
      implements; the registry rejects a driver that returns something else.
    * ``config_schema`` — optional pydantic model validating an instance's
      ``options``. ``None`` = no validation.
    * ``feature_kinds`` — every feature the contract defines, with its kind
      (``behaviour`` or ``guarantee``, spec 2.2). A driver declares the subset
      it supports (``supported_features`` / ``features_for``); a module may
      require a subset and is refused at load without it (B033, B037).
    * ``features`` — the feature names; derived from ``feature_kinds`` (a spec
      a plugin writes with ``features`` alone still works, its features
      unclassified).
    * ``description`` — human/AI-facing one-liner.
    * ``stability`` — ``"stable"`` or ``"experimental"`` (a contract the kernel
      knows but ships no driver for; its surface may still change).
    """

    name: str
    version: str
    provider_contract: type
    config_schema: type[Any] | None = None
    features: frozenset[str] = field(default_factory=frozenset)
    description: str = ""
    stability: CapabilityStability = "stable"
    feature_kinds: Mapping[str, FeatureKind] = field(
        default_factory=lambda: MappingProxyType({})
    )
    major: int | None = None

    def __post_init__(self) -> None:
        if self.stability not in _STABILITIES:
            raise ValueError(
                f"CapabilitySpec {self.name!r}: stability must be one of "
                f"{sorted(_STABILITIES)}, got {self.stability!r}"
            )
        wrong = {f: k for f, k in self.feature_kinds.items() if k not in FEATURE_KINDS}
        if wrong:
            raise ValueError(
                f"CapabilitySpec {self.name!r}: a feature kind is 'behaviour' or "
                f"'guarantee', got {wrong}"
            )
        object.__setattr__(
            self, "feature_kinds", MappingProxyType(dict(self.feature_kinds))
        )
        object.__setattr__(
            self, "features", frozenset(self.features) | frozenset(self.feature_kinds)
        )
        if self.major is None:
            found = _VERSION.fullmatch(self.version.strip())
            object.__setattr__(self, "major", int(found.group(1)) if found else None)

    @property
    def is_experimental(self) -> bool:
        """True when the contract is experimental (no core driver, may change)."""
        return self.stability == "experimental"

    def unclassified_features(self) -> frozenset[str]:
        """The features that carry no kind (a kernel spec has none)."""
        return self.features - frozenset(self.feature_kinds)


class InfraProvider(abc.ABC):
    """Base class for every infrastructure provider.

    A provider is one configured *instance* of a *capability*, backed by a
    concrete *driver*. A driver declares itself once, on its class: the
    capability major it was written for (``contract_version``), the features
    it supports (``supported_features``, or ``features_for`` when they depend
    on an option), its speed claims (``performance_hints``) and whether it is
    for development only. The registry reads that declaration before building
    and checks a built-in like an external driver.
    """

    #: Capability key this provider implements (set by the capability subclass).
    capability: ClassVar[str] = "infra"
    #: The integer major of the capability contract the driver was written for.
    #: Required of every driver, built-ins included: refused at load without
    #: it, or for another major (SANDLOOM-B022).
    contract_version: ClassVar[int]
    #: The capability features this driver supports. The one declaration: the
    #: registry reads it before building, ``features()`` answers from it.
    supported_features: ClassVar[frozenset[str]] = frozenset()
    #: Speed claims about single operations (``{"get_many": "one_round_trip"}``).
    #: No correctness decision reads them; benchmarks and ``sandloom doctor`` do.
    performance_hints: ClassVar[Mapping[str, str]] = MappingProxyType({})
    #: A driver for development and tests only (``memory``): refused in
    #: production, with no accepted risk to lift it (SANDLOOM-B022).
    development_only: ClassVar[bool] = False
    #: Optional pydantic model validating an instance's options before building.
    options_schema: ClassVar[type[Any] | None] = None
    #: Top-level imports needed by the backend. The entry point must be
    #: importable without them so the registry can check them before building.
    required_imports: ClassVar[tuple[str, ...]] = ()
    #: Installation command to show when a backend import is missing.
    install_hint: ClassVar[str] = ""

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        self.name = name
        self.driver = driver
        self.options: dict[str, Any] = dict(options)

    @classmethod
    def from_config(cls, config: InstanceConfig) -> Self:
        """Build the provider of one configured instance (the driver factory)."""
        return cls(name=config.name, driver=config.driver, options=config.options)

    @classmethod
    def features_for(cls, options: Mapping[str, Any]) -> frozenset[str]:
        """The features an instance with ``options`` supports, without building
        it. Override only when a feature depends on an option."""
        return cls.supported_features

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """The options one pytest-xdist worker's instance uses, so parallel
        runs share no state (another database index of the configured URL, a
        key prefix). Default: the options unchanged (right for ``memory``)."""
        return options

    def features(self) -> frozenset[str]:
        """The features THIS instance supports: ``features_for(options)``.

        Never overridden: the registry checks at build that the instance
        reports what its class declared, so a module's requirement that passes
        at startup cannot fail on first use.
        """
        return type(self).features_for(self.options)

    async def aopen(self) -> None:
        """Open the instance before its first operation. Default: no-op.

        The registry calls it once per used instance at startup
        (``InfraRegistry.aopen_all``). A driver that cannot serve raises
        ``Unavailable`` or ``PermissionDenied``; the startup policy decides.
        Named ``aopen`` so it never collides with a capability's domain verb.
        """
        return None

    async def health(self) -> HealthResult:
        """Liveness probe for this instance. Default: always healthy.

        ``details`` carries only non-secret facts: the driver name, an error
        type name, a latency, a count. Never ``str(exc)``, a URL or a key.
        """
        return HealthResult(status="ok")

    async def aclose(self) -> None:
        """Release any resources held by this instance (idempotent)."""
        return None

    async def isolation_identity(self) -> str | None:
        """An opaque name of the backing store's failure and eviction domain.

        Two instances with the same identity share eviction and memory. It
        starts with the driver name and a colon (``redis:<run_id>``) and
        carries no credential. ``None`` means unknown, which fails production
        attestation closed.
        """
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<{type(self).__name__} capability={self.capability!r} "
            f"name={self.name!r} driver={self.driver!r}>"
        )


class ProviderLifecycle(Protocol):
    """How the kernel reads a provider's lifecycle (the driver door, spec 2.4)."""

    name: str
    driver: str

    def features(self) -> frozenset[str]: ...
    async def aopen(self) -> None: ...
    async def health(self) -> HealthResult: ...
    async def aclose(self) -> None: ...
    async def isolation_identity(self) -> str | None: ...


__all__ = [
    "FEATURE_KINDS",
    "CapabilitySpec",
    "CapabilityStability",
    "FeatureKind",
    "InfraProvider",
    "ProviderLifecycle",
]
