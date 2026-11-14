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
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, Self
from urllib.parse import SplitResult, urlsplit, urlunsplit

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


def version_major(version: str) -> int | None:
    """The MAJOR of a capability version string, or ``None``.

    Accepts ``"v1"``, ``"v1.2"``, ``"V1"``, ``"v1.2.3"`` or a bare integer
    (``"1"``, ``"1.0"``): the leading integer after an optional ``v``. ``None``
    when the string has none: version matching then falls back to exact
    equality, and no driver loads for the capability (SANDLOOM-B022).
    """
    head = version.split(".", 1)[0].strip()
    if head[:1] in ("v", "V"):
        head = head[1:]
    try:
        return int(head)
    except ValueError:
        return None


@dataclass(frozen=True)
class CapabilitySpec:
    """A versioned description of one infrastructure capability.

    * ``name`` — capability key (``"cache"``, ``"graph_store"``, …).
    * ``version`` — the module-facing contract version (``"v1"``) a module
      may pin in a requirement or a ``CapabilityRef`` (same major compatible).
    * ``major`` — the driver contract's integer major: what a driver states as
      ``contract_version``; a driver written for another major is refused at
      load (SANDLOOM-B022). Default: the major of ``version``
      (:func:`version_major`); a spec with neither loads no driver. It moves on its
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
    * ``removed_features`` — feature names a previous major had, each mapped
      to its replacement: a refusal of a removed name names the replacement
      (spec 2.2). A removed name is never a feature of the spec.
    * ``kernel_only`` — a kernel-facing capability (``atomic_store``,
      ``lease``, ``broadcast``): the kernel reaches it through
      ``InfraRegistry.provider``; a module's resolve or requirement is
      refused (SANDLOOM-B041).
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
    removed_features: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    kernel_only: bool = False

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
        object.__setattr__(
            self, "removed_features", MappingProxyType(dict(self.removed_features))
        )
        revived = sorted(set(self.removed_features) & self.features)
        if revived:
            raise ValueError(
                f"CapabilitySpec {self.name!r}: removed feature(s) {revived} are "
                "still features of the spec"
            )
        if self.major is None:
            object.__setattr__(self, "major", version_major(self.version))

    @property
    def is_experimental(self) -> bool:
        """True when the contract is experimental (no core driver, may change)."""
        return self.stability == "experimental"

    def unclassified_features(self) -> frozenset[str]:
        """The features that carry no kind (a kernel spec has none)."""
        return self.features - frozenset(self.feature_kinds)

    def unknown_features_hint(self, unknown: frozenset[str]) -> str:
        """``unknown`` with the replacement of each removed name, for a
        refusal: ``['durable'] (durable was renamed durable_delivery)``."""
        renamed = [
            f"{name} was renamed {self.removed_features[name]}"
            for name in sorted(unknown)
            if name in self.removed_features
        ]
        return f"{sorted(unknown)}" + (f" ({'; '.join(renamed)})" if renamed else "")


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
        it. Override only when a feature depends on an option. The registry
        and :meth:`features` keep only what ``supported_features`` declares."""
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
        """The features THIS instance supports: ``features_for(options)``
        within ``supported_features`` (an undeclared feature is never claimed).

        Never overridden: the registry checks at build that the instance
        reports what its class declared, so a module's requirement that passes
        at startup cannot fail on first use.
        """
        cls = type(self)
        return frozenset(cls.features_for(self.options)) & cls.supported_features

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


# --------------------------------------------------------------------------- #
# Parallel test isolation of a driver that talks to Redis (spec 2.6)
# --------------------------------------------------------------------------- #
# ``InfraProvider.test_isolation(options, worker_id)`` is the hook; the Redis
# drivers implement it with ``isolated_redis_options``. There is one layout per
# test process, computed by the test harness (``sandloom.core.testing``: the
# stride, or the shared-Redis ``auto`` allocation) and handed over with
# ``set_worker_layout`` before it calls a hook, so a driver reads no
# environment. ``sandloom.driverkit.testing_isolation`` re-exports these names
# for driver packages. Standard library only.

#: The URL schemes whose database index is moved.
REDIS_SCHEMES = ("redis://", "rediss://")
#: Database indexes between two workers' instances, by default.
DEFAULT_STRIDE = 16
_WORKER_ID = re.compile(r"^gw(\d+)$")


@dataclass(frozen=True, slots=True)
class WorkerLayout:
    """Which database index each worker's URL moves to.

    ``mapping`` is the allocation of the shared-Redis ``auto`` mode,
    ``{base index: (index of gw0, index of gw1, ...)}``; without it the index
    moves by ``stride * (N + 1)``.
    """

    stride: int = DEFAULT_STRIDE
    mapping: Mapping[int, Sequence[int]] | None = None


