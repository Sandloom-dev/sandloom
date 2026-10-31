"""The driver guide's example, through the public ``sandloom.sdk.drivers``."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.registry import FeatureUnsupportedError
from sandloom.sdk.drivers import (
    CapabilitySpec,
    InfraProvider,
    InfraRegistry,
    InstanceConfig,
    Notification,
)

SNIPPET = Path(__file__).resolve().parents[4] / "docs/snippets/drivers/file_notifier.py"


def _guide_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("file_notifier", SNIPPET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry(**options: str) -> InfraRegistry:
    registry = InfraRegistry(
        config={
            "notification": {
                "default": InstanceConfig(
                    capability="notification",
                    name="default",
                    driver="file",
                    options=dict(options),
                )
            }
        }
    )
    registry.register_capability(NOTIFICATION_CAPABILITY)
    _guide_example().register(registry)
    return registry


@pytest.mark.asyncio
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


def test_a_capability_of_your_own_resolves_through_the_public_api() -> None:
    class SearchProvider(InfraProvider):
        capability = "search_index"

    registry = InfraRegistry(
        config={
            "search_index": {
                "default": InstanceConfig(
                    capability="search_index", name="default", driver="memory"
                )
            }
        }
    )
    registry.register_capability(
        CapabilitySpec(
            name="search_index",
            version="v1",
            provider_contract=SearchProvider,
            stability="experimental",
        )
    )
    registry.register_driver(
        "search_index",
        "memory",
        lambda config: SearchProvider(
            name=config.name, driver=config.driver, options=config.options
        ),
    )
    assert isinstance(registry.provider("search_index"), SearchProvider)
