"""L2 event primitives of ``sandloom.sdk.events`` (spec §3).

``class E(Event, channel=C)`` (L1) registers ``model_json_schema()`` through
``ModuleAPI.register_event_schema``; ``await E(...).publish()`` is
``await publish_event(C, e.model_dump(mode="json"))``; ``@on(E)`` registers
through ``ModuleAPI.register_event_consumer`` with a callback that decodes
``envelope["payload"]`` into ``E``.
"""

from __future__ import annotations

from sandloom.core.di import (
    EventEnvelope,
    PoisonEventError,
    publish_event,
    publish_event_versions,
)

__all__ = [
    "EventEnvelope",
    "PoisonEventError",
    "publish_event",
    "publish_event_versions",
]
