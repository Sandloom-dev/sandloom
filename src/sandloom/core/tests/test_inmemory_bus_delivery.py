"""InMemoryEventBus delivery mirrors the durable transport (M4).

Consumers run in their own task with a clean context (no inherited request
tenant), failures are retried then dead-lettered (never swallowed), and the
publisher's timeouts cannot cancel a long handler.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.tenant_context import bind_tenant, current_tenant_id_optional
from sandloom.core.tests._schemas import PermissiveSchemaRegistry


def _bus(**kwargs: Any) -> InMemoryEventBus:
    return InMemoryEventBus(PermissiveSchemaRegistry(), **kwargs)


@pytest.mark.asyncio
async def test_consumer_does_not_inherit_publisher_tenant() -> None:
    bus = _bus()
    seen: list[Any] = []

    async def consumer(_env: Any) -> None:
        seen.append(current_tenant_id_optional())

    await bus.subscribe("core.test.tenant.v1", consumer)
    with bind_tenant(uuid.uuid4()):
        await bus.publish_event("core.test.tenant.v1", {})
    await bus.drain()
    assert seen == [None]  # same as a consumer in another process


@pytest.mark.asyncio
async def test_failing_consumer_is_retried_until_success() -> None:
    bus = _bus(retry_base_seconds=0.001)
    calls = 0

    async def flaky(_env: Any) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("transient")

    await bus.subscribe("core.test.flaky.v1", flaky)
    await bus.publish_event("core.test.flaky.v1", {})
    await bus.drain()
    assert calls == 3
    assert bus.dead_letters == []


@pytest.mark.asyncio
async def test_poison_consumer_is_dead_lettered_not_swallowed() -> None:
    bus = _bus(max_deliveries=3, retry_base_seconds=0.001)
    calls = 0

    async def poison(_env: Any) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("always")

    await bus.subscribe("core.test.poison.v1", poison)
    await bus.publish_event("core.test.poison.v1", {"k": 1})
    await bus.drain()
    assert calls == 3
    assert [(ch, env["payload"], n) for ch, env, n in bus.dead_letters] == [
        ("core.test.poison.v1", {"k": 1}, 3)
    ]


@pytest.mark.asyncio
async def test_publisher_timeout_does_not_cancel_long_handler() -> None:
    bus = _bus()
    finished = asyncio.Event()

    async def slow(_env: Any) -> None:
        await asyncio.sleep(0.1)
        finished.set()

    await bus.subscribe("core.test.slow.v1", slow)
    # The post-commit dispatcher bounds publish with a short budget; publish
    # must return promptly and the handler must still run to completion.
    await asyncio.wait_for(bus.publish_event("core.test.slow.v1", {}), timeout=0.05)
    assert not finished.is_set()
    await bus.drain()
    assert finished.is_set()
