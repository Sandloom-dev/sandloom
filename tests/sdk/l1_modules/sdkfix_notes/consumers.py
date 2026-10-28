"""Consumers of the fixture module: a view of a foreign channel and its own."""

from __future__ import annotations

from uuid import UUID

from sandloom.sdk import Event, on

from .events import NoteCreated

SEEN: list[Event] = []


class CounterBumped(Event, channel="sdkfix_stats.counter.bumped.v1"):  # a view
    tenant_id: UUID
    total: int


@on(CounterBumped)
async def remember_total(event: CounterBumped) -> None:
    SEEN.append(event)


@on(NoteCreated, name="notes-audit", durable=False, dead_letter=False, tenant=None)
async def audit_note(event: NoteCreated) -> None:
    SEEN.append(event)
