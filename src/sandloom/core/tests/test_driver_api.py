"""``sandloom.driver``: the driver guide's example and the conformance kit."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import pytest

from sandloom.core.drivers.log_notifier import create_log_notifier
from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.registry import (
    CapabilityConflictError,
    FeatureUnsupportedError,
)
from sandloom.driver import (
    CapabilitySpec,
    InfraProvider,
    InfraRegistry,
    InstanceConfig,
    Notification,
    NotifierProvider,
)
from sandloom.driver.testing import assert_notifier_conformance

SNIPPET = Path(__file__).resolve().parents[4] / "docs/snippets/drivers/file_notifier.py"


def _guide_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("file_notifier", SNIPPET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(**options: str) -> InstanceConfig:
    return InstanceConfig(
        capability="notification", name="default", driver="file", options=dict(options)
    )


def _registry(**options: str) -> InfraRegistry:
    registry = InfraRegistry(config={"notification": {"default": _config(**options)}})
    registry.register_capability(NOTIFICATION_CAPABILITY)
    _guide_example().register(registry)
    return registry


async def test_the_guide_driver_is_selected_and_delivers(tmp_path: Path) -> None:
    target = tmp_path / "mail.log"
    provider = _registry(path=str(target)).resolve("notification", features={"email"})
    await provider.send(  # type: ignore[attr-defined]
        Notification(channel="email", recipient="a@b.test", subject="Hi", body="x")
    )
    assert target.read_text() == "email|a@b.test|Hi\n"


def test_the_guide_driver_refuses_a_missing_path() -> None:
    with pytest.raises(ValueError, match="'path' option"):
        _registry().provider("notification")


def test_a_feature_the_driver_lacks_is_refused(tmp_path: Path) -> None:
    registry = _registry(path=str(tmp_path / "mail.log"))
    with pytest.raises(FeatureUnsupportedError):
        registry.resolve("notification", features={"sms"})


def test_a_driver_for_another_major_version_is_refused() -> None:
    registry = InfraRegistry()
    registry.register_capability(NOTIFICATION_CAPABILITY)
    with pytest.raises(CapabilityConflictError, match="v2"):
        registry.register_driver(
            "notification",
            "old",
            _guide_example().FileNotifier,
            capability_version="v2",
        )
    assert not registry.has_driver("notification", "old")


def test_a_capability_of_your_own_resolves_through_the_public_api() -> None:
    class VectorIndexProvider(InfraProvider):
        capability = "vector_index"

    registry = InfraRegistry(
        config={
            "vector_index": {
                "default": InstanceConfig(
                    capability="vector_index", name="default", driver="memory"
                )
            }
        }
    )
    registry.register_capability(
        CapabilitySpec(
            name="vector_index",
            version="v1",
            provider_contract=VectorIndexProvider,
            stability="experimental",
        )
    )
    registry.register_driver(
        "vector_index",
        "memory",
        lambda config: VectorIndexProvider(
            name=config.name, driver=config.driver, options=config.options
        ),
    )
    assert isinstance(registry.provider("vector_index"), VectorIndexProvider)


class _Notifier(NotifierProvider):
    def __init__(self) -> None:
        super().__init__(name="default", driver="test", options={})
        self.sent: list[Notification] = []

    def features(self) -> frozenset[str]:
        return frozenset({"email"})

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


async def test_the_kit_passes_a_correct_driver_and_sends_each_channel(
    tmp_path: Path,
) -> None:
    await assert_notifier_conformance(
        _guide_example().FileNotifier(_config(path=str(tmp_path / "mail.log")))
    )
    notifier = _Notifier()
    await assert_notifier_conformance(notifier)
    assert [n.channel for n in notifier.sent] == ["email"]


async def test_the_kit_passes_the_shipped_log_driver() -> None:
    await assert_notifier_conformance(create_log_notifier(_config()))


async def test_the_kit_refuses_another_contract() -> None:
    class NotANotifier(InfraProvider):
        pass

    provider = NotANotifier(name="default", driver="test", options={})
    with pytest.raises(AssertionError, match="NotifierProvider"):
        await assert_notifier_conformance(provider)  # type: ignore[arg-type]


async def test_the_kit_refuses_an_unknown_feature() -> None:
    class Unknown(_Notifier):
        def features(self) -> frozenset[str]:
            return frozenset({"fax"})

    with pytest.raises(AssertionError, match="fax"):
        await assert_notifier_conformance(Unknown())


@pytest.mark.parametrize("field", ["body", "body_fields"])
async def test_the_kit_refuses_a_driver_that_logs_secrets(field: str) -> None:
    class Leaky(_Notifier):
        async def send(self, notification: Notification) -> None:
            logging.getLogger("leaky.driver").debug(
                "sending %s", getattr(notification, field)
            )

    with pytest.raises(AssertionError, match="logged"):
        await assert_notifier_conformance(Leaky())


async def test_the_kit_refuses_a_second_aclose_that_fails() -> None:
    class ClosesOnce(_Notifier):
        closed = False

        async def aclose(self) -> None:
            if self.closed:
                raise RuntimeError("already closed")
            self.closed = True

    with pytest.raises(AssertionError, match="aclose"):
        await assert_notifier_conformance(ClosesOnce())
