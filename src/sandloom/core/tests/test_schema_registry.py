from __future__ import annotations

import pytest

from sandloom.core.facade import CoreFacade


@pytest.mark.asyncio
async def test_list_event_schemas_basic(core_facade: CoreFacade) -> None:
    facade = core_facade
    # Register two schemas
    facade.register_event_schema(
        channel="acme.orders.created.v1",
        name="order_created",
        schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    facade.register_event_schema(
        channel="acme.orders.cancelled.v1",
        name="order_cancelled",
        schema={
            "type": "object",
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": True,
        },
    )

    schemas = facade.list_event_schemas()
    channels = {s.channel for s in schemas}
    assert "acme.orders.created.v1" in channels
    assert "acme.orders.cancelled.v1" in channels
    # Ensure objects have expected attributes
    for s in schemas:
        assert hasattr(s, "version")
        assert s.channel.endswith(f".v{s.version}")
