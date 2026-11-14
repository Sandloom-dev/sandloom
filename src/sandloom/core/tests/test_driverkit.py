"""``sandloom.driverkit``: finding drivers by name, the guide's example, the kit."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import sandloom.core.descriptors.module_names as module_names
import sandloom.driverkit
import sandloom.driverkit.testing
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.drivers.log_notifier import LogNotifierProvider
from sandloom.core.infra.capabilities import (
    BUILTIN_CAPABILITIES,
    NOTIFICATION_CAPABILITY,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.registry import (
    DriverConflictError,
    DriverLoadError,
    ExperimentalCapabilityRefused,
    FeatureUnsupportedError,
    InfraRegistry,
    InfraStartupError,
    ProviderContractError,
    load_selected_plugins,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit import (
    CapabilityRef,
    CapabilitySpec,
    HealthResult,
    InfraProvider,
    InvalidArgument,
    NotFound,
    Notification,
    NotifierProvider,
    ObjectContent,
    ObjectStoreProvider,
    TooLarge,
    Unavailable,
    Unsupported,
    iter_object_chunks,
    validate_object_key,
)
from sandloom.driverkit.testing import NotifierConformance, ObjectStoreConformance

SNIPPET = Path(__file__).resolve().parents[4] / "docs/snippets/drivers/file_notifier.py"


def _guide_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("file_notifier", SNIPPET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Behaviour(InfraProvider):
    """What a well-behaved notification driver does, NotifierProvider or not."""

    capability = "notification"

    async def send(self, notification: Notification) -> None:
        if notification.channel not in self.features():
            raise Unsupported(
                notification.channel, capability="notification", instance=self.name
            )

    async def isolation_identity(self) -> str | None:
        return f"{self.driver}:shared"


class _Notifier(_Behaviour, NotifierProvider):
    contract_version = 2
    supported_features = frozenset({"email"})


def _entry(name: str, target: Any, *, owner: str = "pkg") -> SimpleNamespace:
    def load() -> Any:
        if isinstance(target, Exception):
            raise target
        return target

    return SimpleNamespace(
        name=name,
        value=f"{owner}.driver:X",
        dist=SimpleNamespace(name=owner),
        load=load,
    )


def _install(monkeypatch: pytest.MonkeyPatch, **groups: list[Any]) -> None:
    class _Entries:
        def select(self, *, group: str, name: str | None = None) -> list[Any]:
            found = groups.get(group.replace("sandloom.", ""), [])
            return [e for e in found if name is None or e.name == name]

    monkeypatch.setattr(
        "sandloom.core.infra.registry.metadata.entry_points", lambda: _Entries()
    )
    # importlib.metadata is patched process-wide: the module-name cache filled
    # meanwhile must not outlive the test (monkeypatch restores the real one).
    monkeypatch.setattr(module_names, "_cache", None)


def _config(driver: str = "x", **options: Any) -> dict[str, dict[str, InstanceConfig]]:
    return {
        "notification": {
            "default": InstanceConfig("notification", "default", driver, options)
        }
    }


def _strict() -> InfraRegistry:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    return registry


# ------------------------------------------------------------------- discovery
def test_a_selected_driver_is_found_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, drivers=[_entry("notification.x", _Notifier)])
    registry = _strict()
    config = _config()
    load_selected_plugins(registry, config)
    registry.set_config(config)
    provider = registry.resolve("notification", features=frozenset({"email"}))
    assert isinstance(provider, _Notifier)


def test_an_unselected_package_is_never_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        drivers=[
            _entry("notification.x", _Notifier),
            _entry("notification.broken", RuntimeError("must not load")),
        ],
    )
    load_selected_plugins(_strict(), _config())


def test_a_missing_import_in_the_entry_point_is_b022(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = ModuleNotFoundError("missing backend", name="missing_backend")
    _install(monkeypatch, drivers=[_entry("notification.x", failure)])

    with pytest.raises(DriverLoadError, match="missing backend") as caught:
        load_selected_plugins(_strict(), _config())

    assert caught.value.sandloom_code == "SANDLOOM-B022"


def test_a_dotted_required_import_is_b022(monkeypatch: pytest.MonkeyPatch) -> None:
    class InvalidMetadata(_Notifier):
        required_imports = ("backend.submodule",)

    _install(monkeypatch, drivers=[_entry("notification.x", InvalidMetadata)])

    with pytest.raises(DriverLoadError, match="top-level import names") as caught:
        load_selected_plugins(_strict(), _config())

    assert caught.value.sandloom_code == "SANDLOOM-B022"


async def test_an_import_error_during_open_follows_startup_policy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenOnOpen(_Notifier):
        required_imports = ("json",)

        async def aopen(self) -> None:
            raise ModuleNotFoundError("missing transitive SDK", name="missing_sdk")

    _install(monkeypatch, drivers=[_entry("notification.x", BrokenOnOpen)])
    registry = _strict()
    config = _config()
    load_selected_plugins(registry, config)
    registry.set_config(config)
    registry.validate_requirement("notification")

    await registry.aopen_all(strict=False)
    assert "error opening notification/default" in caplog.text
    with pytest.raises(InfraStartupError, match="missing transitive SDK") as caught:
        await registry.aopen_all(strict=True)

    assert getattr(caught.value, "sandloom_code", None) != "SANDLOOM-B037"


def test_a_capability_in_another_package_loads_in_any_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Search(InfraProvider):
        capability = "vector_index"
        contract_version = 1

    spec = CapabilitySpec(name="vector_index", version="v1", provider_contract=Search)
    _install(
        monkeypatch,
        drivers=[_entry("vector_index.memory", Search)],
        capabilities=[_entry("vector_index", spec)],
    )
    config = {
        "vector_index": {"default": InstanceConfig("vector_index", "default", "memory")}
    }
    registry = InfraRegistry(strict_capabilities=True)
    load_selected_plugins(registry, config)
    registry.set_config(config)
    assert isinstance(registry.provider("vector_index"), Search)


def test_one_driver_can_back_two_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, drivers=[_entry("notification.x", _Notifier)])
    config = _config()
    config["notification"]["second"] = InstanceConfig("notification", "second", "x")
    registry = _strict()
    load_selected_plugins(registry, config)
    registry.set_config(config)
    assert registry.provider("notification", "second").name == "second"


def test_two_providers_of_a_selected_name_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        drivers=[
            _entry("notification.x", _Notifier, owner="one"),
            _entry("notification.x", _Notifier, owner="two"),
        ],
    )
    with pytest.raises(DriverConflictError, match="one, two"):
        load_selected_plugins(_strict(), _config())


def test_a_package_cannot_take_over_a_built_in_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, drivers=[_entry("notification.smtp", _Notifier)])
    registry = _strict()
    registry.register_driver("notification", "smtp", _Notifier.from_config)
    with pytest.raises(DriverConflictError, match="built in"):
        load_selected_plugins(registry, _config("smtp"))


class _NoVersion(NotifierProvider):
    async def send(self, notification: Notification) -> None:
        return None


class _OldVersion(_Notifier):
    contract_version = 1


class _StringVersion(_Notifier):
    contract_version = "v2"  # type: ignore[assignment]


class _Abstract(NotifierProvider):
    contract_version = 2


class _NotCallable(_Notifier):
    send = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (ImportError("no such backend"), "failed to import"),
        (object, "not a subclass"),
        (_NoVersion, "no contract_version"),
        (_OldVersion, "written for contract_version 1"),
        (_StringVersion, "integer major"),
        (_Abstract, "does not implement send"),
        (_NotCallable, "does not implement send"),
    ],
)
def test_a_selected_driver_that_cannot_be_used_stops_startup(
    monkeypatch: pytest.MonkeyPatch, target: Any, message: str
) -> None:
    _install(monkeypatch, drivers=[_entry("notification.x", target)])
    with pytest.raises(DriverLoadError, match=message):
        load_selected_plugins(_strict(), _config())


def test_a_driver_reporting_other_features_than_declared_is_refused() -> None:
    class Drifting(_Notifier):
        def features(self) -> frozenset[str]:
            return frozenset()

    registry = _strict()
    registry.register_driver(
        "notification", "x", Drifting.from_config, features={"email"}
    )
    registry.set_config(_config())
    with pytest.raises(ProviderContractError, match="declares features"):
        registry.provider("notification")


# --------------------------------------------------------------- experimental
def _prod(*risks: str) -> Any:
    return SimpleNamespace(
        is_prod=True, accepts_risk=lambda risk: risk in risks, infra={}
    )


def test_an_experimental_capability_is_refused_in_production() -> None:
    config = {
        "document_store": {
            "default": InstanceConfig("document_store", "default", "memory")
        }
    }
    with pytest.raises(ExperimentalCapabilityRefused, match="document_store"):
        build_infra_registry(_schema(), settings=_prod(), config=config)
    build_infra_registry(
        _schema(), settings=_prod("experimental_capability"), config=config
    )


def _schema() -> Any:
    return InMemorySchemaRegistry()


# ------------------------------------------------------------------- references
def test_a_ref_carries_name_and_version() -> None:
    ref: CapabilityRef[Any] = CapabilityRef("vector_index.v2")
    assert (ref.name, ref.version) == ("vector_index", "v2")
    bare: CapabilityRef[Any] = CapabilityRef("document_store")
    assert (bare.name, bare.version) == ("document_store", None)
    for not_a_version in ("search.vector", "foo.v"):
        ref = CapabilityRef[Any](not_a_version)
        assert (ref.name, ref.version) == (not_a_version, None)


def test_a_ref_for_another_major_version_is_refused() -> None:
    registry = _strict()
    registry.register_driver(
        "notification", "x", _Notifier.from_config, features={"email"}
    )
    registry.set_config(_config())
    with pytest.raises(FeatureUnsupportedError, match="written for v2"):
        registry.resolve("notification", version="v2")
    assert registry.resolve("notification", version="v1")


# -------------------------------------------------------------- surface pinning
#: A contract's obligations, per version. Editing one without bumping the spec's
#: version would break drivers silently: a change is a new version.
_NOTIFICATION_V1 = {"send": "(self, notification: 'Notification') -> 'None'"}


def test_the_public_contract_changes_only_with_its_version() -> None:
    assert NOTIFICATION_CAPABILITY.version == "v1"
    assert NOTIFICATION_CAPABILITY.major == 2
    abstract = sorted(NotifierProvider.__abstractmethods__)
    signatures = {
        name: str(inspect.signature(getattr(NotifierProvider, name)))
        for name in abstract
    }
    assert signatures == _NOTIFICATION_V1


#: Kernel-facing contracts exported before their kit (PR #190 review, C4):
#: their capability slices deliver these kits. Remove an entry when its kit
#: lands; the test fails until you do.
_KIT_PENDING = frozenset({"AtomicStoreProvider", "LeaseProvider", "BroadcastProvider"})


def test_every_public_contract_has_a_kit() -> None:
    contracts = [
        name
        for name in sandloom.driverkit.__all__
        if name.endswith("Provider") and name != "InfraProvider"
    ]
    assert contracts
    for name in contracts:
        kit = name.removesuffix("Provider") + "Conformance"
        assert kit in sandloom.driverkit.testing.__all__, f"{name}: no kit name {kit}"
        delivered = hasattr(sandloom.driverkit.testing, kit)
        if name in _KIT_PENDING:
            assert not delivered, f"{kit} landed: drop {name} from _KIT_PENDING"
            continue
        assert delivered, f"{name} has no kit: {kit}"


def test_a_kit_not_delivered_yet_names_its_slice() -> None:
    with pytest.raises(AttributeError, match="idk-cache-contract") as caught:
        _ = sandloom.driverkit.testing.CacheConformance
    assert "sandloom.driverkit.kits.cache" in str(caught.value)
    with pytest.raises(AttributeError, match="no attribute 'NoSuchKit'"):
        _ = sandloom.driverkit.testing.NoSuchKit
    assert "LeaseConformance" in dir(sandloom.driverkit.testing)


def test_the_api_baseline_pins_a_pending_kit_until_it_lands() -> None:
    script = SNIPPET.parents[3] / "tools/packaging/check_public_api.py"
    spec = importlib.util.spec_from_file_location("check_public_api", script)
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    pending = {"m": {"Kit": {"kind": "pending"}}}
    landed = {"m": {"Kit": {"kind": "class", "members": ["a"], "signature": "()"}}}
    assert api._diff(pending, landed) == []
    assert api._diff(landed, pending) == ["m.Kit: no longer resolves"]
    assert api._diff(pending, {"m": {}}) == ["m.Kit: symbol removed"]


@pytest.mark.parametrize(
    "spec", [*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES], ids=lambda s: s.name
)
def test_a_required_method_is_not_called_a_feature(spec: CapabilitySpec) -> None:
    required = spec.provider_contract.__abstractmethods__  # type: ignore[attr-defined]
    assert not spec.features & set(required)


@pytest.mark.parametrize(
    ("kit", "exempt"),
    [
        (NotifierConformance, frozenset()),
        # ``metadata`` changes what put stores, not whether it runs: a driver
        # without it accepts the content type and drops it (spec 9 lists no
        # Unsupported("metadata")); its kit group runs only when declared.
        (ObjectStoreConformance, frozenset({"metadata"})),
    ],
)
def test_each_behaviour_feature_has_its_call(kit: Any, exempt: frozenset[str]) -> None:
    behaviours = {f for f, k in kit.spec.feature_kinds.items() if k == "behaviour"}
    calls = kit().feature_calls(_built(_kit_driver(kit)))
    assert set(calls) == behaviours - exempt


# ----------------------------------------------------------------------- the kit
class TestTheGuideDriver(NotifierConformance):
    @pytest.fixture
    def make_provider(self, tmp_path: Path) -> Callable[[], NotifierProvider]:
        driver = _guide_module().FileNotifier
        return lambda: driver(
            name="default", driver="file", options={"path": str(tmp_path / "m.log")}
        )


class TestTheShippedLogDriver(NotifierConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], NotifierProvider]:
        return lambda: LogNotifierProvider(name="default", driver="log", options={})


async def test_the_guide_driver_records_the_whole_message(tmp_path: Path) -> None:
    target = tmp_path / "m.log"
    driver = _guide_module().FileNotifier(
        name="default", driver="file", options={"path": str(target)}
    )
    await driver.send(
        Notification(channel="email", recipient="a@b.test", subject="Hi", body="x")
    )
    assert '"body": "x"' in target.read_text()
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="'path' option"):
        _guide_module().FileNotifier(name="default", driver="file", options={})


# ------------------------------------------------- the kit against broken drivers
#: A secret a broken driver leaks; the harness looks for its own marker.
_LEAK = f"smtp://user:{NotifierConformance.leak_marker}@mail.test:25 refused"


class _Failing(_Notifier):
    """A notification driver whose backend is down, mapped as it must be."""

    async def send(self, notification: Notification) -> None:
        raise Unavailable("backend down", capability="notification")

    async def health(self) -> HealthResult:
        return HealthResult(status="fail", details={"error": "ConnectionError"})


_OBJECTS: dict[str, bytes] = {}


class _Store(ObjectStoreProvider):
    """A memory object store that keeps the contract; its broken variants don't."""

    contract_version = 2
    supported_features = frozenset({"presigned_get"})

    async def put(
        self,
        key: str,
        data: ObjectContent,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        validate_object_key(key)
        body = b"".join([chunk async for chunk in iter_object_chunks(data, 64)])
        limit = self.options.get("limit")
        if limit is not None and len(body) > limit:
            raise TooLarge("over the limit", capability="objectstore")
        _OBJECTS[key] = body
        return key

    async def get(self, key: str) -> bytes:
        validate_object_key(key)
        if key not in _OBJECTS:
            raise NotFound("absent", capability="objectstore")
        return _OBJECTS[key]

    async def delete(self, key: str) -> None:
        validate_object_key(key)
        _OBJECTS.pop(key, None)

    async def url(self, key: str) -> str:
        validate_object_key(key)
        if "presigned_get" not in self.features():
            raise Unsupported("presigned_get", capability="objectstore")
        return f"memory://{key}"

    async def isolation_identity(self) -> str | None:
        return f"{self.driver}:objects"


class _Broken:
    """One broken driver per obligation: each fails exactly the check naming
    it. A kit that passes everything proves nothing."""

    # 1. the contract
    class WrongContract(_Behaviour):
        contract_version = 2
        supported_features = frozenset({"email"})

    # 2. the major
    class NoVersion(_Behaviour, NotifierProvider):
        supported_features = frozenset({"email"})

    class StringVersion(_Notifier):
        contract_version = "v2"  # type: ignore[assignment]

    class BoolVersion(_Notifier):
        contract_version = True

    class OldMajor(_Notifier):
        contract_version = 1

    # 3. the features
    class UnknownFeature(_Notifier):
        supported_features = frozenset({"email", "fax"})

    class Drifting(_Notifier):
        def features(self) -> frozenset[str]:
            return frozenset()

    class OverClaiming(_Notifier):
        @classmethod
        def features_for(cls, options: Mapping[str, Any]) -> frozenset[str]:
            return frozenset({"email", "sms"})

    # 4. speed claims
    class HintsNoOperation(_Notifier):
        performance_hints = {"broadcast": "one_round_trip"}

    # 5. declaration and health
    class DevelopmentOnlyNotABool(_Notifier):
        development_only = 1  # type: ignore[assignment]

    class SlowHealth(_Notifier):
        async def health(self) -> HealthResult:
            await asyncio.sleep(1)
            return HealthResult(status="ok")

    class HealthWithAUrl(_Notifier):
        async def health(self) -> HealthResult:
            return HealthResult(status="fail", details={"error": _LEAK})

    class HealthWithTheSecret(_Notifier):
        async def health(self) -> HealthResult:
            marker = NotifierConformance.leak_marker
            return HealthResult(status="fail", details={"checks": [marker]})

    class FailingHealthWithTheSecret(_Failing):
        async def health(self) -> HealthResult:
            return HealthResult(status="fail", details={"error": _LEAK})

    # 6. isolation identity
    class NoIdentity(_Notifier):
        async def isolation_identity(self) -> str | None:
            return None

    class UnprefixedIdentity(_Notifier):
        async def isolation_identity(self) -> str | None:
            return "redis:shared"

    class IdentityPerInstance(_Notifier):
        async def isolation_identity(self) -> str | None:
            return f"{self.driver}:{id(self)}"

    class RaisingIdentity(_Notifier):
        async def isolation_identity(self) -> str | None:
            raise ConnectionError(_LEAK)

    # lifecycle
    class ClosesOnce(_Notifier):
        closed = False

        async def aclose(self) -> None:
            if self.closed:
                raise RuntimeError("already closed")
            self.closed = True

    # 7. feature groups
    class SendsAnyChannel(_Notifier):
        async def send(self, notification: Notification) -> None:
            return None

    class RefusesItsDeclaredLink(_Store):
        async def url(self, key: str) -> str:
            validate_object_key(key)
            raise Unsupported("presigned_get", capability="objectstore")

    class LinkWithoutTheFeature(_Store):
        supported_features = frozenset()

        async def url(self, key: str) -> str:
            validate_object_key(key)
            return f"memory://{key}"

    class RefusesTheWrongFeature(_Store):
        supported_features = frozenset()

        async def url(self, key: str) -> str:
            validate_object_key(key)
            raise Unsupported("metadata", capability="objectstore")

    # 8. error mapping
    class FailingWithABackendError(_Failing):
        async def send(self, notification: Notification) -> None:
            raise ConnectionError(_LEAK)

    class FailingThatSucceeds(_Failing):
        async def send(self, notification: Notification) -> None:
            return None

    # the capabilities' own groups
    class RefusesADedupeKey(_Notifier):
        async def send(self, notification: Notification) -> None:
            if notification.dedupe_key:
                raise InvalidArgument("dedupe", capability="notification")
            await super().send(notification)

    class IgnoresItsLimit(_Store):
        async def put(
            self,
            key: str,
            data: ObjectContent,
            *,
            content_type: str | None = None,
            metadata: Mapping[str, str] | None = None,
        ) -> str:
            validate_object_key(key)
            _OBJECTS[key] = b"".join([c async for c in iter_object_chunks(data, 64)])
            return key

    class MetadataWithoutAReader(_Store):
        supported_features = frozenset({"presigned_get", "metadata"})


class _NotifierKit(NotifierConformance):
    health_timeout_seconds = 0.2


class _StoreKit(ObjectStoreConformance):
    health_timeout_seconds = 0.2
    upload_limit_bytes = 16


def _kit_driver(kit: Any) -> type[InfraProvider]:
    return _Notifier if issubclass(kit, NotifierConformance) else _Store


def _built(cls: type[Any], **options: Any) -> Any:
    return cls(name="default", driver="test", options=options)


async def _failures(
    kit: Any,
    driver: type[InfraProvider],
    failing: type[InfraProvider] | None,
    caplog: pytest.LogCaptureFixture,
) -> set[str]:
    """Run every test of ``kit`` against ``driver`` as pytest would (a fresh
    opened provider per test), and answer the names of those that failed."""
    options = {"limit": kit.upload_limit_bytes} if kit is _StoreKit else {}

    def make() -> Any:
        return _built(driver, **options)

    make_failing = (lambda: _built(failing)) if failing is not None else None
    instance = kit()
    failed: set[str] = set()
    for name in sorted(n for n in dir(kit) if n.startswith("test_")):
        method = getattr(instance, name)
        parameters = inspect.signature(method).parameters
        opened = None
        arguments: dict[str, Any] = {}
        if "provider" in parameters:
            opened = make()
            await opened.aopen()
            arguments["provider"] = opened
        if "make_provider" in parameters:
            arguments["make_provider"] = make
        if "make_failing_provider" in parameters:
            arguments["make_failing_provider"] = make_failing
        if "caplog" in parameters:
            arguments["caplog"] = caplog
        # A task keeps whatever the test raised, pytest's outcomes included.
        run = asyncio.ensure_future(method(**arguments))
        await asyncio.wait([run])
        outcome = run.exception()
        if outcome is not None and not isinstance(outcome, pytest.skip.Exception):
            failed.add(name)
        if opened is not None:
            with suppress(Exception):
                await opened.aclose()
    return failed


@pytest.mark.parametrize(
    ("kit", "driver"), [(_NotifierKit, _Notifier), (_StoreKit, _Store)]
)
async def test_the_kit_passes_a_driver_that_keeps_the_contract(
    kit: Any, driver: type[InfraProvider], caplog: pytest.LogCaptureFixture
) -> None:
    failing = _Failing if kit is _NotifierKit else None
    assert await _failures(kit, driver, failing, caplog) == set()


_HARNESS = "test_harness_"


@pytest.mark.parametrize(
    ("kit", "driver", "failing", "check"),
    [
        (_NotifierKit, _Broken.WrongContract, None, "implements_the_contract"),
        (_NotifierKit, _Broken.NoVersion, None, "states_the_contract_version"),
        (_NotifierKit, _Broken.StringVersion, None, "states_the_contract_version"),
        (_NotifierKit, _Broken.BoolVersion, None, "states_the_contract_version"),
        (_NotifierKit, _Broken.OldMajor, None, "states_the_contract_version"),
        (_NotifierKit, _Broken.UnknownFeature, None, "declares_only_spec_features"),
        (_NotifierKit, _Broken.Drifting, None, "reports_the_features_it_declares"),
        (
            _NotifierKit,
            _Broken.OverClaiming,
            None,
            "reports_the_features_it_declares",
        ),
        (_NotifierKit, _Broken.HintsNoOperation, None, "hints_name_only_operations"),
        (
            _NotifierKit,
            _Broken.DevelopmentOnlyNotABool,
            None,
            "states_development_only_as_a_bool",
        ),
        (_NotifierKit, _Broken.SlowHealth, None, "reports_health_within_the_bound"),
        (
            _NotifierKit,
            _Broken.HealthWithAUrl,
            None,
            "reports_health_within_the_bound",
        ),
        (
            _NotifierKit,
            _Broken.HealthWithTheSecret,
            None,
            "reports_health_within_the_bound",
        ),
        (
            _NotifierKit,
            _Notifier,
            _Broken.FailingHealthWithTheSecret,
            "redacts_the_health_of_a_failing_backend",
        ),
        (_NotifierKit, _Broken.NoIdentity, None, "names_its_isolation_domain"),
        (_NotifierKit, _Broken.UnprefixedIdentity, None, "names_its_isolation_domain"),
        (
            _NotifierKit,
            _Broken.IdentityPerInstance,
            None,
            "names_its_isolation_domain",
        ),
        (_NotifierKit, _Broken.RaisingIdentity, None, "names_its_isolation_domain"),
        (_NotifierKit, _Broken.ClosesOnce, None, "opens_and_closes_twice"),
        (
            _NotifierKit,
            _Broken.SendsAnyChannel,
            None,
            "undeclared_behaviours_raise_unsupported",
        ),
        (
            _StoreKit,
            _Broken.RefusesItsDeclaredLink,
            None,
            "declared_behaviours_do_not_raise_unsupported",
        ),
        (
            _StoreKit,
            _Broken.LinkWithoutTheFeature,
            None,
            "undeclared_behaviours_raise_unsupported",
        ),
        (
            _StoreKit,
            _Broken.RefusesTheWrongFeature,
            None,
            "undeclared_behaviours_raise_unsupported",
        ),
        (
            _NotifierKit,
            _Notifier,
            _Broken.FailingWithABackendError,
            "maps_backend_failures_to_infra_errors",
        ),
        (
            _NotifierKit,
            _Notifier,
            _Broken.FailingThatSucceeds,
            "maps_backend_failures_to_infra_errors",
        ),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
async def test_each_harness_check_fails_exactly_its_broken_driver(
    kit: Any,
    driver: type[InfraProvider],
    failing: type[InfraProvider] | None,
    check: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if failing is None and kit is _NotifierKit:
        failing = _Failing
    assert await _failures(kit, driver, failing, caplog) == {_HARNESS + check}


@pytest.mark.parametrize(
    ("kit", "driver", "check"),
    [
        (_NotifierKit, _Broken.RefusesADedupeKey, "test_accepts_a_dedupe_key"),
        (
            _StoreKit,
            _Broken.IgnoresItsLimit,
            "test_object_upload_over_the_limit_is_too_large",
        ),
        (
            _StoreKit,
            _Broken.MetadataWithoutAReader,
            "test_object_metadata_round_trip",
        ),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
async def test_each_capability_check_fails_exactly_its_broken_driver(
    kit: Any, driver: type[InfraProvider], check: str, caplog: pytest.LogCaptureFixture
) -> None:
    failing = _Failing if kit is _NotifierKit else None
    assert await _failures(kit, driver, failing, caplog) == {check}


async def test_the_kit_gives_each_channel_a_recipient_of_its_kind() -> None:
    class Strict(_Notifier):
        supported_features = frozenset({"email", "sms"})

        async def send(self, notification: Notification) -> None:
            is_phone = notification.recipient.startswith("+")
            assert is_phone == (notification.channel == "sms")

    await NotifierConformance().test_sends_on_each_advertised_channel(_built(Strict))


async def test_the_kit_checks_the_logs_on_every_channel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class LeaksOnSms(_Notifier):
        supported_features = frozenset({"email", "sms"})

        async def send(self, notification: Notification) -> None:
            if notification.channel == "sms":
                logging.getLogger("leaky.driver").debug(notification.body)

    with pytest.raises(AssertionError, match="was logged"):
        await NotifierConformance().test_keeps_the_body_out_of_the_logs(
            _built(LeaksOnSms), caplog
        )


@pytest.mark.parametrize("field", ["body", "body_fields"])
async def test_the_kit_fails_a_driver_that_logs_the_body(
    field: str, caplog: pytest.LogCaptureFixture
) -> None:
    class Leaky(_Notifier):
        async def send(self, notification: Notification) -> None:
            logging.getLogger("leaky.driver").debug(
                "sending %s", getattr(notification, field)
            )

    with pytest.raises(AssertionError, match="was logged"):
        await NotifierConformance().test_keeps_the_body_out_of_the_logs(
            _built(Leaky), caplog
        )


def test_options_reach_the_driver() -> None:
    options: Mapping[str, Any] = {"path": "/m.log"}
    built = _Notifier.from_config(
        InstanceConfig("notification", "default", "x", dict(options))
    )
    assert built.options == options