_layout = WorkerLayout()


def set_worker_layout(layout: WorkerLayout | None = None) -> None:
    """Make ``layout`` (the default one when omitted) this process's layout.

    Test processes only: the test harness calls it before it asks a driver.
    """
    global _layout
    _layout = layout or WorkerLayout()


def worker_layout() -> WorkerLayout:
    """The layout :func:`isolated_redis_options` applies."""
    return _layout


def _plain_index(url: str) -> tuple[SplitResult, int] | None:
    parts = urlsplit(url)
    raw = parts.path.lstrip("/")
    try:
        return parts, (int(raw) if raw else 0)
    except ValueError:
        return None  # not a plain index (unix socket path, ...)


def worker_redis_url(url: str, number: int, stride: int) -> str:
    """``url`` with its database index offset by ``stride * (number + 1)``."""
    found = _plain_index(url)
    if found is None:
        return url
    parts, index = found
    return urlunsplit(parts._replace(path=f"/{index + stride * (number + 1)}"))


def mapped_redis_url(
    url: str, number: int, mapping: Mapping[int, Sequence[int]]
) -> str:
    """``url`` moved to worker ``number``'s allocated index (``auto``)."""
    found = _plain_index(url)
    if found is None:
        return url
    parts, index = found
    indexes = mapping.get(index)
    if not indexes:
        known = ";".join(
            f"{b}={','.join(map(str, i))}" for b, i in sorted(mapping.items())
        )
        raise RuntimeError(
            f"SANDLOOM_TEST_REDIS_DB_STRIDE=auto: no Redis index allocated for base "
            f"index {index}, worker gw{number} "
            f"(SANDLOOM_TEST_REDIS_WORKER_DBS={known!r})"
        )
    if number >= len(indexes):
        # A replacement for crashed workers beyond the allocated spares:
        # deterministic (never a conftest import failure cascade), but it
        # shares an index with another worker: say so on stderr.
        slot = number % len(indexes)
        print(
            f"sandloom testing: worker gw{number} has no Redis index of its own for "
            f"base {index}; sharing gw{slot}'s index {indexes[slot]} (a crashed "
            "worker was replaced more often than the spares allow)",
            file=sys.stderr,
        )
        return urlunsplit(parts._replace(path=f"/{indexes[slot]}"))
    return urlunsplit(parts._replace(path=f"/{indexes[number]}"))


def moved_redis_url(url: str, number: int, layout: WorkerLayout) -> str:
    """``url`` as worker ``number`` uses it under ``layout``."""
    if layout.mapping is not None:
        return mapped_redis_url(url, number, layout.mapping)
    return worker_redis_url(url, number, layout.stride)


def isolated_redis_options(
    options: Mapping[str, Any],
    worker_id: str,
    keys: Sequence[str] = ("url",),
) -> Mapping[str, Any]:
    """``options`` with each Redis URL under ``keys`` moved to worker
    ``worker_id``'s (``gw<N>``) database index under the process's layout.

    The ``test_isolation`` hook of the Redis drivers. Options without such a
    URL, a URL naming no plain index, or a ``worker_id`` that is not ``gw<N>``
    come back unchanged.
    """
    match = _WORKER_ID.match(worker_id)
    if match is None:
        return options
    number = int(match.group(1))
    out = dict(options)
    for key in keys:
        url = out.get(key)
        if isinstance(url, str) and url.startswith(REDIS_SCHEMES):
            out[key] = moved_redis_url(url, number, _layout)
    return out


__all__ = [
    "DEFAULT_STRIDE",
    "FEATURE_KINDS",
    "REDIS_SCHEMES",
    "CapabilitySpec",
    "CapabilityStability",
    "FeatureKind",
    "InfraProvider",
    "ProviderLifecycle",
    "WorkerLayout",
    "isolated_redis_options",
    "mapped_redis_url",
    "moved_redis_url",
    "set_worker_layout",
    "version_major",
    "worker_layout",
    "worker_redis_url",
]


#: A token a health detail may carry (spec 2.4): a type, driver or module name.
_FAILURE_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


def health_failure_details(exc: BaseException) -> dict[str, object]:
    """The ``details`` of a failed probe (spec 2.4): the error type name, and
    for an ``ImportError`` the missing module's name, an operator fact that
    carries no secret. Never the exception text, which can hold a URL with
    credentials or a key."""
    details: dict[str, object] = {"error": type(exc).__name__}
    missing = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
    if isinstance(missing, str) and _FAILURE_TOKEN.fullmatch(missing):
        details["missing_module"] = missing
    return details
