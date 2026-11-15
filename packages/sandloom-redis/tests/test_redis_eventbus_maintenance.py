"""Redis Streams transport maintenance and dead-letter ordering, on fake clients.

* reclaiming stale pending entries advances the server-side scan cursor, and a
  failed ``XAUTOCLAIM`` / ``XTRIM`` is counted, never raised into the consumer;
* a poison entry is acked only after its dead-letter append succeeded (the append
  failing leaves it pending for the next reclaim).

The memory transport's at-least-once delivery and the consumer inbox stay in
``src/sandloom/core/tests/test_transport.py``.
"""

from __future__ import annotations

import pytest
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport


@pytest.mark.asyncio
async def test_reclaim_advances_server_scan_cursor() -> None:
    starts = []

    class Client:
        async def xautoclaim(self, *args, **kwargs):
            starts.append(kwargs["start_id"])
            return ["99-0" if len(starts) == 1 else "0-0", [], []]

    transport = RedisStreamsTransport(reclaim_interval_seconds=0)
    client = Client()
    for _ in range(3):
        await transport._reclaim_stale(client, "s", "g", "c", 1)
    assert starts == ["0-0", "99-0", "0-0"]


def _maintenance_failures(stream: str, operation: str) -> float:
    from sandloom.core.observability import metrics

    return float(
        metrics.streams_maintenance_failures_total.labels(
            stream=stream, operation=operation
        )._value.get()
    )


@pytest.mark.asyncio
async def test_failed_reclaim_is_counted() -> None:
    class Client:
        async def xautoclaim(self, *args, **kwargs):
            raise ConnectionError("redis down")

    transport = RedisStreamsTransport(reclaim_interval_seconds=0)
    before = _maintenance_failures("s-reclaim", "xautoclaim")
    await transport._reclaim_stale(Client(), "s-reclaim", "g", "c", 1)
    assert _maintenance_failures("s-reclaim", "xautoclaim") == before + 1


@pytest.mark.asyncio
async def test_failed_age_trim_is_counted() -> None:
    class Client:
        async def xtrim(self, *args, **kwargs):
            raise ConnectionError("redis down")

    transport = RedisStreamsTransport()
    transport._retention_seconds = 60
    transport._trim_interval = 0
    key = transport.key("s-trim")
    before = _maintenance_failures(key, "xtrim")
    await transport._maybe_trim_by_age(Client(), key)
    assert _maintenance_failures(key, "xtrim") == before + 1


@pytest.mark.asyncio
async def test_dlq_append_failure_leaves_source_message_unacked() -> None:
    """A poison entry must NOT be acked if its DLQ append failed.

    Acking it then would lose it forever: gone from the source pending list and
    never written to the DLQ. Leaving it un-acked lets XAUTOCLAIM reclaim and
    retry it once the broker recovers.
    """
    acked: list[tuple[str, str, str]] = []

    class _FailingDlqClient:
        async def xadd(self, *_a: object, **_k: object) -> str:
            raise RuntimeError("dlq down")

        async def xack(self, stream: str, group: str, msg_id: str) -> int:
            acked.append((stream, group, msg_id))
            return 1

    t = RedisStreamsTransport(client_factory=None)
    # _to_dlq takes the client directly, so no factory I/O is needed.
    await t._to_dlq(_FailingDlqClient(), "s", "g", "1-0", raw="{}", attempts=4)
    assert acked == []  # the source entry was NOT acked


@pytest.mark.asyncio
async def test_dlq_acks_source_only_after_successful_append() -> None:
    """On a successful DLQ append the source entry IS acked exactly once."""
    added: list[str] = []
    acked: list[str] = []

    class _OkClient:
        async def xadd(self, dlq: str, _fields: object) -> str:
            added.append(dlq)
            return "1-0"

        async def xack(self, _stream: str, _group: str, msg_id: str) -> int:
            acked.append(msg_id)
            return 1

    t = RedisStreamsTransport(client_factory=None)
    await t._to_dlq(_OkClient(), "s", "g", "7-0", raw="{}", attempts=4)
    assert added == ["s.dlq"]
    assert acked == ["7-0"]
