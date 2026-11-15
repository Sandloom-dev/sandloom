"""The S3 driver declares its SDK, so a missing one is B037, not B022."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sandloom_s3 import S3ObjectStore

from sandloom.core.descriptors import module_names
from sandloom.core.infra.capabilities import OBJECTSTORE_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import (
    DriverUnavailableError,
    InfraRegistry,
    load_selected_plugins,
)

_OPTIONS = {"bucket": "bucket", "region": "us-east-1"}


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> InfraRegistry:
    entry = SimpleNamespace(
        name="objectstore.s3",
        value="sandloom_s3:S3ObjectStore",
        dist=SimpleNamespace(name="sandloom-s3"),
        load=lambda: S3ObjectStore,
    )

    class _Entries:
        def select(self, *, group: str, name: str | None = None) -> list[Any]:
            if group != "sandloom.drivers":
                return []
            return [entry] if name in (None, entry.name) else []

    monkeypatch.setattr(
        "sandloom.core.infra.registry.metadata.entry_points", lambda: _Entries()
    )
    monkeypatch.setattr(module_names, "_cache", None)
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(OBJECTSTORE_CAPABILITY)
    config = {
        "objectstore": {
            "default": InstanceConfig("objectstore", "default", "s3", _OPTIONS)
        }
    }
    load_selected_plugins(registry, config)
    registry.set_config(config)
    return registry


@pytest.fixture
def without_boto3(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("boto3", "botocore"):
        for loaded in [m for m in sys.modules if m.split(".")[0] == name]:
            monkeypatch.delitem(sys.modules, loaded)
        monkeypatch.setitem(sys.modules, name, None)  # type: ignore[arg-type]
    yield


def test_the_driver_declares_its_sdk() -> None:
    assert S3ObjectStore.required_imports == ("boto3",)
    assert "sandloom-s3" in S3ObjectStore.install_hint


def test_preflight_passes_with_boto3_present(registry: InfraRegistry) -> None:
    registry.validate_requirement("objectstore")
    provider = registry.resolve("objectstore")
    assert isinstance(provider, S3ObjectStore)
    assert provider._connect().meta.service_model.service_name == "s3"


def test_a_missing_boto3_is_b037_with_the_install_hint(
    registry: InfraRegistry, without_boto3: None
) -> None:
    with pytest.raises(DriverUnavailableError) as caught:
        registry.validate_requirement("objectstore")
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    assert "pip install sandloom-s3" in str(caught.value)


def test_the_entry_point_imports_without_boto3() -> None:
    # A fresh interpreter: importing the driver must not need the SDK (else B022).
    code = (
        "import sys; sys.modules['boto3'] = None; sys.modules['botocore'] = None\n"
        "import sandloom_s3\n"
        "assert sandloom_s3.S3ObjectStore.required_imports == ('boto3',)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
