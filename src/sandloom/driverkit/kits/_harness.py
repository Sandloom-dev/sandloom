"""The one harness every provider kit runs before its capability's own groups.

A kit is a pytest class with a ``make_provider`` fixture returning an
**unopened** provider (spec 2.7). The harness opens it, closes it twice, and
checks what every driver of every capability owes, one test per obligation so
a failure names what to fix:

1. it is an instance of the kit's provider ABC and has every operation of the
   Protocol that ABC implements;
2. ``contract_version`` is an ``int`` equal to the capability's major;
3. ``supported_features`` is within the spec's features, and ``features()``
   equals ``features_for(options)``;
4. ``performance_hints`` names only operations of the capability;
5. ``development_only`` is a ``bool``; ``health()`` answers within the bound
   and its ``details`` pass the redaction rule (spec 2.4), a failing backend's
   included;
6. ``isolation_identity()``: two providers built from the same options answer
   equal, non-``None`` identities starting with ``<driver>:``;
7. a declared behaviour feature's call does not raise ``Unsupported``, and an
   undeclared one's raises ``Unsupported(feature)``;
8. a provider built to fail (``make_failing_provider``, optional) raises only
   ``InfraError`` subclasses.

The kit itself is run against one broken driver per obligation in
``sandloom/core/tests/test_driverkit.py``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import ClassVar, Generic, Protocol, TypeVar

import pytest
import pytest_asyncio

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.errors import InfraError, Unsupported
from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider

P = TypeVar("P", bound=InfraProvider)

#: One call of an operation, with its arguments bound.
Call = Callable[[], Awaitable[object]]

#: A ``health()`` detail string that passes (spec 2.4): one token, such as an
#: error type name, a driver name or ``redis:<run_id>``.
HEALTH_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


def require(condition: bool, what: str) -> None:
    """Fail the running check with ``what`` unless ``condition`` holds."""
    if not condition:
        raise AssertionError(what)


def check_contract_version(version: object, major: int | None) -> None:
    """``version`` is the integer ``major`` (a bool or ``"v2"`` is not)."""
    require(
        isinstance(version, int) and not isinstance(version, bool) and version == major,
        f"contract_version must be the integer {major!r}, got {version!r}",
    )


def unvouched_details(details: object, path: str = "details") -> list[str]:
    """Every value of a health ``details`` the redaction rule refuses: a value
    is ``None``, a bool, a number or one token; mappings and lists are read
    the same way. Free text can carry a URL with credentials or a key."""
    if details is None or isinstance(details, (bool, int, float)):
        return []
    if isinstance(details, str):
        return [] if HEALTH_TOKEN.fullmatch(details) else [path]
    if isinstance(details, Mapping):
        found: list[str] = []
        for key, value in details.items():
            found.extend(unvouched_details(value, f"{path}[{key!r}]"))
        return found
    if isinstance(details, (list, tuple)):
        found = []
        for index, value in enumerate(details):
            found.extend(unvouched_details(value, f"{path}[{index}]"))
        return found
    return [path]


def protocol_members(protocol: type) -> frozenset[str]:
    """The public members a Protocol class declares (its own and its bases')."""
    members: set[str] = set()
    for cls in protocol.__mro__:
        if cls in (object, Protocol, Generic) or not getattr(
            cls, "_is_protocol", False
        ):
            continue
        members.update(name for name in vars(cls) if not name.startswith("_"))
        members.update(
            name
            for name in getattr(cls, "__annotations__", {})
            if not name.startswith("_")
        )
    return frozenset(members)


def contract_operations(contract: type, protocol: type | None) -> frozenset[str]:
    """The operations of a capability: the Protocol's members and the public
    methods the provider ABC adds above :class:`InfraProvider`."""
    operations = set(protocol_members(protocol)) if protocol is not None else set()
    for cls in contract.__mro__:
        if cls is InfraProvider or not issubclass(cls, InfraProvider):
            continue
        operations.update(
            name
            for name, value in vars(cls).items()
            if not name.startswith("_") and callable(value)
        )
    return frozenset(operations)


@pytest.mark.asyncio
class ProviderConformance(Generic[P]):
    """The checks every provider kit runs (spec 2.7). A kit sets ``contract``,
    ``protocol`` and ``spec``; a driver's tests subclass the kit and override
    ``make_provider``, and optionally ``make_failing_provider``."""

    #: The provider ABC every driver of the capability subclasses.
    contract: ClassVar[type[InfraProvider]]
    #: The Protocol the ABC implements; ``None`` for a capability with none.
    protocol: ClassVar[type | None] = None
    #: The capability's spec: its major and its features.
    spec: ClassVar[CapabilitySpec]
    #: The bound on ``health()``, as the registry bounds it
    #: (``HEALTH_CHECK_TIMEOUT_SECONDS``, default 5).
    health_timeout_seconds: ClassVar[float] = 5.0
    #: A secret a failing provider's backend may carry (a password in its
    #: options, say): no ``health()`` detail may show it.
    leak_marker: ClassVar[str] = "sandloom-kit-leak-7f3a"

    @pytest.fixture
    def make_provider(self) -> Callable[[], P]:
        """Build an unopened provider of the driver under test."""
        raise NotImplementedError("override make_provider to build your driver")

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], P] | None:
        """Build an unopened provider whose backend fails (unreachable, refused
        credentials carrying :attr:`leak_marker`). ``None``: the error
        mapping and failing-health checks are skipped."""
        return None

    @pytest_asyncio.fixture
    async def provider(self, make_provider: Callable[[], P]) -> AsyncIterator[P]:
        built = make_provider()
        await built.aopen()
        try:
            yield built
        finally:
            await built.aclose()

    def feature_calls(self, provider: P) -> Mapping[str, Call]:
        """One call per behaviour feature of the capability, the call that
        raises ``Unsupported(feature)`` when the feature is not declared."""
        return {}

    def operation_calls(self, provider: P) -> Mapping[str, Call]:
        """A call of each operation with valid arguments, run on a provider
        built to fail: each must raise an ``InfraError``."""
        return {}

    def requires_feature(self, provider: P, feature: str) -> None:
        """Skip the running group unless ``provider`` declares ``feature``."""
        if feature not in provider.features():
            pytest.skip(f"the driver does not declare {feature!r}")

    async def _health(self, built: InfraProvider) -> HealthResult:
        try:
            result = await asyncio.wait_for(
                built.health(), timeout=self.health_timeout_seconds
            )
        except TimeoutError:
            raise AssertionError(
                f"health() did not answer within {self.health_timeout_seconds}s: "
                "the registry records a timeout as a failure"
            ) from None
        require(
            isinstance(result, HealthResult),
            f"health() must return a HealthResult, got {result!r}",
        )
        refused = unvouched_details(result.details)
        require(
            not refused,
            f"health() {', '.join(refused)} is not a number, a bool or one token: "
            "carry an error type name, never str(exc), a URL or a key",
        )
        require(
            self.leak_marker not in repr(result.details),
            "health() details show the backend's secret",
        )
        return result

    # ------------------------------------------------------------ lifecycle
    async def test_harness_opens_and_closes_twice(
        self, make_provider: Callable[[], P]
    ) -> None:
        built = make_provider()
        await built.aopen()
        await built.aclose()
        await built.aclose()

    # ------------------------------------------------------- 1. the contract
    async def test_harness_implements_the_contract(self, provider: P) -> None:
        require(
            isinstance(provider, self.contract),
            f"{type(provider).__name__} must subclass {self.contract.__name__}",
        )
        if self.protocol is None:
            return
        missing = sorted(
            name
            for name in protocol_members(self.protocol)
            if not callable(getattr(provider, name, None))
        )
        require(
            not missing,
            f"{type(provider).__name__} lacks {missing} of {self.protocol.__name__}",
        )

    # --------------------------------------------------------- 2. the major
    async def test_harness_states_the_contract_version(self, provider: P) -> None:
        check_contract_version(
            getattr(type(provider), "contract_version", None), self.spec.major
        )

    # ------------------------------------------------------- 3. the features
    async def test_harness_declares_only_spec_features(self, provider: P) -> None:
        declared = type(provider).supported_features
        require(
            isinstance(declared, frozenset),
            f"supported_features must be a frozenset, got {declared!r}",
        )
        unknown = frozenset(declared) - self.spec.features
        require(
            not unknown,
            f"supported_features names feature(s) {self.spec.name} does not "
            f"define: {self.spec.unknown_features_hint(unknown)}",
        )

    async def test_harness_reports_the_features_it_declares(self, provider: P) -> None:
        expected = frozenset(type(provider).features_for(provider.options))
        require(
            provider.features() == expected,
            f"features() answers {sorted(provider.features())}, features_for "
            f"{sorted(expected)}: declare a feature in supported_features (and "
            "features_for when it depends on an option); never override features()",
        )

    # ------------------------------------------------------- 4. speed claims
    async def test_harness_hints_name_only_operations(self, provider: P) -> None:
        hints = type(provider).performance_hints
        require(
            isinstance(hints, Mapping),
            f"performance_hints must be a mapping, got {hints!r}",
        )
        operations = contract_operations(self.contract, self.protocol)
        unknown = sorted(set(hints) - operations)
        require(
            not unknown,
            f"performance_hints names {unknown}, not operations of "
            f"{self.spec.name} ({sorted(operations)})",
        )

    # -------------------------------------------- 5. declaration and health
    async def test_harness_states_development_only_as_a_bool(self, provider: P) -> None:
        flag = getattr(type(provider), "development_only", None)
        require(
            type(flag) is bool,
            f"development_only must be a bool, got {flag!r}",
        )

    async def test_harness_reports_health_within_the_bound(self, provider: P) -> None:
        await self._health(provider)

    async def test_harness_redacts_the_health_of_a_failing_backend(
        self, make_failing_provider: Callable[[], P] | None
    ) -> None:
        if make_failing_provider is None:
            pytest.skip("no make_failing_provider: a failing backend is not checked")
        built = make_failing_provider()
        try:
            try:
                await built.aopen()
            except InfraError:
                pass  # the error mapping check reads aopen
            await self._health(built)
        finally:
            await _close_quietly(built)

    # ------------------------------------------------ 6. isolation identity
    async def test_harness_names_its_isolation_domain(
        self, make_provider: Callable[[], P]
    ) -> None:
        first, second = make_provider(), make_provider()
        built = [first] if second is first else [first, second]
        for one in built:
            await one.aopen()
        try:
            identities: list[str | None] = []
            for one in (first, second):
                try:
                    identities.append(await one.isolation_identity())
                except Exception as exc:
                    raise AssertionError(
                        f"isolation_identity() raised {type(exc).__name__}: "
                        "a raising call fails production attestation"
                    ) from exc
        finally:
            for one in built:
                await one.aclose()
        identity, other = identities
        require(
            identity is not None,
            "isolation_identity() is None (unknown): name the backing store, "
            f"'{first.driver}:<store>', or production attestation fails closed",
        )
        require(
            isinstance(identity, str) and identity.startswith(f"{first.driver}:"),
            f"isolation_identity() {identity!r} must start with "
            f"'{first.driver}:' (the driver name), so two drivers never collide",
        )
        require(
            identity == other,
            f"two providers built from the same options answer {identity!r} and "
            f"{other!r}: the identity names the store, not the instance",
        )

    # -------------------------------------------------- 7. feature groups
    async def test_harness_declared_behaviours_do_not_raise_unsupported(
        self, provider: P
    ) -> None:
        declared = provider.features()
        for feature, call in self.feature_calls(provider).items():
            if feature not in declared:
                continue
            try:
                await call()
            except Unsupported as exc:
                raise AssertionError(
                    f"{feature!r} is declared, but its call raised "
                    f"Unsupported({exc.feature!r})"
                ) from None

    async def test_harness_undeclared_behaviours_raise_unsupported(
        self, provider: P
    ) -> None:
        declared = provider.features()
        for feature, call in self.feature_calls(provider).items():
            if feature in declared:
                continue
            try:
                await call()
            except Unsupported as exc:
                require(
                    exc.feature == feature,
                    f"the call of the undeclared {feature!r} raised "
                    f"Unsupported({exc.feature!r}), not Unsupported({feature!r})",
                )
                continue
            raise AssertionError(
                f"{feature!r} is not declared, but its call succeeded: it must "
                f"raise Unsupported({feature!r})"
            )

    # -------------------------------------------------- 8. error mapping
    async def test_harness_maps_backend_failures_to_infra_errors(
        self, make_failing_provider: Callable[[], P] | None
    ) -> None:
        if make_failing_provider is None:
            pytest.skip("no make_failing_provider: the error mapping is not checked")
        built = make_failing_provider()
        raised: list[str] = []
        try:
            calls: list[tuple[str, Call]] = [("aopen", built.aopen)]
            calls.extend(self.operation_calls(built).items())
            for name, call in calls:
                try:
                    await call()
                except InfraError:
                    raised.append(name)
                except Exception as exc:
                    raise AssertionError(
                        f"{name} raised {type(exc).__name__}: map the backend's "
                        "errors to InfraError subclasses (sandloom.driverkit)"
                    ) from exc
        finally:
            await _close_quietly(built)
        require(
            bool(raised),
            "the provider built to fail raised nothing: make_failing_provider "
            "must build one whose backend fails",
        )


async def _close_quietly(built: InfraProvider) -> None:
    """Close a provider built to fail; an ``InfraError`` from it is expected."""
    try:
        await built.aclose()
    except InfraError:
        pass


__all__ = [
    "HEALTH_TOKEN",
    "Call",
    "ProviderConformance",
    "check_contract_version",
    "contract_operations",
    "protocol_members",
    "require",
    "unvouched_details",
]
