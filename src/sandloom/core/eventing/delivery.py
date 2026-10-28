"""Delivery primitives shared by the event bus and the consumer runtime.

``start_from`` (where a NEW consumer group starts reading) and
:class:`PoisonEventError` (a handler's "this event can never apply") are read
by the bus/transport layer when it creates a group or parks a delivery, and by
the consumer runtime (:mod:`sandloom.core.consumers.callbacks`, which re-exports
them) when it registers and wraps a handler. They live below both so the bus
never imports the runtime that runs handlers in a Unit of Work (ADR-0007).
"""

from __future__ import annotations

from datetime import UTC, datetime

#: ``start_from`` sentinels: where a NEW consumer group starts reading.
START_FROM_EARLIEST = "earliest"
START_FROM_LATEST = "latest"


def stream_start_id(start_from: str | datetime | int | None) -> str:
    """Translate a ``start_from`` value into a Streams group creation id.

    ``"earliest"`` (default) -> ``"0"`` (everything still retained);
    ``"latest"`` -> ``"$"`` (only events published after the group exists);
    an ISO-8601 timestamp / ``datetime`` / epoch-milliseconds int ->
    the cursor immediately before ``<ms>-0`` (events at or after that
    millisecond, within retention). Datetime precision is truncated to whole
    milliseconds, as in earlier releases.
    """
    if start_from is None or start_from == START_FROM_EARLIEST:
        return "0"
    if start_from == START_FROM_LATEST:
        return "$"
    if isinstance(start_from, bool):
        raise ValueError(f"invalid start_from {start_from!r}")
    if isinstance(start_from, int):
        return _inclusive_stream_cursor(start_from)
    if isinstance(start_from, datetime):
        ts = start_from
    elif isinstance(start_from, str):
        raw = start_from.strip()
        if raw.isdigit():
            return _inclusive_stream_cursor(int(raw))
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"invalid start_from {start_from!r}: use 'earliest', 'latest', "
                "an ISO-8601 timestamp or epoch milliseconds"
            ) from exc
    else:
        raise ValueError(f"invalid start_from {start_from!r}")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return _inclusive_stream_cursor(int(ts.timestamp() * 1000))


def _inclusive_stream_cursor(milliseconds: int) -> str:
    if milliseconds < 0:
        raise ValueError(f"invalid start_from {milliseconds!r}")
    # XGROUP's last-delivered ID is exclusive. The previous millisecond's
    # maximum sequence includes this millisecond's first entry (sequence zero).
    # Redis forbids entry 0-0, so zero already precedes every valid entry.
    return f"{milliseconds - 1}-18446744073709551615" if milliseconds else "0"


class PoisonEventError(Exception):
    """Raised by a consumer handler for an event that can NEVER be applied
    (malformed payload): the delivery is dead-lettered at once (CRITICAL log)
    instead of being retried — the one way out for a ``dead_letter=False``
    consumer, whose transient failures are retried forever."""


__all__ = [
    "START_FROM_EARLIEST",
    "START_FROM_LATEST",
    "PoisonEventError",
    "stream_start_id",
]
