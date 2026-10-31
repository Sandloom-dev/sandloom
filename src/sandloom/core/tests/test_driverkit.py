"""``sandloom.driverkit``: finding drivers by name, the guide's example, the kit."""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

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
    ProviderContractError,
    load_selected_plugins,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit import (
    CapabilityRef,
    CapabilitySpec,
    InfraProvider,
    Notification,
    NotifierProvider,
)
from sandloom.driverkit.testing import NotifierConformance

SNIPPET = Path(__file__).resolve().parents[4] / "docs/snippets/drivers/file_notifier.py"


def _guide_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("file_notifier", SNIPPET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Notifier(NotifierProvider):
    contract_version = "v1"
    supported_features = frozenset({"email"})

    async def send(self, notification: Notification) -> None:
        return None


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


def test_a_capability_in_another_package_loads_in_any_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Search(InfraProvider):
        capability = "vector_index"
        contract_version = "v1"

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
    contract_version = "v2"


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (ImportError("no such backend"), "failed to import"),
        (object, "not a subclass"),
        (_NoVersion, "no contract_version"),
        (_OldVersion, "written for v2"),
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
    abstract = sorted(NotifierProvider.__abstractmethods__)
    signatures = {
        name: str(inspect.signature(getattr(NotifierProvider, name)))
        for name in abstract
    }
    assert signatures == _NOTIFICATION_V1


def test_every_public_contract_has_a_kit() -> None:
    contracts = [
        name
        for name in sandloom.driverkit.__all__
        if name.endswith("Provider") and name != "InfraProvider"
    ]
    assert contracts
    for name in contracts:
        kit = name.removesuffix("Provider") + "Conformance"
        assert kit in sandloom.driverkit.testing.__all__, f"{name} has no kit: {kit}"


@pytest.mark.parametrize(
    "spec", [*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES], ids=lambda s: s.name
)
def test_a_required_method_is_not_called_a_feature(spec: CapabilitySpec) -> None:
    required = spec.provider_contract.__abstractmethods__  # type: ignore[attr-defined]
    assert not spec.features & set(required)


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
    with pytest.raises(ValueError, match="'path' option"):
        _guide_module().FileNotifier(name="default", driver="file", options={})


class _Broken:
    """The kit must fail these: a kit that passes everything proves nothing."""

    class WrongContract(InfraProvider):
        contract_version = "v1"

    class UnknownFeature(_Notifier):
        supported_features = frozenset({"fax"})

    class Drifting(_Notifier):
        def features(self) -> frozenset[str]:
            return frozenset()

    class NoVersion(_NoVersion):
        supported_features = frozenset({"email"})

    class ClosesOnce(_Notifier):
        closed = False

        async def aclose(self) -> None:
            if self.closed:
                raise RuntimeError("already closed")
            self.closed = True


def _built(cls: type[Any]) -> Any:
    return cls(name="default", driver="test", options={})


@pytest.mark.parametrize(
    ("driver", "check"),
    [
        (_Broken.WrongContract, "test_implements_the_contract"),
        (_Broken.NoVersion, "test_states_the_contract_version"),
        (_Broken.UnknownFeature, "test_advertises_only_defined_features"),
        (_Broken.Drifting, "test_reports_the_features_it_declares"),
    ],
)
async def test_the_kit_fails_a_broken_driver(driver: type[Any], check: str) -> None:
    with pytest.raises(AssertionError):
        await getattr(NotifierConformance(), check)(_built(driver))


async def test_the_kit_fails_a_second_close_that_raises() -> None:
    with pytest.raises(RuntimeError, match="already closed"):
        await NotifierConformance().test_closes_more_than_once(
            _built(_Broken.ClosesOnce)
        )


@pytest.mark.parametrize("field", ["body", "body_fields"])
async def test_the_kit_fails_a_driver_that_logs_the_body(
    field: str, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

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
