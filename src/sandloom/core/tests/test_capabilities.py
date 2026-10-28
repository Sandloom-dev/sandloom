"""Capability runtime: versioned specs, contract + feature enforcement.

These tests use entirely in-process fake providers — no real backend — proving
the capability layer is technology-agnostic and that the registry enforces the
contract/feature checks regardless of driver.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.capabilities import (
    BUILTIN_CAPABILITIES,
    CACHE_CAPABILITY,
    CapabilitySpec,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import CacheProvider, InfraProvider
from sandloom.core.infra.registry import (
    CapabilityConflictError,
    ConfigSchemaError,
    FeatureUnsupportedError,
    InfraRegistry,
    ProviderContractError,
)


# --------------------------------------------------------------------------- #
# A made-up capability with no real backend.
# --------------------------------------------------------------------------- #
class GraphStoreProvider(InfraProvider):
    capability = "graph_store"


class _FakeGraph(GraphStoreProvider):
    def __init__(self, *, name, driver, options, supports):  # type: ignore[no-untyped-def]
        super().__init__(name=name, driver=driver, options=options)
        self._supports = frozenset(supports)

    def features(self) -> frozenset[str]:
        return self._supports


class _GraphOptions(BaseModel):
    uri: str


GRAPH_STORE = CapabilitySpec(
    name="graph_store",
    version="v1",
    provider_contract=GraphStoreProvider,
    config_schema=_GraphOptions,
    features=frozenset({"cypher", "transactions", "vector_index"}),
    description="Graph database.",
)


def _graph_registry(*, supports=("cypher", "transactions")) -> InfraRegistry:  # type: ignore[no-untyped-def]
    reg = InfraRegistry(
        config={
            "graph_store": {
                "recommendations": InstanceConfig(
                    capability="graph_store",
                    name="recommendations",
                    driver="neo4j",
                    options={"uri": "bolt://x:7687"},
                )
            }
        }
    )
    reg.register_capability(GRAPH_STORE)
    reg.register_driver(
        "graph_store",
        "neo4j",
        lambda cfg: _FakeGraph(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            supports=supports,
        ),
    )
    return reg


# --------------------------------------------------------------------------- #
# Spec registration
# --------------------------------------------------------------------------- #
def test_register_and_query_capability() -> None:
    reg = _graph_registry()
    spec = reg.capability_spec("graph_store")
    assert spec is GRAPH_STORE
    assert "graph_store" in [s.name for s in reg.capabilities()]


def test_register_same_capability_is_idempotent() -> None:
    reg = InfraRegistry()
    reg.register_capability(GRAPH_STORE)
    reg.register_capability(GRAPH_STORE)  # no raise


def test_conflicting_capability_raises() -> None:
    reg = InfraRegistry()
    reg.register_capability(GRAPH_STORE)
    clash = CapabilitySpec(
        name="graph_store", version="v2", provider_contract=CacheProvider
    )
    with pytest.raises(CapabilityConflictError):
        reg.register_capability(clash)


def test_builtin_capabilities_are_specs() -> None:
    names = {s.name for s in BUILTIN_CAPABILITIES}
    assert names == {
        "database",
        "cache",
        "objectstore",
        "taskqueue",
        "eventbus",
        "notification",
    }
    assert CACHE_CAPABILITY.provider_contract is CacheProvider


# --------------------------------------------------------------------------- #
# Contract enforcement
# --------------------------------------------------------------------------- #
def test_wrong_provider_contract_raises() -> None:
    reg = InfraRegistry(
        config={
            "graph_store": {
                "x": InstanceConfig(
                    capability="graph_store", name="x", driver="bad", options={}
                )
            }
        }
    )
    reg.register_capability(
        CapabilitySpec(
            name="graph_store", version="v1", provider_contract=GraphStoreProvider
        )
    )

    class _NotAGraph(InfraProvider):
        capability = "graph_store"

    reg.register_driver(
        "graph_store",
        "bad",
        lambda cfg: _NotAGraph(name=cfg.name, driver=cfg.driver, options={}),
    )
    with pytest.raises(ProviderContractError):
        reg.provider("graph_store", "x")


def test_resolve_explicit_contract_mismatch_raises() -> None:
    reg = _graph_registry()
    with pytest.raises(ProviderContractError):
        reg.resolve("graph_store", "recommendations", contract=CacheProvider)


# --------------------------------------------------------------------------- #
# Feature enforcement
# --------------------------------------------------------------------------- #
def test_resolve_supported_feature_ok() -> None:
    reg = _graph_registry(supports=("cypher", "transactions"))
    provider = reg.resolve("graph_store", "recommendations", features={"cypher"})
    assert isinstance(provider, GraphStoreProvider)


def test_resolve_unsupported_feature_raises() -> None:
    reg = _graph_registry(supports=("cypher",))
    with pytest.raises(FeatureUnsupportedError) as exc:
        reg.resolve("graph_store", "recommendations", features={"transactions"})
    assert "transactions" in str(exc.value)
    assert exc.value.fix  # actionable hint present


def test_resolve_unknown_feature_raises() -> None:
    reg = _graph_registry()
    with pytest.raises(FeatureUnsupportedError):
        reg.resolve("graph_store", "recommendations", features={"sql_joins"})


# --------------------------------------------------------------------------- #
# Config schema validation
# --------------------------------------------------------------------------- #
def test_bad_options_raise_config_schema_error() -> None:
    reg = InfraRegistry(
        config={
            "graph_store": {
                "broken": InstanceConfig(
                    capability="graph_store",
                    name="broken",
                    driver="neo4j",
                    options={},  # missing required 'uri'
                )
            }
        }
    )
    reg.register_capability(GRAPH_STORE)
    reg.register_driver(
        "graph_store",
        "neo4j",
        lambda cfg: _FakeGraph(
            name=cfg.name, driver=cfg.driver, options={}, supports=()
        ),
    )
    with pytest.raises(ConfigSchemaError):
        reg.provider("graph_store", "broken")


# --------------------------------------------------------------------------- #
# Back-compat: a registry with no spec still resolves (no contract check)
# --------------------------------------------------------------------------- #
def test_no_spec_skips_contract_check() -> None:
    reg = InfraRegistry(
        config={
            "cache": {
                "default": InstanceConfig(
                    capability="cache", name="default", driver="fake", options={}
                )
            }
        }
    )

    class _AnyProvider(InfraProvider):
        capability = "cache"

    reg.register_driver(
        "cache",
        "fake",
        lambda cfg: _AnyProvider(name=cfg.name, driver=cfg.driver, options={}),
    )
    # No spec registered for "cache" → contract check skipped, resolves fine.
    assert isinstance(reg.provider("cache", "default"), _AnyProvider)


def test_describe_includes_capabilities() -> None:
    reg = _graph_registry()
    desc = reg.describe()
    assert "graph_store" in desc["capabilities"]
    assert desc["capabilities"]["graph_store"]["contract"] == "GraphStoreProvider"
    assert "cypher" in desc["capabilities"]["graph_store"]["features"]


# --------------------------------------------------------------------------- #
# Facade routing
# --------------------------------------------------------------------------- #
def test_facade_capability_and_require_capability() -> None:
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade

    reg = _graph_registry(supports=("cypher", "transactions"))
    core = CoreFacade(app=FastAPI())
    core.set_infra(reg)

    provider = core.capability("graph_store", "recommendations")
    assert isinstance(provider, GraphStoreProvider)

    ok = core.require_capability("graph_store", "recommendations", features={"cypher"})
    assert ok is provider  # cached, same instance

    with pytest.raises(FeatureUnsupportedError):
        core.require_capability(
            "graph_store", "recommendations", features={"vector_index"}
        )


@pytest.mark.asyncio
async def test_builtin_health_result_shape() -> None:
    # Sanity: HealthResult import path used by fakes stays valid.
    assert HealthResult(status="ok").status == "ok"
