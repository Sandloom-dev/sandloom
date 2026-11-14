"""The S3 driver is beta: production starts it without an accepted risk."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sandloom_s3 import S3ObjectStore

from sandloom.core.descriptors import module_names
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.capabilities import OBJECTSTORE_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import InfraRegistry, load_selected_plugins
from sandloom.core.schema.registry import InMemorySchemaRegistry

_OPTIONS = {"bucket": "bucket", "region": "us-east-1"}
_CONFIG = {
    "objectstore": {"default": InstanceConfig("objectstore", "default", "s3", _OPTIONS)}
}


@pytest.fixture(autouse=True)
def _entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_the_entry_point_driver_registers_as_beta() -> None:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(OBJECTSTORE_CAPABILITY)
    load_selected_plugins(registry, _CONFIG)
    assert S3ObjectStore.tier == "beta"
    assert registry.driver_tier("objectstore", "s3") == "beta"


def test_production_starts_s3_without_an_accepted_risk() -> None:
    settings: Any = SimpleNamespace(
        is_prod=True, accepts_risk=lambda _risk: False, infra={}
    )
    build_infra_registry(InMemorySchemaRegistry(), settings=settings, config=_CONFIG)
