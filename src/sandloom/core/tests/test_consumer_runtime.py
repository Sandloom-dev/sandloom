"""Behavioural tests for supervised event-consumer lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core.app_factory import _install_ops_endpoints
from sandloom.core.consumers.callbacks import EventConsumerDescriptor
from sandloom.core.consumers.runtime import (
    ConsumerRuntime,
    ConsumerRuntimeSnapshot,
    ConsumerStartupError,
    ConsumerStatus,
)
from sandloom.core.eventing.event_bus import get_event_bus, set_active_event_bus
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import RedisEventBus
from sandloom.core.eventing.subscriptions import SubscriptionHandle, SubscriptionState
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.active import get_active_infra, set_active_infra
from sandloom.core.projections import (
    ProjectionConsumerDescriptor,
    QueryProjectionDescriptor,
)
from sandloom.core.runtime import get_active_core, set_active_core
from sandloom.core.tests._schemas import PermissiveSchemaRegistry
from sandloom.main import create_app


async def _noop(_session: Any, _envelope: Any) -> None:
    return None


@asynccontextmanager
async def _session_opener(_name: str) -> AsyncGenerator[Any, None]:
    yield object()


@dataclass
class _ReadyHandle:
    state: str = "running"

    async def wait_ready(self, _timeout: float | None = None) -> None:
        return None

    async def wait_terminated(self) -> None:
        await asyncio.Future()

    async def close(self) -> None:
        self.state = "stopped"


class _PartiallyFailingBus:
    def __init__(self) -> None:
        self.attempted: list[str] = []

    async def subscribe(
        self,
        channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _ReadyHandle:
        self.attempted.append(channel)
        if channel == "demo.first.created.v1":
            raise RuntimeError("broker rejected subscription")
        return _ReadyHandle()


class _TransientlyFailingBus:
    def __init__(self) -> None:
        self.attempts = 0

    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _ReadyHandle:
        self.attempts += 1
        if self.attempts == 1:
            raise ConnectionError("broker warming up")
        return _ReadyHandle()


class _ControllableBus:
    def __init__(self) -> None:
        self.handle = _ReadyHandle()

    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _ReadyHandle:
        return self.handle


class _TerminableHandle(_ReadyHandle):
    def __init__(self, *, close_error: bool = False) -> None:
        super().__init__()
        self._terminated = asyncio.Event()
        self._close_error = close_error

    async def wait_terminated(self) -> None:
        await self._terminated.wait()

    async def close(self) -> None:
        await super().close()
        if self._close_error:
            raise RuntimeError("adapter close failed")

    def fail(self) -> None:
        self.state = "failed"
        self._terminated.set()


class _RestartingBus:
    def __init__(self, *, first_close_fails: bool = False) -> None:
        self.handles: list[_TerminableHandle] = []
        self.resubscribed = asyncio.Event()
        self._first_close_fails = first_close_fails

    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _TerminableHandle:
        handle = _TerminableHandle(
            close_error=self._first_close_fails and not self.handles
        )
        self.handles.append(handle)
        if len(self.handles) > 1:
            self.resubscribed.set()
        return handle


class _ContractViolatingBus:
    def __init__(self) -> None:
        self.attempts = 0

    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> None:
        self.attempts += 1
        return None


class _NeverReadyHandle(_ReadyHandle):
    def __init__(self) -> None:
        super().__init__(state="starting")

    async def wait_ready(self, _timeout: float | None = None) -> None:
        await asyncio.Event().wait()


class _NeverReadyBus:
    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _NeverReadyHandle:
        return _NeverReadyHandle()


class _FailingReadyAndCloseHandle(_ReadyHandle):
    async def wait_ready(self, _timeout: float | None = None) -> None:
        raise ConnectionError("broker warming up")

    async def close(self) -> None:
        self.state = "stopped"
        raise RuntimeError("adapter close failed")


class _CloseFailingStartupBus:
    def __init__(self) -> None:
        self.attempts = 0

    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _ReadyHandle:
        self.attempts += 1
        if self.attempts == 1:
            return _FailingReadyAndCloseHandle()
        return _ReadyHandle()


class _PartiallyHangingBus:
    def __init__(self) -> None:
        self.attempted: list[str] = []

    async def subscribe(
        self,
        channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _ReadyHandle:
        self.attempted.append(channel)
        if channel == "demo.hanging.created.v1":
            await asyncio.Event().wait()
        return _ReadyHandle()


class _HangingCloseHandle(_ReadyHandle):
    async def close(self) -> None:
        await asyncio.Event().wait()


class _HangingCloseBus:
    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _HangingCloseHandle:
        return _HangingCloseHandle()


class _BlockingReadyHandle(_ReadyHandle):
    def __init__(self) -> None:
        super().__init__(state="starting")
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()

    async def wait_ready(self, _timeout: float | None = None) -> None:
        self.waiting.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.state = "stopped"
        self.closed.set()


class _BlockingReadyBus:
    def __init__(self) -> None:
        self.handle = _BlockingReadyHandle()

    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _BlockingReadyHandle:
        return self.handle


class _CancellationResistantHandle(_ReadyHandle):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_terminated(self) -> None:
        self.waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.release.wait()


class _CancellationResistantBus:
    def __init__(self) -> None:
        self.handle = _CancellationResistantHandle()

    async def subscribe(
        self,
        _channel: str,
        _callback: object,
        *,
        group: str | None = None,
    ) -> _CancellationResistantHandle:
        return self.handle


@pytest.mark.asyncio
async def test_optional_consumer_never_delays_application_startup() -> None:
    core = CoreFacade(app=FastAPI())
    core.set_event_bus(_NeverReadyBus())
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="optional-slow",
            channel="demo.optional.created.v1",
            handler=_noop,
            durable=False,
            required=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=10,
        retry_delay=0,
    )

    snapshot = await asyncio.wait_for(runtime.start(), timeout=0.05)

    assert snapshot.ready is True
    await runtime.stop()


@pytest.mark.asyncio
async def test_permanent_subscription_contract_error_is_not_retried_forever() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _ContractViolatingBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="legacy",
            channel="demo.legacy.created.v1",
            handler=_noop,
            durable=False,
            required=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0,
        retry_delay=0,
    )

    await runtime.start()
    await asyncio.sleep(0.05)

    assert bus.attempts == 1
    assert runtime.snapshot().consumers[0].state == "failed"
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_restarts_a_subscription_whose_task_dies() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _RestartingBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="restartable",
            channel="demo.restartable.created.v1",
            handler=_noop,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0.1,
        retry_delay=0,
    )
    await runtime.start()

    bus.handles[0].fail()
    await asyncio.wait_for(bus.resubscribed.wait(), timeout=1)

    assert len(bus.handles) == 2
    assert runtime.snapshot().ready is True
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_restarts_even_if_dead_subscription_close_fails() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _RestartingBus(first_close_fails=True)
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="restartable",
            channel="demo.restartable.created.v1",
            handler=_noop,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0.1,
        retry_delay=0,
    )
    await runtime.start()

    bus.handles[0].fail()
    await asyncio.wait_for(bus.resubscribed.wait(), timeout=1)

    assert len(bus.handles) == 2
    assert runtime.snapshot().ready is True
    await runtime.stop()


@pytest.mark.asyncio
async def test_required_startup_retry_survives_handle_close_failure() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _CloseFailingStartupBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="restartable",
            channel="demo.restartable.created.v1",
            handler=_noop,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0.1,
        retry_delay=0,
    )

    assert (await runtime.start()).ready is True
    assert bus.attempts == 2
    await runtime.stop()


@pytest.mark.asyncio
async def test_required_startup_timeout_bounds_subscribe_and_attempts_all() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _PartiallyHangingBus()
    core.set_event_bus(bus)
    for name, channel in (
        ("hanging", "demo.hanging.created.v1"),
        ("healthy", "demo.healthy.created.v1"),
    ):
        core.register_event_consumer(
            EventConsumerDescriptor(
                name=name,
                channel=channel,
                handler=_noop,
                durable=False,
            )
        )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0.02,
        retry_delay=0,
    )

    with pytest.raises(ConsumerStartupError):
        await asyncio.wait_for(runtime.start(), timeout=0.2)

    assert set(bus.attempted) == {
        "demo.hanging.created.v1",
        "demo.healthy.created.v1",
    }


@pytest.mark.asyncio
async def test_runtime_shutdown_is_bounded_when_adapter_close_hangs() -> None:
    core = CoreFacade(app=FastAPI())
    core.set_event_bus(_HangingCloseBus())
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="hanging-close",
            channel="demo.hanging-close.created.v1",
            handler=_noop,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0.02,
        retry_delay=0,
        shutdown_timeout=0.02,
    )
    await runtime.start()

    await asyncio.wait_for(runtime.stop(), timeout=0.2)

    assert runtime.snapshot().consumers[0].state == "stopped"


@pytest.mark.asyncio
async def test_shutdown_closes_optional_handle_cancelled_during_wait_ready() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _BlockingReadyBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="optional-starting",
            channel="demo.optional-starting.created.v1",
            handler=_noop,
            durable=False,
            required=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=10,
        retry_delay=0,
    )
    await runtime.start()
    await asyncio.wait_for(bus.handle.waiting.wait(), timeout=1)

    await runtime.stop()

    assert bus.handle.closed.is_set()


@pytest.mark.asyncio
async def test_shutdown_monitor_wait_uses_shutdown_budget() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _CancellationResistantBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="slow-monitor",
            channel="demo.slowmonitor.created.v1",
            handler=_noop,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=10,
        retry_delay=0,
        shutdown_timeout=0.02,
    )
    await runtime.start()
    await asyncio.wait_for(bus.handle.waiting.wait(), timeout=1)

    await asyncio.wait_for(runtime.stop(), timeout=0.2)
    bus.handle.release.set()
    await asyncio.sleep(0)

    assert runtime.snapshot().consumers[0].state == "stopped"


@pytest.mark.asyncio
async def test_ready_endpoint_rejects_unavailable_required_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FastAPI()
    core = CoreFacade(app=application)

    class _UnavailableRuntime:
        def snapshot(self) -> ConsumerRuntimeSnapshot:
            return ConsumerRuntimeSnapshot(
                ready=False,
                required_unavailable=("billing@paid",),
                consumers=(
                    ConsumerStatus(
                        name="billing@paid",
                        channel="orders.paid.v1",
                        kind="side_effect",
                        required=True,
                        state="retrying",
                    ),
                ),
            )

    application.state.consumer_runtime = _UnavailableRuntime()
    _install_ops_endpoints(application, core)
    from sandloom.core.config.settings import settings

    token = "ready-consumer-test-internal-token-0123456789"
    monkeypatch.setattr(settings, "internal_api_token", token)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready", headers={"X-Internal-Token": token})

    assert response.status_code == 503
    assert response.json()["required_consumers_unavailable"] == ["billing@paid"]


@pytest.mark.asyncio
async def test_lifespan_aborts_when_required_consumer_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = asyncio.Event()
    previous_core = get_active_core()
    previous_infra = get_active_infra()
    previous_event_bus = get_event_bus()

    class _FailingRuntime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def start(self) -> None:
            raise ConsumerStartupError(("billing@paid",))

        async def stop(self) -> None:
            stopped.set()

    async def noop_lifecycle(_core: object) -> None:
        return None

    monkeypatch.setattr("sandloom.core.app_factory.run_startup", noop_lifecycle)
    monkeypatch.setattr("sandloom.core.app_factory.run_shutdown", noop_lifecycle)
    monkeypatch.setattr(
        "sandloom.core.consumers.runtime.ConsumerRuntime",
        _FailingRuntime,
    )
    monkeypatch.setattr(
        "sandloom.core.app_factory.settings.outbox_relay_enabled", "off"
    )
    try:
        application = create_app()

        with pytest.raises(ConsumerStartupError):
            async with application.router.lifespan_context(application):
                pass
    finally:
        if previous_core is not None:
            set_active_core(previous_core)
        set_active_infra(previous_infra)
        set_active_event_bus(previous_event_bus)

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_redis_subscription_becomes_ready_only_after_broker_subscribe() -> None:
    subscribed = asyncio.Event()
    closed = asyncio.Event()

    class _PubSub:
        async def subscribe(self, *_channels: str) -> None:
            subscribed.set()

        async def psubscribe(self, *_channels: str) -> None:
            subscribed.set()

        def listen(self) -> AsyncIterator[dict[str, Any]]:
            return self

        def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
            return self

        async def __anext__(self) -> dict[str, Any]:
            await closed.wait()
            raise StopAsyncIteration

        async def close(self) -> None:
            closed.set()

    class _Client:
        def pubsub(self) -> _PubSub:
            return _PubSub()

    async def client_factory() -> _Client:
        return _Client()

    bus = RedisEventBus(
        PermissiveSchemaRegistry(),
        client_factory=client_factory,
    )

    async def handler(_envelope: object) -> None:
        return None

    handle = await bus.subscribe("demo.redis.created.v1", handler)

    await handle.wait_ready(timeout=1)
    assert subscribed.is_set()
    assert handle.state is SubscriptionState.RUNNING
    await bus.close()
    assert handle.snapshot().state is SubscriptionState.STOPPED


@pytest.mark.asyncio
async def test_redis_cancelled_dynamic_subscribe_rolls_back_local_registration() -> (
    None
):
    dynamic_started = asyncio.Event()
    release_dynamic = asyncio.Event()
    closed = asyncio.Event()

    class _PubSub:
        block_dynamic = False

        async def subscribe(self, *_channels: str) -> None:
            if self.block_dynamic:
                dynamic_started.set()
                await release_dynamic.wait()

        async def psubscribe(self, *_channels: str) -> None:
            await self.subscribe(*_channels)

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def punsubscribe(self, *_channels: str) -> None:
            return None

        def listen(self) -> AsyncIterator[dict[str, Any]]:
            return self

        def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
            return self

        async def __anext__(self) -> dict[str, Any]:
            await closed.wait()
            raise StopAsyncIteration

        async def close(self) -> None:
            closed.set()

    pubsub = _PubSub()

    class _Client:
        def pubsub(self) -> _PubSub:
            return pubsub

    async def client_factory() -> _Client:
        return _Client()

    async def handler(_envelope: object) -> None:
        return None

    channel = "demo.rediscancelled.created.v1"
    bus = RedisEventBus(
        PermissiveSchemaRegistry(),
        client_factory=client_factory,
    )
    first_handle = await bus.subscribe(channel, handler)
    await first_handle.wait_ready(timeout=1)
    pubsub.block_dynamic = True

    subscribe_task = asyncio.create_task(bus.subscribe(channel, handler))
    await asyncio.wait_for(dynamic_started.wait(), timeout=1)
    subscribe_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await subscribe_task

    assert bus._subs[channel] == [handler]
    assert bus._subscription_handles[channel] == [first_handle]
    await bus.close()


@pytest.mark.asyncio
async def test_required_runtime_readiness_tracks_live_subscription_state() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _ControllableBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="live",
            channel="demo.live.created.v1",
            handler=_noop,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0,
        retry_delay=0,
    )
    await runtime.start()

    bus.handle.state = "retrying"
    assert runtime.snapshot().ready is False
    bus.handle.state = "running"
    assert runtime.snapshot().ready is True

    await runtime.stop()


@pytest.mark.asyncio
async def test_in_memory_subscription_is_active_until_runtime_stops() -> None:
    registry = PermissiveSchemaRegistry()
    core = CoreFacade(app=FastAPI())
    bus = InMemoryEventBus(registry)
    core.set_event_bus(bus)
    seen: list[int] = []

    async def handler(_session: Any, envelope: Any) -> None:
        seen.append(envelope["payload"]["value"])

    core.register_event_consumer(
        EventConsumerDescriptor(
            name="memory",
            channel="demo.memory.created.v1",
            handler=handler,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0,
        retry_delay=0,
    )

    assert (await runtime.start()).ready is True
    await bus.publish_event("demo.memory.created.v1", {"value": 1})
    await runtime.stop()
    await bus.publish_event("demo.memory.created.v1", {"value": 2})

    assert seen == [1]


@pytest.mark.asyncio
async def test_bus_shutdown_terminates_in_memory_subscription_handle() -> None:
    bus = InMemoryEventBus(PermissiveSchemaRegistry())

    async def handler(_envelope: object) -> None:
        return None

    handle = await bus.subscribe("demo.memory.created.v1", handler)
    await bus.close()

    assert handle.state is SubscriptionState.STOPPED


@pytest.mark.asyncio
async def test_subscription_handle_is_terminal_even_if_adapter_close_fails() -> None:
    async def failing_close() -> None:
        raise RuntimeError("adapter close failed")

    handle = SubscriptionHandle(failing_close)
    handle.mark_running()

    with pytest.raises(RuntimeError, match="adapter close failed"):
        await handle.close()

    assert handle.state is SubscriptionState.STOPPED


@pytest.mark.asyncio
async def test_projection_consumers_share_the_supervised_runtime() -> None:
    registry = PermissiveSchemaRegistry()
    core = CoreFacade(app=FastAPI())
    bus = InMemoryEventBus(registry)
    core.set_event_bus(bus)
    seen: list[str] = []

    async def handler(_session: Any, envelope: Any) -> None:
        seen.append(envelope["payload"]["name"])

    core.register_query_projection(
        QueryProjectionDescriptor(name="search", owner_module="read_model")
    )
    core.register_projection_consumer(
        ProjectionConsumerDescriptor(
            name="search@created",
            source_channel="demo.search.created.v1",
            projection="search",
            handler=handler,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0,
        retry_delay=0,
    )

    snapshot = await runtime.start()
    await bus.publish_event("demo.search.created.v1", {"name": "Ada"})
    await bus.drain()

    assert [(item.name, item.kind) for item in snapshot.consumers] == [
        ("search@created", "projection")
    ]
    assert seen == ["Ada"]
    await runtime.stop()


@pytest.mark.asyncio
async def test_required_consumer_retries_transient_startup_failure() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _TransientlyFailingBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="required",
            channel="demo.required.created.v1",
            handler=_noop,
            durable=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0.1,
        retry_delay=0,
    )

    snapshot = await runtime.start()

    assert snapshot.ready is True
    assert bus.attempts == 2
    await runtime.stop()


@pytest.mark.asyncio
async def test_optional_failure_does_not_block_readiness() -> None:
    core = CoreFacade(app=FastAPI())
    bus = _PartiallyFailingBus()
    core.set_event_bus(bus)
    core.register_event_consumer(
        EventConsumerDescriptor(
            name="optional",
            channel="demo.first.created.v1",
            handler=_noop,
            durable=False,
            required=False,
        )
    )
    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0,
        retry_delay=0,
    )

    snapshot = await runtime.start()

    assert snapshot.ready is True
    await runtime.stop()


@pytest.mark.asyncio
async def test_required_failure_blocks_startup_but_all_consumers_are_attempted() -> (
    None
):
    core = CoreFacade(app=FastAPI())
    bus = _PartiallyFailingBus()
    core.set_event_bus(bus)
    for name, channel in (
        ("first", "demo.first.created.v1"),
        ("second", "demo.second.created.v1"),
    ):
        core.register_event_consumer(
            EventConsumerDescriptor(
                name=name,
                channel=channel,
                handler=_noop,
                durable=False,
            )
        )

    runtime = ConsumerRuntime(
        core,
        session_opener=_session_opener,
        startup_timeout=0,
        retry_delay=0,
    )

    with pytest.raises(ConsumerStartupError):
        await runtime.start()

    assert bus.attempted == [
        "demo.first.created.v1",
        "demo.second.created.v1",
    ]
    assert runtime.snapshot().ready is False
