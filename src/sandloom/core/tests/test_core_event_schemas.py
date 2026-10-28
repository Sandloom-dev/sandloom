from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.eventing.core_events import (
    AUDIT_EVENT_SCHEMA,
    HEARTBEAT_SCHEMA,
    MODULE_LOADED_SCHEMA,
    NOTIFICATION_EVENT_SCHEMA,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry


@pytest.fixture()
def core() -> CoreFacade:
    facade = CoreFacade(app=FastAPI())
    facade.register_schema_registry(InMemorySchemaRegistry())
    return facade


def test_baseline_core_event_schemas_registered(core: CoreFacade) -> None:
    # registration happens in facade init via main startup usually; invoke directly here
    from sandloom.core.eventing.core_events import register_core_event_schemas

    register_core_event_schemas(core)

    for channel, schema in [
        ("core.system.heartbeat.v1", HEARTBEAT_SCHEMA),
        ("core.system.module_loaded.v1", MODULE_LOADED_SCHEMA),
        ("core.audit.v1", AUDIT_EVENT_SCHEMA),
        ("core.notification.v1", NOTIFICATION_EVENT_SCHEMA),
    ]:
        stored = core.get_event_schema(channel)
        assert stored is not None, f"schema for {channel} should be registered"
        assert stored.schema == schema
    # Fallback check through listing
    channels = {s.channel for s in core.list_event_schemas()}
    assert "core.system.heartbeat.v1" in channels


def test_re_register_idempotent(core: CoreFacade) -> None:
    from sandloom.core.eventing.core_events import register_core_event_schemas

    register_core_event_schemas(core)
    # second call should not raise
    register_core_event_schemas(core)

    # Heartbeat schema remains unchanged
    stored = core.get_event_schema("core.system.heartbeat.v1")
    assert stored is not None
    assert stored.schema is HEARTBEAT_SCHEMA or stored.schema == HEARTBEAT_SCHEMA
