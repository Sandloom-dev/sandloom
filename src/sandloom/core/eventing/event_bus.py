"""The process-wide event bus binding: ``set_active_event_bus`` / ``get_event_bus``.

``create_app`` binds the bus it built through the infrastructure registry;
module-level callers that resolve the bus lazily (the outbox relay, a bare
facade) read it here. Outside production a missing bus falls back to an
in-memory one. The bus implementations live in their own modules
(``bus_base``, ``memory_bus``, ``pubsub_bus``, ``streams_bus``).
"""

from __future__ import annotations

import logging

import sandloom.core.schema.registry as _registry
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.eventing.bus_base import EventBusProtocol
from sandloom.core.eventing.memory_bus import InMemoryEventBus

logger = logging.getLogger(__name__)

event_bus: EventBusProtocol | None = None


def set_active_event_bus(bus: EventBusProtocol) -> None:
    """Bind the process-wide event bus accessor to ``bus``.

    The bus is built through the infrastructure registry (the ``eventbus``
    capability) and bound here at startup. Module-level callers that resolve the
    bus lazily (``publish_event`` in DI, the outbox relay) then see the same
    instance the application owns.
    """
    global event_bus
    event_bus = bus


def get_event_bus() -> EventBusProtocol:
    """Return the active event bus.

    Resolves the module-level binding at call time so callers that imported
    this accessor before startup wiring ran still get the live instance.

    In non-prod environments a missing bus falls back to a lazily created
    in-memory instance so unit tests and scripts work without full app wiring.
    In prod a missing bus is a startup wiring error and raises.
    """
    global event_bus
    if event_bus is None:
        if settings.environment == Environment.prod:
            raise RuntimeError(
                "Event bus is not initialized; bind one via set_active_event_bus()"
                " during startup"
            )

        event_bus = InMemoryEventBus(_registry.InMemorySchemaRegistry())
    return event_bus


__all__ = [
    "event_bus",
    "get_event_bus",
    "set_active_event_bus",
]
