"""The Redis Streams transport's per-group oldest-unacked age (B-07).

The kernel's heartbeat that exports it as a gauge is tested on a fake transport in
``src/sandloom/core/tests/test_operational_metrics.py``.
"""

from __future__ import annotations

import time
from typing import Any

from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport


class _FakeStreamsClient:
    def __init__(
        self,
        pending_min: str | None,
        after: list[Any],
        group: dict[str, Any] | None = None,
    ) -> None:
        self.pending_min = pending_min
        self.after = after
        self.group = group
        self.calls: list[tuple[str, Any]] = []

    async def xinfo_groups(self, key: str) -> list[dict[str, Any]]:
        return [] if self.group is None else [{"name": "g", **self.group}]

    async def xpending(self, key: str, group: str) -> dict[str, Any]:
        self.calls.append(("xpending", group))
        return {"pending": 1, "min": self.pending_min}

    async def xrange(self, key: str, *, min: str, count: int) -> list[Any]:
        self.calls.append(("xrange", min))
        return self.after


def _redis_transport(client: Any) -> Any:
    transport = RedisStreamsTransport.__new__(RedisStreamsTransport)
    transport._prefix = ""

    async def _client() -> Any:
        return client

    transport._client = _client  # type: ignore[method-assign]
    return transport


async def test_streams_oldest_unacked_age_from_entry_ids() -> None:
    now_ms = int(time.time() * 1000)
    pending = _FakeStreamsClient(
        f"{now_ms - 42_000}-0", [], {"pending": 3, "lag": 5, "last-delivered-id": "1-0"}
    )
    transport = _redis_transport(pending)
    age = await transport.oldest_unacked_age_seconds("s.v1", "g")
    assert 41 <= age < 60
    assert pending.calls == [("xpending", "g")]  # one command

    lagging = _FakeStreamsClient(
        None,
        [(f"{now_ms - 7_000}-1", {})],
        {"pending": 0, "lag": 2, "last-delivered-id": "5-0"},
    )
    transport = _redis_transport(lagging)
    age = await transport.oldest_unacked_age_seconds("s.v1", "g")
    assert 6 <= age < 30
    assert lagging.calls == [("xrange", "(5-0")]  # exclusive start

    idle = _FakeStreamsClient(
        None, [], {"pending": 0, "lag": 0, "last-delivered-id": "5-0"}
    )
    transport = _redis_transport(idle)
    assert await transport.oldest_unacked_age_seconds("s.v1", "g") == 0.0
    assert idle.calls == []  # nothing pending, nothing lagging: no command
