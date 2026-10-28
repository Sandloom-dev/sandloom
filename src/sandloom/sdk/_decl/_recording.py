"""What ``sandloom.sdk.testing.published`` reads: the events published with
``Event.publish()`` while a test records them.

Off (``None``) outside the testing plugin, so publishing costs one attribute
read. Events are recorded when ``publish()`` returns, i.e. when the outbox
row joined the running unit (a unit that later rolls back keeps its record).
"""

from __future__ import annotations

from typing import Any

#: The events published in the running test, or ``None`` when not recording.
active: list[Any] | None = None


def start() -> None:
    """Begin recording (the testing plugin, per test)."""
    global active
    active = []


def stop() -> None:
    """Stop recording and drop what was recorded."""
    global active
    active = None


def record(event: Any) -> None:
    """Record ``event`` if a test is recording."""
    events = active
    if events is not None:
        events.append(event)


__all__ = ["active", "record", "start", "stop"]
