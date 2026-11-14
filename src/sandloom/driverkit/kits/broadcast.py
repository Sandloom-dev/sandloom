"""``BroadcastConformance``: what a ``broadcast`` driver must do (spec section 6).

It publishes real messages on channels of its own (``kit.<random>.…``): point
``make_provider`` at a test backend.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from typing import ClassVar

import pytest

from sandloom.core.infra.errors import InvalidArgument, TooLarge, Unavailable
from sandloom.core.infra.ports.broadcast import (
    BROADCAST_CAPABILITY,
    MIN_PAYLOAD_LIMIT_BYTES,
    Broadcast,
    BroadcastMessage,
    BroadcastProvider,
    Subscription,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

#: What the non-reading subscriber test publishes (spec section 6).
BACKLOG_MESSAGES = 10_000


def _channel(name: str, token: str | None = None) -> str:
    return f"kit.{token or uuid.uuid4().hex[:12]}.{name}"


async def _publish(provider: Broadcast, channel: str, *payloads: bytes) -> None:
    for payload in payloads:
        await provider.publish(channel, payload)


async def _next(subscription: Subscription, wait: float, what: str) -> BroadcastMessage:
    """The next message, or a failure naming ``what`` was not delivered."""
    iterator = subscription.__aiter__()
    try:
        return await asyncio.wait_for(iterator.__anext__(), timeout=wait)
    except TimeoutError:
        raise AssertionError(
            f"{what}: nothing arrived within {wait}s on a live subscription"
        ) from None
    except StopAsyncIteration:
        raise AssertionError(f"{what}: the subscription ended") from None


async def _nothing(subscription: Subscription, wait: float, what: str) -> None:
    """Fail when a message arrives within ``wait``."""
    iterator = subscription.__aiter__()
    try:
        message = await asyncio.wait_for(iterator.__anext__(), timeout=wait)
    except TimeoutError:
        return
    except StopAsyncIteration:
        return
    raise AssertionError(
        f"{what}: delivered a message on {message.channel!r} that must not arrive"
    )


@pytest.mark.asyncio
class BroadcastConformance(ProviderConformance[BroadcastProvider]):
    """What a ``broadcast`` driver must do, after the harness's checks.

    A driver's test subclass sets :attr:`payload_limit_bytes` to the limit the
    driver declares, and overrides :meth:`interrupt_connections` to run the
    outage group (a stable-tier driver must).
    """

    contract = BroadcastProvider
    protocol = Broadcast
    spec = BROADCAST_CAPABILITY

    #: The payload limit the driver declares (at least 1 MiB): a payload of this
    #: size is accepted, one byte more raises ``TooLarge``.
    payload_limit_bytes: ClassVar[int] = MIN_PAYLOAD_LIMIT_BYTES
    #: How long a delivery may take on this backend.
    delivery_seconds: ClassVar[float] = 5.0
    #: How long to wait for a message that must not arrive.
    quiet_seconds: ClassVar[float] = 0.3

    async def interrupt_connections(self, provider: BroadcastProvider) -> None:
        """Cut every connection ``provider`` holds to its backend, as a network
        outage or a failover would (``CLIENT KILL`` on Redis). The provider must
        recover for a new ``subscribe`` and ``publish`` afterwards. Override it
        to run the outage group; the default skips it."""
        pytest.skip("interrupt_connections is not overridden: no outage check")

    # ------------------------------------------------------------- harness
    def feature_calls(self, provider: BroadcastProvider) -> Mapping[str, Call]:
        async def subscribe_to_a_pattern() -> object:
            subscription = await provider.subscribe([_channel("*")])
            await subscription.aclose()
            return subscription

        return {"pattern_subscribe": subscribe_to_a_pattern}

    def operation_calls(self, provider: BroadcastProvider) -> Mapping[str, Call]:
        async def subscribe() -> object:
            subscription = await provider.subscribe([_channel("x")])
            await subscription.aclose()
            return subscription

        return {
            "publish": lambda: provider.publish(_channel("x"), b"payload"),
            "subscribe": subscribe,
        }

    # ----------------------------------------------------------- core group
    async def test_a_live_subscriber_receives_a_message(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("a")
        subscription = await provider.subscribe([channel])
        try:
            await provider.publish(channel, b"hello")
            message = await _next(subscription, self.delivery_seconds, "a message")
            require(
                message == BroadcastMessage(channel, b"hello"),
                f"the message arrived as {message!r}, not as published",
            )
        finally:
            await subscription.aclose()

    async def test_every_live_subscriber_receives_the_message(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("fan")
        subscriptions = [await provider.subscribe([channel]) for _ in range(3)]
        try:
            await provider.publish(channel, b"to-all")
            for index, subscription in enumerate(subscriptions):
                message = await _next(
                    subscription, self.delivery_seconds, f"subscriber {index}"
                )
                require(
                    message.payload == b"to-all",
                    f"subscriber {index} got {message!r}",
                )
        finally:
            for subscription in subscriptions:
                await subscription.aclose()

    async def test_a_subscriber_that_joins_later_gets_nothing_published_before(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("late")
        await provider.publish(channel, b"before")
        subscription = await provider.subscribe([channel])
        try:
            await _nothing(
                subscription, self.quiet_seconds, "a subscriber that joined late"
            )
            await provider.publish(channel, b"after")
            message = await _next(subscription, self.delivery_seconds, "a new message")
            require(
                message.payload == b"after",
                f"the late subscriber got {message!r}: nothing published before "
                "it joined may reach it (no replay)",
            )
        finally:
            await subscription.aclose()

    async def test_publishing_with_no_subscriber_succeeds(
        self, provider: BroadcastProvider
    ) -> None:
        await provider.publish(_channel("nobody"), b"into the void")

    async def test_messages_of_one_publisher_arrive_in_order(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("order")
        subscription = await provider.subscribe([channel])
        try:
            payloads = [str(index).encode() for index in range(200)]
            await _publish(provider, channel, *payloads)
            received = [
                (await _next(subscription, self.delivery_seconds, "an ordered run"))
                for _ in payloads
            ]
            require(
                [m.payload for m in received] == payloads,
                "messages of one publisher on one channel arrived out of order",
            )
        finally:
            await subscription.aclose()

    async def test_one_subscription_serves_several_channels(
        self, provider: BroadcastProvider
    ) -> None:
        token = uuid.uuid4().hex[:12]
        first, second, other = (
            _channel("one", token),
            _channel("two", token),
            _channel("three", token),
        )
        subscription = await provider.subscribe([first, second])
        try:
            await provider.publish(other, b"not-subscribed")
            await provider.publish(first, b"1")
            await provider.publish(second, b"2")
            got = {
                (m.channel, m.payload)
                for m in [
                    await _next(subscription, self.delivery_seconds, "channel one"),
                    await _next(subscription, self.delivery_seconds, "channel two"),
                ]
            }
            require(
                got == {(first, b"1"), (second, b"2")},
                f"the subscription received {sorted(got)}: each message must carry "
                "its own channel, and a channel it did not subscribe to gives none",
            )
            await _nothing(subscription, self.quiet_seconds, "an unsubscribed channel")
        finally:
            await subscription.aclose()

    async def test_a_message_carries_its_bytes_unchanged(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("bytes")
        subscription = await provider.subscribe([channel])
        try:
            for payload in (b"", bytes(range(256)), "héllo ✓".encode()):
                await provider.publish(channel, payload)
                message = await _next(subscription, self.delivery_seconds, "a payload")
                require(
                    message.payload == payload,
                    f"the payload {payload[:16]!r}... arrived as "
                    f"{message.payload[:16]!r}...",
                )
        finally:
            await subscription.aclose()

    async def test_a_read_that_times_out_loses_nothing(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("cancel")
        subscription = await provider.subscribe([channel])
        try:
            iterator = subscription.__aiter__()
            for _ in range(3):
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(iterator.__anext__(), timeout=0.1)
            await provider.publish(channel, b"after-timeouts")
            message = await _next(subscription, self.delivery_seconds, "the message")
            require(
                message.payload == b"after-timeouts",
                "a read that was cancelled by a timeout lost or changed a message",
            )
        finally:
            await subscription.aclose()

    async def test_a_subscriber_that_does_not_read_gets_everything_or_unavailable(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("backlog")
        subscription = await provider.subscribe([channel])
        try:
            for index in range(BACKLOG_MESSAGES):
                await provider.publish(channel, str(index).encode())
            received: list[int] = []
            ended = False
            iterator = subscription.__aiter__()
            while len(received) < BACKLOG_MESSAGES:
                try:
                    message = await asyncio.wait_for(
                        iterator.__anext__(), timeout=self.delivery_seconds
                    )
                except Unavailable:
                    ended = True
                    break
                except TimeoutError:
                    raise AssertionError(
                        f"after {len(received)} of {BACKLOG_MESSAGES} messages the "
                        "subscription neither delivered the rest nor raised "
                        "Unavailable: a live subscription never drops silently"
                    ) from None
                received.append(int(message.payload))
            require(
                received == list(range(len(received))),
                "the subscription skipped or reordered messages and carried on "
                f"(it got {received[:3]}... of {BACKLOG_MESSAGES}): it must deliver "
                "all of them in order or raise Unavailable",
            )
            require(
                ended or len(received) == BACKLOG_MESSAGES,
                "the subscription ended without all messages and without Unavailable",
            )
        finally:
            await subscription.aclose()

    async def test_aclose_ends_the_iteration(self, provider: BroadcastProvider) -> None:
        channel = _channel("close")
        subscription = await provider.subscribe([channel])
        waiting = asyncio.ensure_future(subscription.__aiter__().__anext__())
        await asyncio.sleep(0.1)
        await subscription.aclose()
        await subscription.aclose()  # idempotent
        try:
            await asyncio.wait_for(waiting, timeout=self.delivery_seconds)
        except StopAsyncIteration:
            pass
        except TimeoutError:
            raise AssertionError(
                "a read waiting for a message did not end after aclose"
            ) from None
        else:
            raise AssertionError("a closed subscription delivered a message")
        await provider.publish(channel, b"after close")
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(
                subscription.__aiter__().__anext__(), timeout=self.delivery_seconds
            )

    async def test_a_message_matching_two_channels_arrives_once(
        self, provider: BroadcastProvider
    ) -> None:
        token = uuid.uuid4().hex[:12]
        channel = _channel("twice", token)
        channels = [channel, channel]
        if "pattern_subscribe" in provider.features():
            channels.append(f"kit.{token}.*")
        subscription = await provider.subscribe(channels)
        try:
            await provider.publish(channel, b"once")
            await _next(subscription, self.delivery_seconds, "the message")
            await _nothing(
                subscription,
                self.quiet_seconds,
                "a message that matched several channels of one subscription",
            )
        finally:
            await subscription.aclose()

    async def test_a_channel_must_be_a_non_empty_string(
        self, provider: BroadcastProvider
    ) -> None:
        with pytest.raises(InvalidArgument):
            await provider.publish("", b"x")
        with pytest.raises(InvalidArgument):
            await provider.subscribe([""])
        with pytest.raises(InvalidArgument):
            await provider.subscribe([])

    # ------------------------------------------------------- payload limit
    async def test_the_declared_payload_limit_is_at_least_one_mebibyte(
        self, provider: BroadcastProvider
    ) -> None:
        require(
            self.payload_limit_bytes >= MIN_PAYLOAD_LIMIT_BYTES,
            f"payload_limit_bytes {self.payload_limit_bytes} is under the "
            f"{MIN_PAYLOAD_LIMIT_BYTES}-byte floor every driver accepts",
        )

    async def test_a_payload_at_the_limit_is_accepted_and_one_byte_more_is_too_large(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("limit")
        subscription = await provider.subscribe([channel])
        try:
            await provider.publish(channel, b"x" * self.payload_limit_bytes)
            message = await _next(
                subscription, self.delivery_seconds * 4, "a payload at the limit"
            )
            require(
                len(message.payload) == self.payload_limit_bytes,
                "a payload at the declared limit arrived with another size",
            )
            with pytest.raises(TooLarge):
                await provider.publish(channel, b"x" * (self.payload_limit_bytes + 1))
            await _nothing(subscription, self.quiet_seconds, "an oversized payload")
        finally:
            await subscription.aclose()

    # ------------------------------------------------- group pattern_subscribe
    async def test_a_pattern_matches_every_channel_with_its_prefix(
        self, provider: BroadcastProvider
    ) -> None:
        self.requires_feature(provider, "pattern_subscribe")
        token = uuid.uuid4().hex[:12]
        subscription = await provider.subscribe([f"kit.{token}.*"])
        try:
            near, deep = f"kit.{token}.a", f"kit.{token}.a.b"
            await provider.publish(f"kit.{token}x.a", b"another prefix")
            await provider.publish(near, b"near")
            await provider.publish(deep, b"deep")
            got = [
                await _next(subscription, self.delivery_seconds, "a pattern match"),
                await _next(subscription, self.delivery_seconds, "a pattern match"),
            ]
            require(
                [(m.channel, m.payload) for m in got]
                == [(near, b"near"), (deep, b"deep")],
                f"the pattern received {got!r}: it matches every channel with its "
                "prefix and no channel with another",
            )
            await _nothing(subscription, self.quiet_seconds, "another prefix")
        finally:
            await subscription.aclose()

    async def test_a_pattern_and_a_name_share_one_subscription(
        self, provider: BroadcastProvider
    ) -> None:
        self.requires_feature(provider, "pattern_subscribe")
        token = uuid.uuid4().hex[:12]
        named = _channel("named")
        subscription = await provider.subscribe([named, f"kit.{token}.*"])
        try:
            await provider.publish(named, b"by name")
            await provider.publish(f"kit.{token}.x", b"by pattern")
            got = {
                (await _next(subscription, self.delivery_seconds, "a message")).payload
                for _ in range(2)
            }
            require(got == {b"by name", b"by pattern"}, f"received {got!r}")
        finally:
            await subscription.aclose()

    # ------------------------------------------------------- group shared
    async def test_a_second_provider_from_the_same_options_receives(
        self,
        provider: BroadcastProvider,
        make_provider: Callable[[], BroadcastProvider],
    ) -> None:
        self.requires_feature(provider, "shared")
        other = make_provider()
        await other.aopen()
        channel = _channel("shared")
        subscription = await provider.subscribe([channel])
        try:
            await other.publish(channel, b"from the other process")
            message = await _next(
                subscription, self.delivery_seconds, "a message from another provider"
            )
            require(message.payload == b"from the other process", f"got {message!r}")
        finally:
            await subscription.aclose()
            await other.aclose()

    # --------------------------------------------------------- group outage
    async def test_an_outage_raises_unavailable_and_a_resubscribe_works(
        self, provider: BroadcastProvider
    ) -> None:
        channel = _channel("outage")
        subscription = await provider.subscribe([channel])
        try:
            await self.interrupt_connections(provider)
            iterator = subscription.__aiter__()
            try:
                message = await asyncio.wait_for(
                    iterator.__anext__(), timeout=self.delivery_seconds * 2
                )
            except Unavailable:
                pass
            except TimeoutError:
                raise AssertionError(
                    "the connection was cut and the iterator neither raised "
                    "Unavailable nor delivered: it reconnected silently or hung"
                ) from None
            else:
                raise AssertionError(
                    f"the iterator delivered {message!r} after the connection was cut"
                )
            # The subscription stays ended: no silent reconnect behind it.
            with pytest.raises(Unavailable):
                await asyncio.wait_for(
                    iterator.__anext__(), timeout=self.delivery_seconds
                )
        finally:
            await subscription.aclose()
        await self._publish_after_outage(provider)

    async def _publish_after_outage(self, provider: BroadcastProvider) -> None:
        channel = _channel("recovered")
        deadline = asyncio.get_running_loop().time() + self.delivery_seconds * 2
        while True:
            try:
                fresh = await provider.subscribe([channel])
                try:
                    await provider.publish(channel, b"back")
                    message = await _next(
                        fresh, self.delivery_seconds, "a message after the outage"
                    )
                finally:
                    await fresh.aclose()
                require(message.payload == b"back", f"got {message!r}")
                return
            except Unavailable:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError(
                        "subscribe and publish still raise Unavailable after the "
                        "outage: the provider must reconnect for new operations"
                    ) from None
                await asyncio.sleep(0.2)


__all__ = ["BACKLOG_MESSAGES", "BroadcastConformance"]
