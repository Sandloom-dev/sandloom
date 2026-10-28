"""Preserve transactional event provenance across deferred/batched publishing.

The context contains only the current publish batch and is reset on exit. It
keeps third-party store/publisher call signatures compatible while the bundled
stores carry persisted origin metadata to every bundled bus implementation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

EventOrigin = tuple[str | None, datetime | None]
_origins: ContextVar[Mapping[str, EventOrigin] | None] = ContextVar(
    "sandloom_event_origins", default=None
)


@contextmanager
def event_origins(origins: Mapping[str, EventOrigin]) -> Iterator[None]:
    token = _origins.set({**(_origins.get() or {}), **origins})
    try:
        yield
    finally:
        _origins.reset(token)


def envelope_origin(
    event_id: str | None, trace_id: str | None, emitted_at: str
) -> tuple[str | None, str]:
    origin = (_origins.get() or {}).get(event_id or "")
    if origin is None:
        return trace_id, emitted_at
    trace, timestamp = origin
    if timestamp is None:
        return trace, emitted_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return trace, timestamp.astimezone(UTC).isoformat()
