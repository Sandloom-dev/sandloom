from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade


@pytest.mark.asyncio
async def test_publish_missing_schema_raises_in_every_profile(
    bus: InMemoryEventBus,
) -> None:
    # Dev = prod: no profile accepts an unregistered versioned channel.
    assert not settings.is_prod
    with pytest.raises(ValueError, match="SANDLOOM-R003"):
        await bus.publish_event("strict.example2.v1", {"a": 1})


@pytest.mark.asyncio
async def test_publish_with_schema_ok(
    core_facade: CoreFacade,
    bus: InMemoryEventBus,
) -> None:
    channel = "strict.has.schema.v1"
    core_facade.register_event_schema(
        channel=channel,
        name="simple",
        schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        },
    )
    await bus.publish_event(channel, {"a": 5})  # no raise
