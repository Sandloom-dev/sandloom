"""Hot-path stand: a durable event consumer (DIAGNOSTIC ONLY).

Registered with ``BENCH_HOTPATH_CONSUMER=1``: ``hotpath@item.created`` consumes
``bench.item.created.v1`` exactly once (``durable=True``: the event id is
claimed in the consumer inbox inside the delivery's Unit of Work) and resolves
the waiter the ``ladder/hot_paths.py`` rung registered for the payload id, so
one rung op = publish (outbox + post-commit XADD) + one full delivery (XREADGROUP,
decode, UoW + inbox claim + COMMIT, XACK).
"""

from __future__ import annotations

import asyncio
from typing import Any

from benchsuite.common import EVENT_CHANNEL

CONSUMER = "hotpath@item.created"
#: payload id -> the rung's waiter.
WAITERS: dict[int, asyncio.Future[None]] = {}


async def on_item_created(session: Any, envelope: Any) -> None:
    waiter = WAITERS.pop(int(envelope["payload"]["id"]), None)
    if waiter is not None and not waiter.done():
        waiter.set_result(None)


def register(core: Any) -> None:
    core.register_event_consumer(
        EVENT_CHANNEL, on_item_created, name=CONSUMER, durable=True
    )
