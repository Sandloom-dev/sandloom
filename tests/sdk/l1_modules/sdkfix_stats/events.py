"""The fixture stats module's own channel."""

from __future__ import annotations

from uuid import UUID

from sandloom.sdk import Event


class CounterBumped(Event, channel="sdkfix_stats.counter.bumped.v1"):
    tenant_id: UUID
    total: int
