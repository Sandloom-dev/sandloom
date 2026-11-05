"""Shared fixtures for core tests.

The core moved from a global schema-registry singleton to explicit DI wiring
(facade.register_schema_registry + bus constructor injection). These fixtures
provide a facade and bus wired to the same registry, mirroring app startup.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing.fixtures import (  # noqa: F401 - autouse fixture
    _sandloom_db_feature_skip,
)


@pytest.fixture()
def schema_registry() -> InMemorySchemaRegistry:
    return InMemorySchemaRegistry()


@pytest.fixture()
def core_facade(schema_registry: InMemorySchemaRegistry) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(schema_registry)
    return core


@pytest.fixture()
def bus(schema_registry: InMemorySchemaRegistry) -> InMemoryEventBus:
    return InMemoryEventBus(schema_registry)


def pytest_configure(config: pytest.Config) -> None:
    # skip_unless_db_feature's marker (the plugin registers it for apps)
    config.addinivalue_line(
        "markers",
        "sandloom_db_feature(*features, database='default'): skip unless the "
        "database instance has the features (skip_unless_db_feature)",
    )
