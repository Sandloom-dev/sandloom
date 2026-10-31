"""First-class extra capabilities: contracts + plugin SPI (no core drivers)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.extra_capabilities import (
    EXTRA_CAPABILITIES,
    GRAPH_STORE_CAPABILITY,
    GraphStoreProvider,
)
from sandloom.core.infra.registry import (
    DriverNotRegisteredError,
    InfraRegistry,
    InstanceNotConfiguredError,
)


class _FakeNeo4j(GraphStoreProvider):
    """A plugin-supplied graph driver — entirely in-process for the test."""

    def __init__(self, *, name: str, driver: str, options: dict[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._nodes: list[dict[str, Any]] = []

    def features(self) -> frozenset[str]:
        return frozenset({"cypher", "transactions"})

    async def query(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return list(self._nodes)

    async def execute(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> None:
        self._nodes.append(dict(params or {}))


def _plugin_register(registry: InfraRegistry) -> None:
    """The shape an `sandloom.infra` entry-point plugin would expose."""
    registry.register_capability(GRAPH_STORE_CAPABILITY)
    registry.register_driver(
        "graph_store",
        "neo4j",
        lambda cfg: _FakeNeo4j(
            name=cfg.name, driver=cfg.driver, options=dict(cfg.options)
        ),
    )


def test_extra_capabilities_are_specs() -> None:
    names = {s.name for s in EXTRA_CAPABILITIES}
    assert names == {
        "document_store",
        "graph_store",
        "workflow",
        "search",
        "lock",
        "secrets",
    }


@pytest.mark.asyncio
async def test_plugin_adds_capability_and_driver() -> None:
    reg = InfraRegistry(
        config={
            "graph_store": {
                "recommendations": InstanceConfig(
                    capability="graph_store",
                    name="recommendations",
                    driver="neo4j",
                    options={},
                )
            }
        }
    )
    _plugin_register(reg)

    graph = reg.resolve("graph_store", "recommendations", features={"cypher"})
    assert isinstance(graph, GraphStoreProvider)
    await graph.execute("CREATE (:User)", {"id": "u1"})
    rows = await graph.query("MATCH (u:User) RETURN u")
    assert rows == [{"id": "u1"}]


def test_known_capability_without_driver_gives_clear_error() -> None:
    # Spec registered (known capability) but no driver/instance → precise error,
    # not a vague "not found".
    reg = InfraRegistry()
    reg.register_capability(GRAPH_STORE_CAPABILITY)
    with pytest.raises(InstanceNotConfiguredError):
        reg.provider("graph_store", "default")


def test_configured_but_no_driver_gives_clear_error() -> None:
    reg = InfraRegistry(
        config={
            "graph_store": {
                "default": InstanceConfig(
                    capability="graph_store",
                    name="default",
                    driver="neo4j",
                    options={},
                )
            }
        }
    )
    reg.register_capability(GRAPH_STORE_CAPABILITY)
    with pytest.raises(DriverNotRegisteredError):
        reg.provider("graph_store", "default")


# --------------------------------------------------------------------------- #
# Stability metadata: the six driver-less capabilities are EXPERIMENTAL
# --------------------------------------------------------------------------- #
def test_extra_capabilities_are_marked_experimental() -> None:
    from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES

    assert {s.stability for s in EXTRA_CAPABILITIES} == {"experimental"}
    assert all(s.is_experimental for s in EXTRA_CAPABILITIES)
    # Built-ins (shipped with drivers) stay stable.
    assert {s.stability for s in BUILTIN_CAPABILITIES} == {"stable"}


def test_capability_spec_rejects_unknown_stability() -> None:
    from sandloom.core.infra.capabilities import CapabilitySpec

    with pytest.raises(ValueError, match="stability"):
        CapabilitySpec(
            name="x",
            version="v1",
            provider_contract=GraphStoreProvider,
            stability="beta",  # type: ignore[arg-type]
        )


def test_describe_renders_stability() -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.drivers.bootstrap import build_infra_registry

    described = build_infra_registry(settings).describe()["capabilities"]
    assert described["graph_store"]["stability"] == "experimental"
    assert described["graph_store"]["drivers"] == []
    assert described["database"]["stability"] == "stable"
    assert "sqlalchemy" in described["database"]["drivers"]


def test_experimental_without_driver_raises_experimental_error() -> None:
    from sandloom.core.infra.registry import ExperimentalCapabilityError

    reg = InfraRegistry()
    reg.register_capability(GRAPH_STORE_CAPABILITY)
    with pytest.raises(ExperimentalCapabilityError, match="EXPERIMENTAL") as info:
        reg.resolve("graph_store", "default", features=frozenset({"cypher"}))
    # Back-compat: existing handlers for the precise errors still match.
    assert isinstance(info.value, InstanceNotConfiguredError)
    assert isinstance(info.value, DriverNotRegisteredError)
    assert "driver package" in str(info.value)
    with pytest.raises(ExperimentalCapabilityError):
        reg.validate_requirement("graph_store", "default")


def test_experimental_configured_with_unregistered_driver_raises() -> None:
    from sandloom.core.infra.registry import ExperimentalCapabilityError

    reg = InfraRegistry(
        config={
            "graph_store": {
                "default": InstanceConfig(
                    capability="graph_store", name="default", driver="neo4j"
                )
            }
        }
    )
    reg.register_capability(GRAPH_STORE_CAPABILITY)
    with pytest.raises(ExperimentalCapabilityError, match="'neo4j'"):
        reg.provider("graph_store", "default")
    with pytest.raises(ExperimentalCapabilityError):
        reg.validate_requirement("graph_store", "default")


def test_experimental_with_driver_but_unknown_instance_keeps_precise_error() -> None:
    from sandloom.core.infra.registry import ExperimentalCapabilityError

    reg = InfraRegistry()
    _plugin_register(reg)
    with pytest.raises(InstanceNotConfiguredError) as info:
        reg.provider("graph_store", "missing")
    assert not isinstance(info.value, ExperimentalCapabilityError)


def test_facade_require_capability_on_experimental_raises() -> None:
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade
    from sandloom.core.infra.registry import ExperimentalCapabilityError

    reg = InfraRegistry()
    reg.register_capability(GRAPH_STORE_CAPABILITY)
    core = CoreFacade(FastAPI())
    core.set_infra(reg)
    with pytest.raises(ExperimentalCapabilityError, match="EXPERIMENTAL"):
        core.require_capability("graph_store", features={"cypher"})
