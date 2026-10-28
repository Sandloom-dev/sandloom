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
