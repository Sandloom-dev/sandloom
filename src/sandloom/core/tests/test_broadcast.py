"""The ``broadcast`` capability: the in-memory driver, and the kit against one
broken driver per obligation (spec section 6, 2.7).

The Redis driver runs the same kit in ``test_broadcast_redis.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

import pytest

from sandloom.core.drivers.memory_broadcast import (
    MemoryBroadcastProvider,
    _MemorySubscription,
)
from sandloom.core.drivers.memory_broadcast import _Channels as Channels
from sandloom.core.drivers.memory_broadcast import _check_publish as check_publish
from sandloom.core.drivers.memory_broadcast import _check_subscribe as check_subscribe
from sandloom.core.infra.errors import (
    InfraError,
    InvalidArgument,
    TooLarge,
    Unavailable,
    Unsupported,
)
from sandloom.core.infra.ports.broadcast import (
    BROADCAST_CAPABILITY,
    MIN_PAYLOAD_LIMIT_BYTES,
    BroadcastMessage,
    BroadcastProvider,
    Subscription,
)
from sandloom.driverkit.testing import BroadcastConformance


def _memory(**options: Any) -> MemoryBroadcastProvider:
    return MemoryBroadcastProvider(name="default", driver="memory", options=options)


# ----------------------------------------------------------- the shipped driver
class _MemoryKit(BroadcastConformance):
    delivery_seconds = 2.0
    quiet_seconds = 0.1
    payload_limit_bytes = 8 * 1024 * 1024

    @pytest.fixture
    def make_provider(self) -> Callable[[], BroadcastProvider]:
        namespace = uuid.uuid4().hex
        return lambda: _memory(namespace=namespace)


class TestTheMemoryDriver(_MemoryKit):
    # Inherited checks that cannot apply to an in-process driver are not
    # collected (a skip would count against the capped skip ledger): it
    # declares no ``shared``, has no connection to interrupt and no backend
    # that fails.
    test_a_second_provider_from_the_same_options_receives = None  # type: ignore[assignment]
    test_an_outage_raises_unavailable_and_a_resubscribe_works = None  # type: ignore[assignment]
    test_harness_redacts_the_health_of_a_failing_backend = None  # type: ignore[assignment]
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]


async def test_the_spec_names_the_two_features() -> None:
    assert BROADCAST_CAPABILITY.features == {"pattern_subscribe", "shared"}
    assert MemoryBroadcastProvider.supported_features == {"pattern_subscribe"}
    assert MemoryBroadcastProvider.development_only is True


async def test_a_subscriber_that_falls_behind_its_buffer_gets_unavailable() -> None:
    provider = _memory(namespace=uuid.uuid4().hex, buffer_messages=5)
    subscription = await provider.subscribe(["c"])
    for index in range(10):
        await provider.publish("c", str(index).encode())
    got = [(await subscription.__aiter__().__anext__()).payload for _ in range(5)]
    assert got == [b"0", b"1", b"2", b"3", b"4"]  # what it held, in order
    with pytest.raises(Unavailable):
        await subscription.__aiter__().__anext__()
    with pytest.raises(Unavailable):  # and it stays ended
        await subscription.__aiter__().__anext__()
    await provider.aclose()


async def test_a_pattern_channel_is_not_published_to() -> None:
    provider = _memory(namespace=uuid.uuid4().hex)
    with pytest.raises(InvalidArgument):
        await provider.publish("a.*", b"x")
    with pytest.raises(InvalidArgument):
        await provider.publish("a", "not bytes")  # type: ignore[arg-type]
    await provider.aclose()


async def test_a_payload_over_the_option_is_too_large() -> None:
    provider = _memory(namespace=uuid.uuid4().hex, max_payload_bytes=2 * 1024 * 1024)
    await provider.publish("a", b"x" * (2 * 1024 * 1024))
    with pytest.raises(TooLarge):
        await provider.publish("a", b"x" * (2 * 1024 * 1024 + 1))
    await provider.aclose()


async def test_the_limit_option_cannot_go_under_the_floor() -> None:
    with pytest.raises(ValueError):
        _memory(max_payload_bytes=MIN_PAYLOAD_LIMIT_BYTES - 1)
    with pytest.raises(ValueError):
        _memory(typo=1)


async def test_namespaces_keep_providers_apart() -> None:
    first, second = _memory(namespace="one"), _memory(namespace="two")
    subscription = await second.subscribe(["c"])
    await first.publish("c", b"x")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.__aiter__().__anext__(), timeout=0.1)
    assert await first.isolation_identity() != await second.isolation_identity()
    await first.aclose()
    await second.aclose()


async def test_a_closed_provider_refuses_operations_and_ends_its_subscriptions() -> (
    None
):
    provider = _memory(namespace=uuid.uuid4().hex)
    subscription = await provider.subscribe(["c"])
    await provider.aclose()
    with pytest.raises(StopAsyncIteration):
        await subscription.__aiter__().__anext__()
    with pytest.raises(Unavailable):
        await provider.publish("c", b"x")
    with pytest.raises(Unavailable):
        await provider.subscribe(["c"])


async def test_publishing_from_another_thread_reaches_the_subscriber() -> None:
    provider = _memory(namespace=uuid.uuid4().hex)
    subscription = await provider.subscribe(["c"])

    def publish_elsewhere() -> None:
        asyncio.run(provider.publish("c", b"from a thread"))

    await asyncio.to_thread(publish_elsewhere)
    message = await asyncio.wait_for(subscription.__aiter__().__anext__(), timeout=2)
    assert message == BroadcastMessage("c", b"from a thread")
    await provider.aclose()


# ------------------------------------------------ the kit against broken drivers
class _Hooked(MemoryBroadcastProvider):
    """The shipped driver with one place to break: ``subscription_class``,
    ``parse`` (the channels of a subscribe), ``check_limit``."""

    subscription_class: ClassVar[type[_MemorySubscription]] = _MemorySubscription
    outage: ClassVar[str] = "unavailable"

    def parse(self, channels: Sequence[str]) -> Channels:
        return check_subscribe(self.name, channels)

    async def subscribe(self, channels: Sequence[str]) -> Subscription:
        subscription = self.subscription_class(
            self._hub, self.parse(channels), self._options.buffer_messages
        )
        self._subscriptions.add(subscription)
        return subscription

    async def _interrupt(self) -> None:
        """What an outage does to the subscriptions: the shipped behaviour is
        the sticky ``Unavailable`` of a lost connection."""
        for subscription in tuple(self._subscriptions):
            subscription._overflowed = True
            subscription._hub.subscriptions.discard(subscription)
            subscription._wake()


class _OutageKit(_MemoryKit):
    """The memory kit plus an outage the test driver can simulate."""

    async def interrupt_connections(self, provider: BroadcastProvider) -> None:
        await provider._interrupt()  # type: ignore[attr-defined]


class NoFanOut(_Hooked):
    async def publish(self, channel: str, payload: bytes) -> None:
        channel, data = check_publish(
            self.name, channel, payload, self._options.max_payload_bytes
        )
        message = BroadcastMessage(channel, data)
        for subscription in tuple(self._hub.subscriptions):
            if subscription.channels.matches(channel):
                subscription.deliver(message)
                return  # only the first subscriber


class Replays(_Hooked):
    _history: ClassVar[dict[str, list[bytes]]] = {}

    async def publish(self, channel: str, payload: bytes) -> None:
        self._history.setdefault(channel, []).append(payload)
        await super().publish(channel, payload)

    async def subscribe(self, channels: Sequence[str]) -> Subscription:
        subscription = await super().subscribe(channels)
        for channel in channels:
            for payload in self._history.get(channel, ()):
                subscription.deliver(BroadcastMessage(channel, payload))  # type: ignore[attr-defined]
        return subscription


class Reordering(_MemorySubscription):
    def _append(self, message: BroadcastMessage) -> None:
        super()._append(message)
        if len(self._buffer) > 1:
            self._buffer.rotate(1)  # the newest first


class ReordersMessages(_Hooked):
    subscription_class = Reordering


class Mangles(_Hooked):
    async def publish(self, channel: str, payload: bytes) -> None:
        await super().publish(channel, payload.decode("utf-8", "ignore").encode())


class LosesOnCancel(_MemorySubscription):
    _swallow = False

    async def __anext__(self) -> BroadcastMessage:
        try:
            return await super().__anext__()
        except asyncio.CancelledError:
            self._swallow = True  # the reader that gave up took a message
            raise

    def _append(self, message: BroadcastMessage) -> None:
        if self._swallow:
            self._swallow = False
            return
        super()._append(message)


class DropsWhenCancelled(_Hooked):
    subscription_class = LosesOnCancel


class SilentlyDrops(_MemorySubscription):
    def _append(self, message: BroadcastMessage) -> None:
        if len(self._buffer) >= 1000:
            return  # a full buffer skips and carries on
        super()._append(message)


class DropsOnOverflow(_Hooked):
    subscription_class = SilentlyDrops


class NoLimit(_Hooked):
    async def publish(self, channel: str, payload: bytes) -> None:
        channel, data = check_publish(self.name, channel, payload, 2**40)
        self._hub.publish(BroadcastMessage(channel, data))


class DoesNotClose(_MemorySubscription):
    async def aclose(self) -> None:
        return None


class KeepsListening(_Hooked):
    subscription_class = DoesNotClose


class MatchesEverything(_MemorySubscription):
    def __init__(self, hub: Any, channels: Channels, capacity: int) -> None:
        super().__init__(hub, channels, capacity)

        class _Every:
            exact = channels.exact
            prefixes = channels.prefixes

            @staticmethod
            def matches(channel: str) -> bool:
                return channel in channels.exact or bool(channels.prefixes)

        self.channels = _Every()  # type: ignore[assignment]


class PatternMatchesAll(_Hooked):
    subscription_class = MatchesEverything


class Twice(_MemorySubscription):
    def deliver(self, message: BroadcastMessage) -> None:
        for _ in range(self._copies(message.channel)):
            super().deliver(message)

    def _copies(self, channel: str) -> int:
        return self.channels.exact.count(channel) + sum(
            channel.startswith(prefix) for prefix in self.channels.prefixes
        )


class DeliversOncePerChannel(_Hooked):
    subscription_class = Twice

    def parse(self, channels: Sequence[str]) -> Channels:
        kept = check_subscribe(self.name, channels)
        return Channels(
            tuple(c for c in channels if not c.endswith(".*")), kept.prefixes
        )


class EmptyChannel(_Hooked):
    async def publish(self, channel: str, payload: bytes) -> None:
        if channel == "":
            return
        await super().publish(channel, payload)

    def parse(self, channels: Sequence[str]) -> Channels:
        if list(channels) in ([""], []):
            return Channels((), ())
        return super().parse(channels)


class OwnHub(_Hooked):
    supported_features = frozenset({"pattern_subscribe", "shared"})

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._hub = type(self._hub)()  # not shared with another provider


class AcceptsPatternUndeclared(_Hooked):
    supported_features = frozenset()


class RefusesDeclaredPattern(_Hooked):
    def parse(self, channels: Sequence[str]) -> Channels:
        if any(channel.endswith(".*") for channel in channels):
            raise Unsupported(
                "pattern_subscribe", capability="broadcast", instance=self.name
            )
        return check_subscribe(self.name, channels)


class SilentReconnect(_Hooked):
    async def _interrupt(self) -> None:
        for subscription in tuple(self._subscriptions):
            subscription._buffer.clear()  # the gap, and no error


class RaisesOnce(_MemorySubscription):
    _raised = False

    async def __anext__(self) -> BroadcastMessage:
        if self._overflowed and not self._raised:
            self._raised = True
            raise Unavailable("lost", capability="broadcast")
        if self._overflowed:
            self._overflowed = False  # carries on behind the open iterator
        return await super().__anext__()


class NotSticky(_Hooked):
    subscription_class = RaisesOnce


class StaysDown(_Hooked):
    async def _interrupt(self) -> None:
        await super()._interrupt()
        self._closed = True  # never serves again


def _kit(driver: type[_Hooked], **attrs: Any) -> tuple[type[Any], type[_Hooked]]:
    return (type("_Kit", (_OutageKit,), attrs), driver)


async def _failures(kit: type[Any], driver: type[_Hooked]) -> set[str]:
    """Run every test of ``kit`` against ``driver`` as pytest would (a fresh
    opened provider per test); answer the names of the tests that failed."""
    instance = kit()
    failed: set[str] = set()
    for name in sorted(n for n in dir(kit) if n.startswith("test_")):
        method = getattr(instance, name)
        parameters = inspect.signature(method).parameters
        namespace = uuid.uuid4().hex

        def make(driver: type[_Hooked] = driver, ns: str = namespace) -> _Hooked:
            return driver(name="default", driver="memory", options={"namespace": ns})

        arguments: dict[str, Any] = {}
        opened = None
        if "provider" in parameters:
            opened = make()
            await opened.aopen()
            arguments["provider"] = opened
        if "make_provider" in parameters:
            arguments["make_provider"] = make
        if "make_failing_provider" in parameters:
            arguments["make_failing_provider"] = None
        try:
            await method(**arguments)
        except pytest.skip.Exception:
            pass
        except (
            AssertionError,
            InfraError,
            TimeoutError,
            StopAsyncIteration,
            pytest.fail.Exception,
        ):
            failed.add(name)
        if opened is not None:
            await opened.aclose()
    return failed


async def test_the_kit_passes_a_driver_that_keeps_the_contract() -> None:
    kit, driver = _kit(_Hooked)
    assert await _failures(kit, driver) == set()


_KIT = "test_"
_HARNESS = "test_harness_"


@pytest.mark.parametrize(
    ("driver", "attrs", "expected"),
    [
        (
            NoFanOut,
            {},
            {"test_every_live_subscriber_receives_the_message"},
        ),
        (
            Replays,
            {},
            {"test_a_subscriber_that_joins_later_gets_nothing_published_before"},
        ),
        (
            ReordersMessages,
            {},
            {
                "test_messages_of_one_publisher_arrive_in_order",
                "test_a_subscriber_that_does_not_read_gets_everything_or_unavailable",
                "test_a_pattern_matches_every_channel_with_its_prefix",
            },
        ),
        (Mangles, {}, {"test_a_message_carries_its_bytes_unchanged"}),
        (
            DropsWhenCancelled,
            {},
            {
                "test_a_read_that_times_out_loses_nothing",
                "test_a_subscriber_that_joins_later_gets_nothing_published_before",
            },
        ),
        (
            DropsOnOverflow,
            {},
            {"test_a_subscriber_that_does_not_read_gets_everything_or_unavailable"},
        ),
        (
            NoLimit,
            {},
            {"test_a_payload_at_the_limit_is_accepted_and_one_byte_more_is_too_large"},
        ),
        (
            _Hooked,
            {"payload_limit_bytes": MIN_PAYLOAD_LIMIT_BYTES - 1},
            {
                "test_the_declared_payload_limit_is_at_least_one_mebibyte",
                "test_a_payload_at_the_limit_is_accepted_and_one_byte_more_is_too_large",
            },
        ),
        (KeepsListening, {}, {"test_aclose_ends_the_iteration"}),
        (
            PatternMatchesAll,
            {},
            {"test_a_pattern_matches_every_channel_with_its_prefix"},
        ),
        (
            DeliversOncePerChannel,
            {},
            {"test_a_message_matching_two_channels_arrives_once"},
        ),
        (
            EmptyChannel,
            {},
            {"test_a_channel_must_be_a_non_empty_string"},
        ),
        (
            OwnHub,
            {},
            {"test_a_second_provider_from_the_same_options_receives"},
        ),
        (
            AcceptsPatternUndeclared,
            {},
            {_HARNESS + "undeclared_behaviours_raise_unsupported"},
        ),
        (
            RefusesDeclaredPattern,
            {},
            {
                _HARNESS + "declared_behaviours_do_not_raise_unsupported",
                "test_a_message_matching_two_channels_arrives_once",
                "test_a_pattern_matches_every_channel_with_its_prefix",
                "test_a_pattern_and_a_name_share_one_subscription",
            },
        ),
        (
            SilentReconnect,
            {},
            {"test_an_outage_raises_unavailable_and_a_resubscribe_works"},
        ),
        (
            NotSticky,
            {},
            {"test_an_outage_raises_unavailable_and_a_resubscribe_works"},
        ),
        (
            StaysDown,
            {},
            {"test_an_outage_raises_unavailable_and_a_resubscribe_works"},
        ),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
async def test_each_kit_check_fails_exactly_its_broken_driver(
    driver: type[_Hooked], attrs: dict[str, Any], expected: set[str]
) -> None:
    kit, _ = _kit(driver, delivery_seconds=0.5, quiet_seconds=0.1, **attrs)
    assert await _failures(kit, driver) == expected


# ------------------------------------------------------- the registry's instance
def _registry(monkeypatch: pytest.MonkeyPatch, infra: dict[str, Any]) -> Any:
    from sandloom.core.config.settings import settings
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    monkeypatch.setattr(settings, "infra", infra)
    return build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)


async def test_the_default_broadcast_instance_is_redis_on_the_cache_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    registry = _registry(monkeypatch, {})
    config = registry._config["broadcast"]["default"]
    assert config.driver == "redis"
    assert config.implicit is True
    assert config.options["url"] == settings.cache_url
    provider = registry.provider("broadcast")
    assert type(provider).__name__ == "RedisBroadcastProvider"
    assert provider.features() == {"pattern_subscribe", "shared"}
    assert not registry.in_use("broadcast")  # opened only when a user needs it


async def test_infra_can_select_the_memory_driver_for_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        monkeypatch, {"broadcast": {"default": {"driver": "memory", "namespace": "t"}}}
    )
    provider = registry.provider("broadcast")
    assert isinstance(provider, MemoryBroadcastProvider)
    assert provider.features() == {"pattern_subscribe"}


# ------------------------------------------------- the volatile bus over broadcast
def _bus(provider: Any) -> Any:
    from sandloom.core.eventing.pubsub_bus import RedisEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    registry = InMemorySchemaRegistry()
    for channel in ("core.thing.v1", "core.lost.v1", "core.gone.v1"):
        registry.register(channel, channel, {"type": "object"})
    return RedisEventBus(registry, broadcast=provider)


def _envelope(channel: str, **payload: Any) -> Any:
    return {
        "id": uuid.uuid4().hex,
        "channel": channel,
        "schema_version": "v1",
        "trace_id": None,
        "emitted_at": "2026-01-01T00:00:00+00:00",
        "payload": payload,
    }


async def _until(condition: Callable[[], bool], what: str = "the condition") -> None:
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} never held")


async def test_the_bus_delivers_a_published_event_to_each_subscriber() -> None:
    provider = _memory(namespace=uuid.uuid4().hex)
    bus = _bus(provider)
    first: list[Any] = []
    second: list[Any] = []

    async def on_first(env: Any) -> None:
        first.append(env)

    async def on_second(env: Any) -> None:
        second.append(env)

    handle = await bus.subscribe("core.thing.v1", on_first)
    await bus.subscribe("core.thing.v1", on_second)
    await _until(lambda: handle.state.value == "running", "the subscription")
    await bus.publish_event("core.thing.v1", {"hello": "world"})
    await _until(lambda: len(first) == 1 and len(second) == 1, "both deliveries")
    assert first[0]["payload"] == {"hello": "world"}
    await bus.close()
    await provider.aclose()


async def test_a_channel_only_reaches_its_own_subscribers() -> None:
    provider = _memory(namespace=uuid.uuid4().hex)
    bus = _bus(provider)
    thing: list[Any] = []
    gone: list[Any] = []

    async def on_thing(env: Any) -> None:
        thing.append(env)

    async def on_gone(env: Any) -> None:
        gone.append(env)

    first = await bus.subscribe("core.thing.v1", on_thing)
    second = await bus.subscribe("core.gone.v1", on_gone)
    await _until(
        lambda: first.state.value == "running" and second.state.value == "running"
    )
    await bus.publish_event("core.thing.v1", {"n": 1})
    await _until(lambda: len(thing) == 1)
    await asyncio.sleep(0.05)
    assert len(thing) == 1 and gone == []
    await bus.close()
    await provider.aclose()


async def test_unsubscribing_the_last_callback_ends_its_subscription() -> None:
    provider = _memory(namespace=uuid.uuid4().hex)
    bus = _bus(provider)

    async def on_event(_env: Any) -> None:
        return None

    handle = await bus.subscribe("core.gone.v1", on_event)
    await _until(lambda: handle.state.value == "running")
    assert provider._hub.subscriptions
    await handle.close()
    assert not provider._hub.subscriptions
    assert bus._tasks == {}
    await bus.close()
    await provider.aclose()


async def test_the_bus_resubscribes_when_its_subscription_is_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _memory(namespace=uuid.uuid4().hex)
    bus = _bus(provider)
    got: list[Any] = []

    async def on_event(env: Any) -> None:
        got.append(env)

    handle = await bus.subscribe("core.lost.v1", on_event)
    await _until(lambda: handle.state.value == "running")
    with caplog.at_level("WARNING", logger="sandloom.core.eventing.event_bus"):
        for subscription in tuple(provider._hub.subscriptions):
            subscription._overflowed = True
            subscription._wake()
        await _until(
            lambda: any("lost the subscription" in r.message for r in caplog.records)
        )
    await _until(
        lambda: bool(provider._hub.subscriptions) and handle.state.value == "running"
    )
    await bus.publish_event("core.lost.v1", {"after": True})
    await _until(lambda: len(got) == 1, "delivery after the resubscribe")
    await bus.close()
    await provider.aclose()


async def test_a_failed_publish_reaches_the_caller() -> None:
    provider = _memory(namespace=uuid.uuid4().hex)
    bus = _bus(provider)
    await provider.aclose()  # a closed instance refuses publish
    with pytest.raises(Unavailable):
        await bus.publish_event("core.thing.v1", {"x": 1})
    await bus.close()
