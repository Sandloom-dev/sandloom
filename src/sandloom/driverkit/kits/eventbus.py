"""``EventTransportConformance``: what an ``eventbus`` transport driver must do
(spec 7): the core groups, then one group per behaviour feature the driver
declares and, for a driver that can cut its connections, the outage group.

A driver's tests subclass the kit and override ``make_provider`` (an unopened
:class:`EventTransportProvider`; the harness checks that it opens, closes twice,
declares its features, reports health and its isolation domain, and that a
declared feature's call works and an undeclared one's raises ``Unsupported``,
before these groups). Every test uses stream names of its own: entries outlive
a test on a shared backend.

What the kit cannot learn from the provider, the driver's tests state:

* the delivery budget: build the provider with ``max_deliveries`` equal to
  :attr:`EventTransportConformance.max_deliveries` (default 3) and the shortest
  retry backoff the driver allows (a few milliseconds), and, when the driver
  reclaims a dead consumer's pending entries after an idle time, an idle time
  of about a second;
* how to make the backend misbehave (:meth:`~EventTransportConformance.refused_stream`,
  :meth:`~EventTransportConformance.failing_dlq_append`,
  :meth:`~EventTransportConformance.interrupt_connections`) and how to read what
  was dead-lettered when the driver has no ``dlq_ops``
  (:meth:`~EventTransportConformance.parked_ids`): a check that needs a hook the
  driver does not give is skipped with its reason, never passed; a driver of the
  stable tier sets :attr:`~EventTransportConformance.require_stable_hooks` and
  the same check fails instead;
* with ``lease_renewal``, the reclaim idle time of about a second (the kit
  renews at :meth:`renew_interval_seconds` for five intervals and expects the
  entry to stay with its consumer, then to be reclaimed once renewal stops);
* what ``retention()`` must answer for given options
  (:meth:`~EventTransportConformance.retention_cases`).

The checks that a delivery does *not* come back poll :meth:`consume` next to a
control entry that is left unacked: when the control returns, an entry that
should have been gone had its chance to return too.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

import pytest

from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import AlreadyExists, InfraError, Unavailable
from sandloom.core.infra.ports.eventbus import (
    EVENTBUS_CAPABILITY,
    ConsumerInfo,
    DeadLetter,
    Delivery,
    EventTransport,
    EventTransportProvider,
    GroupOwnedElsewhere,
    GroupOwner,
    PartialPublish,
    Retention,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

if TYPE_CHECKING:
    from sandloom.core.eventing.envelope import EventEnvelope

#: Pause between two polls of an empty ``consume``.
_POLL_SECONDS = 0.02
#: How many acquirers race for one group's owner marker.
_RACERS = 8
#: ``block_ms`` of a blocking consume that waits for an entry.
_BLOCK_MS = 5000
#: A message-id millisecond further than this from the local clock is not a
#: timestamp (the memory transport numbers its entries 1, 2, ...).
_CLOCK_WINDOW_MS = 86_400_000


def _stream() -> str:
    return f"kit.{uuid.uuid4().hex[:12]}.v1"


def _group() -> str:
    return f"kit-group-{uuid.uuid4().hex[:8]}"


def _envelope(event_id: str, *, channel: str = "kit.event.v1") -> EventEnvelope:
    return {
        "id": event_id,
        "channel": channel,
        "schema_version": "v1",
        "trace_id": None,
        "emitted_at": "2026-01-01T00:00:00+00:00",
        "payload": {"event": event_id},
    }


def _owner(
    module: str = "kit-a", database: str = "db:kit-one", service: str | None = None
) -> GroupOwner:
    return GroupOwner(module=module, database=database, service=service)


def _ids(deliveries: Sequence[Delivery]) -> list[str]:
    """The domain event ids of ``deliveries``, in order."""
    return [delivery.envelope["id"] for delivery in deliveries]


def _id_millis(message_id: str) -> int | None:
    """The millisecond a ``<ms>-<seq>`` message id carries, else ``None``."""
    head, _, tail = message_id.partition("-")
    if head.isdigit() and tail.isdigit():
        return int(head)
    return None


def _event_ids(letters: Sequence[DeadLetter]) -> list[str | None]:
    return [letter.event_id for letter in letters]


class EventTransportConformance(ProviderConformance[EventTransportProvider]):
    """What an ``eventbus`` transport driver must do, after the harness's
    checks: publish and consume, at-least-once delivery, the dead-letter queue,
    ordered batches, consumer groups and group ownership."""

    contract = EventTransportProvider
    protocol = EventTransport
    spec = EVENTBUS_CAPABILITY

    #: The delivery budget the provider must be built with: an entry that
    #: fails this many times moves to the dead-letter queue.
    max_deliveries: ClassVar[int] = 3
    #: How long to wait for a redelivery (retry backoff, reclaim of a dead
    #: consumer's entries) before the entry counts as lost.
    redelivery_timeout_seconds: ClassVar[float] = 30.0
    #: How long to keep polling to show that an entry does NOT come back, once
    #: every other signal says it is gone. Longer than the retry backoff the
    #: provider is built with.
    settle_seconds: ClassVar[float] = 1.0
    #: How long an outage may take to surface as ``Unavailable``, and, doubled,
    #: how long the transport may take to serve again.
    outage_seconds: ClassVar[float] = 5.0
    #: ``True`` for a driver of the stable tier: a check whose hook is missing
    #: (``refused_stream``, ``failing_dlq_append``, ``interrupt_connections``,
    #: ``retention_cases``, ``parked_ids`` without ``dlq_ops``) fails instead of
    #: being skipped. A driver that cannot give a hook leaves the check's test
    #: out (``test_x = None``) in its own class, on the record.
    require_stable_hooks: ClassVar[bool] = False

    # ------------------------------------------------------------- the hooks
    def _hook_missing(self, hook: str, unchecked: str) -> NoReturn:
        """Skip (or, with :attr:`require_stable_hooks`, fail) a check whose
        ``hook`` the driver's tests do not give."""
        if self.require_stable_hooks:
            raise AssertionError(
                f"{hook} is not given, so {unchecked}: a driver of the stable "
                "tier must give it (require_stable_hooks)"
            )
        pytest.skip(f"no {hook} hook: {unchecked}")

    async def interrupt_connections(self, provider: EventTransportProvider) -> None:
        """Cut every connection ``provider`` holds to its backend, as a network
        outage or a failover would (``CLIENT KILL`` on Redis). The call in
        flight, or when none is, the next one, must fail: build the provider
        so that it does not retry silently. It must serve again afterwards.
        Override it to run the outage group (a stable-tier driver must); the
        default skips it."""
        self._hook_missing("interrupt_connections", "the outage is not checked")

    def refused_stream(
        self, provider: EventTransportProvider, stream: str
    ) -> AbstractAsyncContextManager[None] | None:
        """A context in which an append to ``stream`` (and only to it) fails
        with the backend's own failure; ``None``: the driver gives no way, and
        the ``publish_many`` failure check is skipped."""
        return None

    def failing_dlq_append(
        self, provider: EventTransportProvider
    ) -> AbstractAsyncContextManager[None] | None:
        """A context in which appending to a dead-letter queue fails while every
        other operation works; ``None``: no way, and the check that the source
        is acked only after the dead-letter append (on an explicit
        ``dead_letter`` and when ``max_deliveries`` is reached) is skipped."""
        return None

    async def parked_ids(
        self, provider: EventTransportProvider, stream: str
    ) -> Sequence[str] | None:
        """The domain event ids parked in ``stream``'s dead-letter queue, read
        from the backend. Used when the driver does not declare ``dlq_ops``
        (``list_dlq`` reads them otherwise). ``None``: no way; then the checks
        that an entry is parked are skipped, since a parked entry and a dropped
        one could not be told apart."""
        return None

    def retention_cases(self) -> Sequence[tuple[Mapping[str, Any], Retention]]:
        """``(options, retention)`` pairs: a provider built with ``options``
        (merged over the one under test) must report ``retention``. Empty: the
        echo check is skipped."""
        return ()

    def make_provider_with_options(
        self, provider: EventTransportProvider, options: Mapping[str, Any]
    ) -> EventTransportProvider:
        """An unopened provider of ``provider``'s driver with ``options`` merged
        over its own. Override when ``from_config`` cannot rebuild it."""
        config = InstanceConfig(
            provider.capability,
            provider.name,
            provider.driver,
            {**provider.options, **options},
        )
        return type(provider).from_config(config)

    def feature_calls(self, provider: EventTransportProvider) -> Mapping[str, Call]:
        # ``blocking_consume`` has no call that raises ``Unsupported``: without
        # it ``block_ms`` is not honoured, which the feature group checks.
        stream, group = _stream(), _group()
        return {
            "group_start_id": partial(
                provider.ensure_group, stream, group, start_id="$"
            ),
            "lease_renewal": partial(provider.renew_pending, [], "kit-feature"),
            "dlq_ops": partial(provider.list_dlq, stream),
            "inspection": partial(provider.group_info, stream),
            "inventory": provider.list_consumers,
        }

    def operation_calls(self, provider: EventTransportProvider) -> Mapping[str, Call]:
        stream, group = _stream(), _group()
        envelope = _envelope("kit-op")
        delivery = Delivery(id="1-0", stream=stream, group=group, envelope=envelope)
        owner = _owner()
        return {
            "publish": partial(provider.publish, stream, envelope),
            "publish_many": partial(
                provider.publish_many, [(stream, envelope), (stream, envelope)]
            ),
            "ensure_group": partial(provider.ensure_group, stream, group),
            "consume": partial(
                provider.consume, stream, group=group, consumer="kit-op"
            ),
            "ack": partial(provider.ack, delivery),
            "ack_many": partial(provider.ack_many, [delivery]),
            "dead_letter": partial(provider.dead_letter, delivery, reason="kit"),
            "claim_group_owner": partial(
                provider.claim_group_owner, stream, group, owner
            ),
            "group_owner": partial(provider.group_owner, stream, group),
            "list_group_owners": provider.list_group_owners,
            "transfer_group_owner": partial(
                provider.transfer_group_owner,
                stream,
                group,
                expected=owner,
                owner=_owner("kit-b"),
            ),
            "release_group_owner": partial(provider.release_group_owner, stream, group),
        }

    # ----------------------------------------------------------- the helpers
    async def _drain(
        self,
        provider: EventTransportProvider,
        stream: str,
        group: str,
        consumer: str,
        *,
        until: Callable[[list[Delivery]], bool],
        wait_seconds: float | None = None,
        count: int = 10,
    ) -> list[Delivery]:
        """Every delivery ``consume`` hands out, polling until ``until`` holds
        for what was seen or ``wait_seconds`` (default
        :attr:`redelivery_timeout_seconds`) has passed."""
        deadline = time.monotonic() + (
            self.redelivery_timeout_seconds if wait_seconds is None else wait_seconds
        )
        seen: list[Delivery] = []
        while True:
            batch = await provider.consume(
                stream, group=group, consumer=consumer, count=count
            )
            seen.extend(batch)
            if until(seen) or time.monotonic() >= deadline:
                return seen
            if not batch:
                await asyncio.sleep(_POLL_SECONDS)

    async def _read_and_ack(
        self, provider: EventTransportProvider, stream: str, group: str
    ) -> list[str]:
        """The event ids ``group`` reads over :attr:`settle_seconds`, each acked
        on arrival (so none returns): everything the group had to read."""
        read: list[str] = []
        until = time.monotonic() + self.settle_seconds
        while time.monotonic() < until:
            batch = await provider.consume(stream, group=group, consumer="kit-1")
            read.extend(_ids(batch))
            await provider.ack_many(batch)
            if not batch:
                await asyncio.sleep(_POLL_SECONDS)
        return read

    async def _first_deliveries(
        self,
        provider: EventTransportProvider,
        stream: str,
        group: str,
        consumer: str,
        wanted: int,
    ) -> list[Delivery]:
        """The first ``wanted`` deliveries of a stream; fails when fewer arrive."""
        seen = await self._drain(
            provider,
            stream,
            group,
            consumer,
            until=lambda s: len(s) >= wanted,
            wait_seconds=self.redelivery_timeout_seconds,
        )
        require(
            len(seen) == wanted,
            f"{wanted} entries were published and consume handed out "
            f"{len(seen)} within {self.redelivery_timeout_seconds:g} seconds",
        )
        return seen

    async def _dead_letter_ids(
        self, provider: EventTransportProvider, stream: str
    ) -> list[str] | None:
        """The event ids parked in ``stream``'s DLQ: read with ``list_dlq`` when
        the driver declares ``dlq_ops``, else with the :meth:`parked_ids` hook;
        ``None`` when neither can read them."""
        if "dlq_ops" in provider.features():
            listed = await provider.list_dlq(stream, count=1000)
            return [entry.event_id for entry in listed if entry.event_id is not None]
        hooked = await self.parked_ids(provider, stream)
        return None if hooked is None else list(hooked)

    async def _require_parking_observable(
        self, provider: EventTransportProvider
    ) -> None:
        """Skip (fail, with :attr:`require_stable_hooks`) when nothing reads the
        DLQ: without it a parked entry cannot be told from a dropped one."""
        if await self._dead_letter_ids(provider, _stream()) is None:
            self._hook_missing(
                "parked_ids (and no dlq_ops feature)",
                "a parked entry cannot be told from a dropped one",
            )

    async def _require_parked(
        self, provider: EventTransportProvider, stream: str, event_id: str, how: str
    ) -> None:
        """Fail unless ``event_id`` is in the DLQ; skip when the DLQ cannot be
        read (:meth:`_require_parking_observable`)."""
        await self._require_parking_observable(provider)
        deadline = time.monotonic() + self.redelivery_timeout_seconds
        parked = await self._dead_letter_ids(provider, stream)
        while parked is not None and event_id not in parked:
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS)
            parked = await self._dead_letter_ids(provider, stream)
        require(
            parked is not None and event_id in parked,
            f"{how}: the entry is not in {stream}.dlq (parked: {parked})",
        )

    async def _consume_until_parked(
        self,
        provider: EventTransportProvider,
        stream: str,
        group: str,
        event_id: str,
        how: str,
    ) -> None:
        """Keep consuming (the dead-letter append is retried from ``consume``)
        until ``event_id`` is parked, else fail."""
        deadline = time.monotonic() + self.redelivery_timeout_seconds
        parked: list[str] | None = None
        while time.monotonic() < deadline:
            parked = await self._dead_letter_ids(provider, stream)
            if parked is not None and event_id in parked:
                return
            try:
                await provider.consume(stream, group=group, consumer="kit-1")
            except InfraError:
                pass  # still recovering
            await asyncio.sleep(_POLL_SECONDS)
        raise AssertionError(
            f"{how}: the entry is not in {stream}.dlq (parked: {parked})"
        )

    # ----------------------------------------------------- publish and consume
    async def test_publish_then_consume_delivers_the_envelope(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        message_id = await provider.publish(stream, _envelope("e1"))
        require(
            isinstance(message_id, str) and bool(message_id),
            f"publish must return the transport message id, got {message_id!r}",
        )
        (delivery,) = await self._first_deliveries(provider, stream, group, "kit-1", 1)
        require(
            (delivery.stream, delivery.group) == (stream, group),
            f"a Delivery must name its stream and group, got "
            f"({delivery.stream!r}, {delivery.group!r})",
        )
        require(
            delivery.envelope == _envelope("e1"),
            f"the envelope must arrive as published, got {delivery.envelope!r}",
        )
        require(
            isinstance(delivery.id, str) and bool(delivery.id),
            "a Delivery id must be a non-empty string",
        )

    async def test_publish_returns_a_distinct_id_per_entry(
        self, provider: EventTransportProvider
    ) -> None:
        stream = _stream()
        first = await provider.publish(stream, _envelope("e1"))
        second = await provider.publish(stream, _envelope("e2"))
        require(first != second, "two publishes must return two message ids")

    async def test_consume_of_a_group_without_entries_returns_nothing(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        got = await provider.consume(stream, group=group, consumer="kit-1")
        require(got == [], f"consume of an empty stream must return [], got {got!r}")

    async def test_a_new_group_reads_the_stream_from_its_start(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.publish(stream, _envelope("early-1"))
        await provider.publish(stream, _envelope("early-2"))
        await provider.ensure_group(stream, group)
        got = await self._first_deliveries(provider, stream, group, "kit-1", 2)
        require(
            _ids(got) == ["early-1", "early-2"],
            f"a group created with the default start_id must read the entries "
            f"published before it, in order, got {_ids(got)}",
        )

    async def test_ensure_group_is_idempotent_and_keeps_the_position(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("e1"))
        (first,) = await self._first_deliveries(provider, stream, group, "kit-1", 1)
        await provider.ack(first)
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("e2"))
        got = await self._first_deliveries(provider, stream, group, "kit-1", 1)
        require(
            _ids(got) == ["e2"],
            f"ensure_group of an existing group must not reset it, got {_ids(got)}",
        )

    async def test_consumers_of_a_group_share_its_entries(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        for index in range(4):
            await provider.publish(stream, _envelope(f"e{index}"))
        first = await provider.consume(stream, group=group, consumer="kit-1", count=2)
        await provider.ack_many(first)
        second = await provider.consume(stream, group=group, consumer="kit-2", count=10)
        got = _ids(first) + _ids(second)
        require(
            got == ["e0", "e1", "e2", "e3"],
            f"the consumers of one group share its entries: each new entry goes "
            f"to one consumer, in order, got {_ids(first)} then {_ids(second)}",
        )

    async def test_two_groups_each_receive_every_entry(
        self, provider: EventTransportProvider
    ) -> None:
        stream, one, other = _stream(), _group(), _group()
        await provider.ensure_group(stream, one)
        await provider.ensure_group(stream, other)
        for index in range(3):
            await provider.publish(stream, _envelope(f"e{index}"))
        in_one = await self._first_deliveries(provider, stream, one, "kit-1", 3)
        in_other = await self._first_deliveries(provider, stream, other, "kit-1", 3)
        await provider.ack_many(in_one)
        require(
            _ids(in_one) == _ids(in_other) == ["e0", "e1", "e2"],
            f"each group must receive every entry in order, got {_ids(in_one)} "
            f"and {_ids(in_other)}",
        )
        # The ack of one group is not the ack of the other.
        again = await self._drain(
            provider,
            stream,
            other,
            "kit-1",
            until=lambda s: len(s) >= 3,
        )
        require(
            sorted(_ids(again)) == ["e0", "e1", "e2"],
            "an ack in one group must not stop the redelivery in another group",
        )

    # ---------------------------------------------------------------- publish_many
    async def test_publish_many_appends_in_order_and_returns_the_ids(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        items = [(stream, _envelope(f"e{index}")) for index in range(5)]
        ids = await provider.publish_many(items)
        require(
            len(ids) == len(items) and len(set(ids)) == len(items),
            f"publish_many must return one distinct id per item, got {ids}",
        )
        got = await self._first_deliveries(provider, stream, group, "kit-1", 5)
        require(
            _ids(got) == [f"e{index}" for index in range(5)],
            f"publish_many must append in order, a group reads {_ids(got)}",
        )
        require(
            [delivery.id for delivery in got] == ids,
            "the ids publish_many returns must be the delivery ids, in order",
        )

    async def test_publish_many_of_nothing_returns_nothing(
        self, provider: EventTransportProvider
    ) -> None:
        require(await provider.publish_many([]) == [], "publish_many([]) must be []")

    async def test_publish_many_failing_in_the_middle_raises_partial_publish(
        self, provider: EventTransportProvider
    ) -> None:
        good, bad, group = _stream(), _stream(), _group()
        refusal = self.refused_stream(provider, bad)
        if refusal is None:
            self._hook_missing(
                "refused_stream", "a failing publish_many is not checked"
            )
        await provider.ensure_group(good, group)
        items = [
            (good, _envelope("first")),
            (good, _envelope("second")),
            (bad, _envelope("third")),
            (good, _envelope("fourth")),
        ]
        caught: PartialPublish | None = None
        async with refusal:
            try:
                await provider.publish_many(items)
            except PartialPublish as exc:
                caught = exc
        if caught is None:
            raise AssertionError(
                "publish_many must raise PartialPublish when an item fails, not "
                "return as if every item was appended"
            )
        require(
            isinstance(caught, InfraError) and isinstance(caught.retryable, bool),
            "PartialPublish must be an InfraError with a bool retryable",
        )
        published = list(caught.published)
        require(
            len(published) <= 2 and all(isinstance(i, str) for i in published),
            f"PartialPublish.published names the leading items known appended "
            f"(at most the 2 before the failure), got {published}",
        )
        got = await self._drain(
            provider,
            good,
            group,
            "kit-1",
            until=lambda s: len(s) >= len(published),
            wait_seconds=10.0,
        )
        require(
            _ids(got)[: len(published)] == ["first", "second"][: len(published)],
            f"the {len(published)} ids in PartialPublish.published must be the "
            f"leading items, appended in order, a group reads {_ids(got)}",
        )
        require(
            published == [delivery.id for delivery in got[: len(published)]],
            f"PartialPublish.published must hold the message ids of the leading "
            f"items, {published} are not the delivery ids "
            f"{[delivery.id for delivery in got[: len(published)]]}",
        )

    # ------------------------------------------------------------- at least once
    async def test_ack_stops_redelivery(self, provider: EventTransportProvider) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("acked"))
        await provider.publish(stream, _envelope("control"))
        first = await self._first_deliveries(provider, stream, group, "kit-1", 2)
        await provider.ack(next(d for d in first if d.envelope["id"] == "acked"))
        seen = await self._drain(
            provider,
            stream,
            group,
            "kit-1",
            until=lambda s: "control" in _ids(s),
        )
        require(
            "control" in _ids(seen),
            "an unacked delivery must come back (at least once)",
        )
        require(
            "acked" not in _ids(seen),
            "an acked delivery was delivered again: ack must stop redelivery",
        )

    async def test_ack_many_stops_redelivery(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        for event_id in ("acked-1", "acked-2", "control"):
            await provider.publish(stream, _envelope(event_id))
        first = await self._first_deliveries(provider, stream, group, "kit-1", 3)
        await provider.ack_many([d for d in first if d.envelope["id"] != "control"])
        seen = await self._drain(
            provider,
            stream,
            group,
            "kit-1",
            until=lambda s: "control" in _ids(s),
        )
        require("control" in _ids(seen), "an unacked delivery must come back")
        require(
            not {"acked-1", "acked-2"} & set(_ids(seen)),
            f"ack_many must stop the redelivery of every delivery it names, "
            f"got {_ids(seen)} again",
        )

    async def test_an_unacked_delivery_returns_to_its_consumer(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("e1"))
        (first,) = await self._first_deliveries(provider, stream, group, "kit-1", 1)
        again = await self._drain(
            provider, stream, group, "kit-1", until=lambda s: len(s) >= 1
        )
        require(
            bool(again),
            "an unacked delivery must be redelivered after its retry backoff",
        )
        require(
            (again[0].id, again[0].envelope) == (first.id, first.envelope),
            "a redelivery must carry the same delivery id and envelope",
        )

    async def test_a_dead_consumers_pending_entries_are_reclaimed(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("e1"))
        await self._first_deliveries(provider, stream, group, "kit-dead", 1)
        # "kit-dead" never acks and never consumes again.
        taken = await self._drain(
            provider, stream, group, "kit-alive", until=lambda s: len(s) >= 1
        )
        require(
            _ids(taken) == ["e1"],
            "a consumer that died must have its pending entries reclaimed by "
            f"another consumer of the group, which got {_ids(taken)}",
        )

    # ------------------------------------------------------------ the dead letters
    async def test_max_deliveries_moves_the_entry_to_the_dlq(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("poison"))
        seen = await self._drain(
            provider,
            stream,
            group,
            "kit-1",
            until=lambda s: len(s) >= self.max_deliveries,
        )
        require(
            len(seen) == self.max_deliveries,
            f"an entry nobody acks must be delivered max_deliveries "
            f"({self.max_deliveries}) times, it was delivered {len(seen)}: build "
            f"the provider with max_deliveries={self.max_deliveries}",
        )
        extra = await self._drain(
            provider,
            stream,
            group,
            "kit-1",
            until=bool,
            wait_seconds=self.settle_seconds,
        )
        require(
            not extra,
            f"an entry must be dead-lettered after exactly max_deliveries "
            f"({self.max_deliveries}) deliveries, it was delivered again",
        )
        await self._require_parked(provider, stream, "poison", "max_deliveries")

    async def test_the_source_is_acked_only_after_the_dlq_append(
        self, provider: EventTransportProvider
    ) -> None:
        failing = self.failing_dlq_append(provider)
        if failing is None:
            self._hook_missing("failing_dlq_append", "the DLQ ordering is not checked")
        await self._require_parking_observable(provider)
        # The automatic path: an entry that reached max_deliveries is parked by
        # the transport itself. While the append fails, the source stays
        # unacked, and the append is retried once the queue is back.
        auto_stream, auto_group = _stream(), _group()
        await provider.ensure_group(auto_stream, auto_group)
        await provider.publish(auto_stream, _envelope("poison"))
        seen = await self._drain(
            provider,
            auto_stream,
            auto_group,
            "kit-1",
            until=lambda s: len(s) >= self.max_deliveries,
        )
        require(
            len(seen) == self.max_deliveries,
            f"an entry nobody acks must be delivered max_deliveries "
            f"({self.max_deliveries}) times, it was delivered {len(seen)}",
        )
        async with failing:
            until = time.monotonic() + self.settle_seconds
            while time.monotonic() < until:
                try:
                    await provider.consume(
                        auto_stream, group=auto_group, consumer="kit-1"
                    )
                except InfraError:
                    pass  # failing loudly is fine; acking the source is not
                await asyncio.sleep(_POLL_SECONDS)
        await self._consume_until_parked(
            provider,
            auto_stream,
            auto_group,
            "poison",
            "max_deliveries reached while the dead-letter append failed: the "
            "source entry was acked (or dropped) without being parked, the "
            "entry is lost; once the queue is back",
        )
        # The explicit path: dead_letter.
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("e1"))
        (delivery,) = await self._first_deliveries(provider, stream, group, "kit-1", 1)
        again = self.failing_dlq_append(provider)  # a context is entered once
        if again is None:
            raise AssertionError("failing_dlq_append gave a context, then None")
        async with again:
            try:
                await provider.dead_letter(delivery, reason="kit")
            except InfraError:
                pass  # failing loudly is fine; acking the source is not
        back = await self._drain(
            provider, stream, group, "kit-1", until=lambda s: len(s) >= 1
        )
        require(
            _ids(back) == ["e1"],
            "the source entry was acked although its DLQ append failed: the "
            "entry is lost (ack only after the dead-letter append succeeded)",
        )
        await provider.dead_letter(back[0], reason="kit")
        await self._require_parked(provider, stream, "e1", "dead_letter")

    async def test_dead_letter_parks_the_entry_at_once(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("poison"))
        await provider.publish(stream, _envelope("control"))
        first = await self._first_deliveries(provider, stream, group, "kit-1", 2)
        await provider.dead_letter(
            next(d for d in first if d.envelope["id"] == "poison"), reason="kit"
        )
        seen = await self._drain(
            provider,
            stream,
            group,
            "kit-1",
            until=lambda s: "control" in _ids(s),
        )
        require("control" in _ids(seen), "an unacked delivery must come back")
        require(
            "poison" not in _ids(seen),
            "dead_letter must park the entry at once, not leave it to be redelivered",
        )
        await self._require_parked(provider, stream, "poison", "dead_letter")

    async def test_never_dead_letter_keeps_redelivering(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group, other = _stream(), _group(), _group()
        await provider.ensure_group(stream, group)
        await provider.ensure_group(stream, other)
        provider.never_dead_letter(stream, group)
        await provider.publish(stream, _envelope("revocation"))
        wanted = self.max_deliveries + 2
        seen = await self._drain(
            provider, stream, group, "kit-1", until=lambda s: len(s) >= wanted
        )
        require(
            len(seen) >= wanted,
            f"a group exempted by never_dead_letter must see the failing entry "
            f"past max_deliveries ({self.max_deliveries}), it saw {len(seen)} "
            "deliveries",
        )
        parked = await self._dead_letter_ids(provider, stream)
        require(not parked, f"never_dead_letter: the entry reached the DLQ ({parked})")
        await provider.ack(seen[-1])
        # The exemption is the group's: another group of the stream, failing the
        # same entry, still dead-letters it after max_deliveries.
        failed = await self._drain(
            provider,
            stream,
            other,
            "kit-1",
            until=lambda s: len(s) >= self.max_deliveries,
        )
        require(
            len(failed) == self.max_deliveries,
            f"a group that was not exempted must see the failing entry "
            f"max_deliveries ({self.max_deliveries}) times, it saw {len(failed)}",
        )
        extra = await self._drain(
            provider,
            stream,
            other,
            "kit-1",
            until=bool,
            wait_seconds=self.settle_seconds,
        )
        require(
            not extra,
            "never_dead_letter exempted a group that did not ask for it: another "
            "group of the stream kept receiving the failing entry past "
            f"max_deliveries ({self.max_deliveries})",
        )
        await self._require_parked(provider, stream, "revocation", "max_deliveries")

    async def test_dead_letter_parks_even_a_never_dead_letter_group(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        provider.never_dead_letter(stream, group)
        await provider.publish(stream, _envelope("poison"))
        await provider.publish(stream, _envelope("control"))
        first = await self._first_deliveries(provider, stream, group, "kit-1", 2)
        await provider.dead_letter(
            next(d for d in first if d.envelope["id"] == "poison"), reason="kit"
        )
        seen = await self._drain(
            provider,
            stream,
            group,
            "kit-1",
            until=lambda s: "control" in _ids(s),
        )
        require("control" in _ids(seen), "an unacked delivery must come back")
        require(
            "poison" not in _ids(seen),
            "an explicit dead_letter must park the entry even in a group "
            "exempted by never_dead_letter (G-EVENT-07)",
        )

    # --------------------------------------------------------------- ownership
    async def test_the_first_claim_records_the_owner(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        require(
            await provider.group_owner(stream, group) is None,
            "a group nobody claimed has no owner",
        )
        mine = _owner("kit-a", "db:kit-one", "svc-1")
        await provider.claim_group_owner(stream, group, mine)
        require(
            await provider.group_owner(stream, group) == mine,
            "the first claim must record the owner",
        )
        listed = await provider.list_group_owners()
        require(
            (stream, group, mine) in listed,
            "list_group_owners must list every recorded owner as "
            "(stream, group, owner)",
        )

    async def test_the_same_owner_claims_again(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.claim_group_owner(stream, group, _owner("kit-a", "db:one", "x"))
        await provider.claim_group_owner(stream, group, _owner("kit-a", "db:one", "y"))
        # a replica of the same deployment: same module and database

    async def test_a_second_owner_is_refused(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        mine = _owner("kit-a", "db:one")
        await provider.claim_group_owner(stream, group, mine)
        for rival in (_owner("kit-b", "db:one"), _owner("kit-a", "db:two")):
            try:
                await provider.claim_group_owner(stream, group, rival)
            except GroupOwnedElsewhere as exc:
                require(
                    isinstance(exc, AlreadyExists) and exc.retryable is False,
                    "GroupOwnedElsewhere must be an AlreadyExists and not retryable",
                )
                continue
            raise AssertionError(
                f"a claim by {rival.module!r} on {rival.database!r} succeeded "
                f"on a group owned by {mine.module!r} on {mine.database!r}: "
                "raise GroupOwnedElsewhere"
            )
        require(
            await provider.group_owner(stream, group) == mine,
            "a refused claim must leave the recorded owner unchanged",
        )

    async def test_one_of_many_concurrent_claims_wins(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        results = await asyncio.gather(
            *(
                provider.claim_group_owner(stream, group, _owner(f"kit-racer-{i}"))
                for i in range(_RACERS)
            ),
            return_exceptions=True,
        )
        winners = [r for r in results if r is None]
        refused = [r for r in results if isinstance(r, GroupOwnedElsewhere)]
        require(
            len(winners) == 1 and len(refused) == _RACERS - 1,
            f"exactly one of {_RACERS} concurrent claims by different owners "
            f"must win and the rest raise GroupOwnedElsewhere, got "
            f"{len(winners)} winners and {len(refused)} refusals",
        )

    async def test_an_owner_is_per_stream_and_group(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.claim_group_owner(stream, group, _owner("kit-a"))
        await provider.claim_group_owner(stream, _group(), _owner("kit-b"))
        await provider.claim_group_owner(_stream(), group, _owner("kit-c"))

    async def test_transfer_is_a_compare_and_set_on_the_whole_owner(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        mine, other = _owner("kit-a", "db:one"), _owner("kit-b", "db:one")
        await provider.claim_group_owner(stream, group, mine)
        require(
            not await provider.transfer_group_owner(
                stream, group, expected=other, owner=other
            ),
            "transfer with a stale expected owner must answer False",
        )
        require(
            await provider.group_owner(stream, group) == mine,
            "a refused transfer must leave the owner unchanged",
        )
        require(
            not await provider.transfer_group_owner(
                stream, group, expected=replace(mine, service="other"), owner=other
            ),
            "the compare covers the whole owner, service included",
        )
        require(
            await provider.transfer_group_owner(
                stream, group, expected=mine, owner=other
            ),
            "transfer with the recorded owner as expected must answer True",
        )
        require(
            await provider.group_owner(stream, group) == other,
            "a transfer must record the new owner",
        )
        require(
            not await provider.transfer_group_owner(
                _stream(), _group(), expected=mine, owner=other
            ),
            "transfer of a group without an owner must answer False",
        )

    async def test_one_of_many_concurrent_transfers_wins(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        mine = _owner("kit-a")
        await provider.claim_group_owner(stream, group, mine)
        results = await asyncio.gather(
            *(
                provider.transfer_group_owner(
                    stream, group, expected=mine, owner=_owner(f"kit-racer-{i}")
                )
                for i in range(_RACERS)
            )
        )
        require(
            results.count(True) == 1,
            f"exactly one of {_RACERS} transfers expecting the same owner may "
            f"win, {results.count(True)} did: the compare-and-set is not atomic",
        )

    async def test_release_forgets_the_owner(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        require(
            not await provider.release_group_owner(stream, group),
            "release of a group without an owner must answer False",
        )
        await provider.claim_group_owner(stream, group, _owner("kit-a"))
        require(
            await provider.release_group_owner(stream, group),
            "release of an owned group must answer True",
        )
        require(
            await provider.group_owner(stream, group) is None,
            "a released group has no owner",
        )
        require(
            not await provider.release_group_owner(stream, group),
            "a second release must answer False",
        )
        await provider.claim_group_owner(stream, group, _owner("kit-b"))

    # --------------------------------------------------------------- retention
    async def test_retention_reports_a_retention(
        self, provider: EventTransportProvider
    ) -> None:
        retention = provider.retention()
        require(
            isinstance(retention, Retention),
            f"retention() must return a Retention, got {retention!r}",
        )
        for name in ("max_age_seconds", "max_length"):
            value = getattr(retention, name)
            require(
                value is None
                or (
                    isinstance(value, int) and not isinstance(value, bool) and value > 0
                ),
                f"Retention.{name} must be a positive int or None (unbounded), "
                f"got {value!r}",
            )

    async def test_retention_echoes_the_options(
        self, provider: EventTransportProvider
    ) -> None:
        cases = self.retention_cases()
        if not cases:
            self._hook_missing(
                "retention_cases", "what retention() echoes is not checked"
            )
        for options, expected in cases:
            built = self.make_provider_with_options(provider, options)
            await built.aopen()
            try:
                require(
                    built.retention() == expected,
                    f"built with {dict(options)}, retention() answers "
                    f"{built.retention()}, not {expected}",
                )
            finally:
                await built.aclose()

    # ----------------------------------------------------- group blocking_consume
    async def test_a_blocking_consume_returns_when_an_entry_arrives(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "blocking_consume")
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        waiting = asyncio.ensure_future(
            provider.consume(
                stream, group=group, consumer="kit-wait", block_ms=_BLOCK_MS
            )
        )
        try:
            await asyncio.sleep(0.2)
            require(
                not waiting.done(),
                "consume(block_ms=) of a stream without entries returned at once: "
                "it must wait for an entry",
            )
            await provider.publish(stream, _envelope("e1"))
            try:
                delivered = await asyncio.wait_for(waiting, timeout=_BLOCK_MS / 1000)
            except TimeoutError:
                raise AssertionError(
                    "an entry was published and the blocked consume did not return "
                    f"within its block time ({_BLOCK_MS} ms)"
                ) from None
        finally:
            waiting.cancel()
        require(
            _ids(delivered) == ["e1"],
            f"the blocked consume must hand out the new entry, got {_ids(delivered)}",
        )

    async def test_a_blocking_consume_of_an_empty_stream_returns_nothing_in_time(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "blocking_consume")
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        block_ms = 200
        started = time.monotonic()
        try:
            delivered = await asyncio.wait_for(
                provider.consume(
                    stream, group=group, consumer="kit-wait", block_ms=block_ms
                ),
                timeout=block_ms / 1000 + self.outage_seconds,
            )
        except TimeoutError:
            raise AssertionError(
                f"consume(block_ms={block_ms}) of a stream without entries did not "
                "return: the block time bounds the wait"
            ) from None
        waited = time.monotonic() - started
        require(delivered == [], f"nothing was published, got {_ids(delivered)}")
        require(
            waited >= block_ms / 1000 * 0.8,
            f"consume(block_ms={block_ms}) returned after {waited:.2f} s: it must "
            "wait out the block time",
        )

    # ---------------------------------------------------------- group group_start_id
    async def test_a_group_started_at_the_end_skips_the_history(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "group_start_id")
        stream, group = _stream(), _group()
        await provider.publish(stream, _envelope("e1"))
        await provider.ensure_group(stream, group, start_id="$")
        await provider.publish(stream, _envelope("e2"))
        seen = await self._read_and_ack(provider, stream, group)
        require(
            seen == ["e2"],
            f"a group started at '$' reads only what follows: expected ['e2'], "
            f"got {seen}",
        )

    async def test_a_group_started_at_a_time_reads_from_there(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "group_start_id")
        stream, group = _stream(), _group()
        await provider.publish(stream, _envelope("e1"))
        await asyncio.sleep(0.05)
        boundary = int(time.time() * 1000)
        second = await provider.publish(stream, _envelope("e2"))
        stamp = _id_millis(second)
        if stamp is not None and abs(stamp - boundary) < _CLOCK_WINDOW_MS:
            # The backend's own clock, which may differ from this machine's.
            boundary = stamp - 1
        await provider.ensure_group(stream, group, start_id=f"{boundary}-0")
        seen = await self._read_and_ack(provider, stream, group)
        require(
            seen == ["e2"],
            f"a group started at '<ms>-0' reads the entries from that time on: "
            f"expected ['e2'], got {seen}",
        )

    async def test_the_start_id_applies_to_a_new_group_only(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "group_start_id")
        stream, from_start, from_end = _stream(), _group(), _group()
        await provider.publish(stream, _envelope("e1"))
        await provider.ensure_group(stream, from_start, start_id="0")
        await provider.ensure_group(stream, from_end, start_id="$")
        await provider.ensure_group(stream, from_start, start_id="$")
        await provider.ensure_group(stream, from_end, start_id="0")
        await provider.publish(stream, _envelope("e2"))
        first = await self._read_and_ack(provider, stream, from_start)
        second = await self._read_and_ack(provider, stream, from_end)
        require(
            first == ["e1", "e2"],
            "a second ensure_group with '$' moved the position of an existing "
            f"group: it read {first}",
        )
        require(
            second == ["e2"],
            "a second ensure_group with '0' moved the position of an existing "
            f"group: it read {second}",
        )

    # ------------------------------------------------------- group lease_renewal
    async def test_the_renew_interval_is_positive(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "lease_renewal")
        interval = provider.renew_interval_seconds()
        require(
            isinstance(interval, (int, float))
            and not isinstance(interval, bool)
            and 0 < interval < float("inf"),
            f"renew_interval_seconds() must be a positive number, got {interval!r}",
        )

    async def test_renewing_nothing_is_a_no_op(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "lease_renewal")
        await provider.renew_pending([], "kit-1")

    async def test_renewal_keeps_an_entry_with_its_consumer_until_it_stops(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "lease_renewal")
        interval = provider.renew_interval_seconds()
        require(
            interval > 0 and 8 * interval <= self.redelivery_timeout_seconds,
            f"renew_interval_seconds() is {interval}: build the provider with a "
            "reclaim idle time of about a second so the kit can outlast it",
        )
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("e1"))
        (held,) = await self._first_deliveries(provider, stream, group, "kit-slow", 1)
        stolen: list[str] = []
        until = time.monotonic() + 5 * interval
        while time.monotonic() < until:
            await provider.renew_pending([held], "kit-slow")
            stolen.extend(
                _ids(await provider.consume(stream, group=group, consumer="kit-other"))
            )
            await asyncio.sleep(interval / 2)
        require(
            not stolen,
            f"the entry was renewed every {interval / 2:.2f} s for "
            f"{5 * interval:.2f} s and another consumer of the group still "
            f"received {stolen}",
        )
        taken = await self._drain(
            provider, stream, group, "kit-other", until=lambda s: len(s) >= 1
        )
        require(
            _ids(taken) == ["e1"],
            "once renewal stopped the entry must be reclaimed by another consumer "
            f"of the group, which got {_ids(taken)}",
        )

    # ------------------------------------------------------------- group dlq_ops
    async def _parked(
        self,
        provider: EventTransportProvider,
        stream: str,
        group: str,
        event_ids: Sequence[str],
    ) -> list[Delivery]:
        """Publish ``event_ids``, take them as ``group`` and dead-letter each."""
        await provider.ensure_group(stream, group)
        for event_id in event_ids:
            await provider.publish(stream, _envelope(event_id))
        taken = await self._first_deliveries(
            provider, stream, group, "kit-park", len(event_ids)
        )
        for delivery in taken:
            await provider.dead_letter(delivery, reason="kit")
        return taken

    async def test_list_dlq_describes_a_parked_entry(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "dlq_ops")
        stream, group = _stream(), _group()
        require(
            await provider.list_dlq(stream) == [],
            "a stream that parked nothing has an empty dead-letter queue",
        )
        (delivery,) = await self._parked(provider, stream, group, ["e1"])
        (letter,) = await provider.list_dlq(stream)
        require(isinstance(letter, DeadLetter), f"list_dlq returned {letter!r}")
        require(
            letter.event_id == "e1", f"the parked event is e1, got {letter.event_id!r}"
        )
        require(
            letter.group == group,
            f"the entry was parked by {group!r}, list_dlq says {letter.group!r}",
        )
        require(
            letter.original_id == delivery.id,
            f"original_id must be the delivery id {delivery.id!r}, got "
            f"{letter.original_id!r}",
        )
        require(
            bool(letter.id) and isinstance(letter.reason, str) and bool(letter.reason),
            f"a parked entry has an id and a reason: {letter!r}",
        )
        require(letter.replayable, "an entry that kept its payload is replayable")

    async def test_list_dlq_pages_in_order(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "dlq_ops")
        stream, group = _stream(), _group()
        await self._parked(provider, stream, group, ["e1", "e2", "e3"])
        everything = await provider.list_dlq(stream, count=1000)
        require(
            _event_ids(everything) == ["e1", "e2", "e3"],
            "list_dlq lists in the order entries were parked, got "
            f"{_event_ids(everything)}",
        )
        page = await provider.list_dlq(stream, count=2)
        require(
            _event_ids(page) == ["e1", "e2"],
            f"list_dlq(count=2) returns the first two, got {_event_ids(page)}",
        )
        rest = await provider.list_dlq(stream, after=page[-1].id)
        require(
            _event_ids(rest) == ["e3"],
            f"list_dlq(after=<id>) returns what follows it, got {_event_ids(rest)}",
        )
        require(
            await provider.list_dlq(stream, after=everything[-1].id) == [],
            "nothing follows the last entry",
        )

    async def test_replay_returns_the_entry_to_the_group_that_failed_it(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "dlq_ops")
        stream, failing, bystander = _stream(), _group(), _group()
        await provider.ensure_group(stream, bystander)
        await provider.publish(stream, _envelope("e1"))
        # The bystander handled e1 for good; the failing group parked it.
        (seen,) = await self._first_deliveries(provider, stream, bystander, "kit-b", 1)
        await provider.ack(seen)
        await provider.ensure_group(stream, failing)
        (poison,) = await self._first_deliveries(provider, stream, failing, "kit-a", 1)
        await provider.dead_letter(poison, reason="kit")
        replayed = await provider.replay_dlq(stream)
        require(replayed == 1, f"replay_dlq must return 1, returned {replayed}")
        require(
            await provider.list_dlq(stream) == [],
            "a replayed entry leaves the dead-letter queue",
        )
        await provider.publish(stream, _envelope("control"))
        again = await self._drain(
            provider,
            stream,
            failing,
            "kit-a",
            until=lambda s: {"e1", "control"} <= set(_ids(s)),
        )
        require(
            "e1" in _ids(again),
            f"the group that failed the entry must receive it again, got {_ids(again)}",
        )
        others = await self._drain(
            provider,
            stream,
            bystander,
            "kit-b",
            until=lambda s: "control" in _ids(s),
        )
        require(
            "control" in _ids(others) and "e1" not in _ids(others),
            "a replayed entry is for the group that failed it: another group "
            f"must not receive it again, it got {_ids(others)}",
        )

    async def test_replay_honours_ids_and_count(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "dlq_ops")
        stream, group = _stream(), _group()
        await self._parked(provider, stream, group, ["e1", "e2", "e3"])
        letters = await provider.list_dlq(stream)
        require(
            await provider.replay_dlq(stream, ids=["9-9"]) == 0,
            "replay_dlq of an id that is not parked must replay nothing",
        )
        require(
            await provider.replay_dlq(stream, ids=[letters[1].id]) == 1,
            "replay_dlq(ids=[one id]) must replay exactly that entry",
        )
        left = _event_ids(await provider.list_dlq(stream))
        require(left == ["e1", "e3"], f"e2 was replayed, the DLQ holds {left}")
        require(
            await provider.replay_dlq(stream, count=1) == 1,
            "replay_dlq(count=1) must replay one entry",
        )
        left = _event_ids(await provider.list_dlq(stream))
        require(left == ["e3"], f"e1 was replayed next, the DLQ holds {left}")
        got = await self._drain(
            provider, stream, group, "kit-a", until=lambda s: len(s) >= 2
        )
        require(
            sorted(_ids(got)) == ["e1", "e2"],
            f"the group must receive the two replayed entries, got {_ids(got)}",
        )

    async def test_replay_can_be_limited_to_one_group(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "dlq_ops")
        stream, one, two = _stream(), _group(), _group()
        await provider.ensure_group(stream, one)
        await provider.ensure_group(stream, two)
        await provider.publish(stream, _envelope("e1"))
        await provider.publish(stream, _envelope("e2"))
        by_one = await self._first_deliveries(provider, stream, one, "kit-a", 2)
        by_two = await self._first_deliveries(provider, stream, two, "kit-b", 2)
        await provider.dead_letter(by_one[0], reason="kit")
        await provider.ack(by_one[1])
        await provider.ack(by_two[0])
        await provider.dead_letter(by_two[1], reason="kit")
        require(
            await provider.replay_dlq(stream, group="kit-no-such-group") == 0,
            "replay_dlq(group=) of a group that parked nothing must replay nothing",
        )
        require(
            await provider.replay_dlq(stream, group=one) == 1,
            "replay_dlq(group=) must replay only the entries that group parked",
        )
        left = _event_ids(await provider.list_dlq(stream))
        require(left == ["e2"], f"the other group's entry stays parked, got {left}")

    # ------------------------------------------------------------ group inspection
    async def test_group_info_reports_position_and_pending(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "inspection")
        stream, reader, idle = _stream(), _group(), _group()
        require(
            await provider.group_info(stream) == [],
            "a stream without groups reports no group",
        )
        await provider.ensure_group(stream, reader)
        await provider.ensure_group(stream, idle)
        for event_id in ("e1", "e2"):
            await provider.publish(stream, _envelope(event_id))

        async def info(name: str) -> Any:
            found = [g for g in await provider.group_info(stream) if g.group == name]
            require(len(found) == 1, f"group_info must list {name!r} once: {found}")
            return found[0]

        waiting = await info(reader)
        require(waiting.pending == 0, f"nothing was delivered: {waiting}")
        require(waiting.lag in (None, 2), f"two entries wait for the group: {waiting}")
        taken = await self._first_deliveries(provider, stream, reader, "kit-1", 2)
        delivered = await info(reader)
        require(delivered.pending == 2, f"two deliveries are unacked: {delivered}")
        require(
            delivered.last_delivered_id == taken[-1].id,
            f"last_delivered_id must be {taken[-1].id!r}: {delivered}",
        )
        require(
            (await info(idle)).pending == 0,
            "a group that read nothing has nothing pending",
        )
        await provider.ack_many(taken)
        require(
            (await info(reader)).pending == 0, "acked deliveries are no longer pending"
        )

    async def test_stream_info_reports_length_and_the_newest_entry(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "inspection")
        stream = _stream()
        empty = await provider.stream_info(stream)
        require(
            (empty.length, empty.last_id, empty.last_entry_age_seconds)
            == (0, None, None),
            f"a stream without entries reports none: {empty}",
        )
        ids = [await provider.publish(stream, _envelope(f"e{i}")) for i in range(3)]
        info = await provider.stream_info(stream)
        require(info.length == 3, f"three entries were published: {info}")
        require(
            info.last_id == ids[-1],
            f"last_id must be the id publish returned last, {ids[-1]!r}: {info}",
        )
        age = info.last_entry_age_seconds
        require(age is None or age >= 0, f"an age is never negative: {info}")

    async def test_the_oldest_unacked_age_follows_the_group(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "inspection")
        stream, group = _stream(), _group()
        require(
            await provider.oldest_unacked_age_seconds(stream, group) is None,
            "a group that does not exist has no age (None)",
        )
        await provider.ensure_group(stream, group)
        require(
            await provider.oldest_unacked_age_seconds(stream, group) == 0.0,
            "a group with nothing unacked has age 0.0",
        )
        await provider.publish(stream, _envelope("e1"))
        await asyncio.sleep(0.25)
        age = await provider.oldest_unacked_age_seconds(stream, group)
        require(
            age is not None and age > 0.0,
            f"an entry waiting for the group counts, got {age!r}",
        )
        (taken,) = await self._first_deliveries(provider, stream, group, "kit-1", 1)
        age = await provider.oldest_unacked_age_seconds(stream, group)
        require(
            age is not None and age > 0.0,
            f"a delivered entry stays unacked until it is acked, got {age!r}",
        )
        await provider.ack(taken)
        require(
            await provider.oldest_unacked_age_seconds(stream, group) == 0.0,
            "an acked entry no longer counts",
        )

    # ------------------------------------------------------------- group inventory
    async def test_the_consumer_inventory_lists_and_forgets(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "inventory")
        stream, group = _stream(), _group()
        info = ConsumerInfo(
            stream=stream,
            group=group,
            module="kit-a",
            database="db:kit-one",
            service="kit-svc",
            consumer="kit-1",
        )

        async def mine(*, include_stale: bool = False) -> list[ConsumerInfo]:
            listed = await provider.list_consumers(include_stale=include_stale)
            return [c for c in listed if (c.stream, c.group) == (stream, group)]

        try:
            require(not await mine(), "an unregistered consumer is not listed")
            await provider.register_consumer(stream, group, info)
            (entry,) = await mine()
            require(
                (entry.module, entry.database, entry.service, entry.consumer)
                == ("kit-a", "db:kit-one", "kit-svc", "kit-1"),
                f"the inventory must list what was registered: {entry}",
            )
            require(
                not entry.stale and entry.last_seen is not None,
                f"a consumer registered just now is fresh and seen: {entry}",
            )
            require(
                len(await mine(include_stale=True)) == 1,
                "include_stale=True lists fresh consumers too",
            )
            require(
                await provider.forget_consumer(stream, group),
                "forget_consumer of a listed consumer must answer True",
            )
            require(not await mine(), "a forgotten consumer is not listed")
            require(
                not await provider.forget_consumer(stream, group),
                "forget_consumer of an unknown consumer must answer False",
            )
        finally:
            await provider.forget_consumer(stream, group)

    async def test_registering_again_replaces_the_entry(
        self, provider: EventTransportProvider
    ) -> None:
        self.requires_feature(provider, "inventory")
        stream, group = _stream(), _group()
        first = ConsumerInfo(
            stream=stream, group=group, module="kit-a", database="db:kit-one"
        )
        try:
            await provider.register_consumer(stream, group, first)
            await provider.register_consumer(
                stream, group, replace(first, module="kit-b", consumer="kit-2")
            )
            listed = [
                c
                for c in await provider.list_consumers()
                if (c.stream, c.group) == (stream, group)
            ]
            require(
                [(c.module, c.consumer) for c in listed] == [("kit-b", "kit-2")],
                f"one entry per (stream, group), the last registered: {listed}",
            )
        finally:
            await provider.forget_consumer(stream, group)

    # ---------------------------------------------------------------- group shared
    async def test_two_providers_of_one_configuration_share_streams_and_groups(
        self, make_provider: Callable[[], EventTransportProvider]
    ) -> None:
        first, second = make_provider(), make_provider()
        await first.aopen()
        await second.aopen()
        try:
            self.requires_feature(first, "shared")
            stream, group = _stream(), _group()
            await first.ensure_group(stream, group)
            await first.publish(stream, _envelope("e1"))
            (delivery,) = await self._first_deliveries(
                second, stream, group, "kit-2", 1
            )
            require(
                _ids([delivery]) == ["e1"],
                "an entry published through one provider must reach a consumer "
                "of another provider of the same configuration",
            )
            await second.ack(delivery)
            await first.claim_group_owner(stream, group, _owner())
            require(
                await second.group_owner(stream, group) == _owner(),
                "an owner claimed through one provider must be visible through "
                "the other",
            )
            require(
                await second.release_group_owner(stream, group)
                and await first.group_owner(stream, group) is None,
                "a release through one provider must reach the other",
            )
        finally:
            await first.aclose()
            await second.aclose()

    # ------------------------------------------------------------- group outage
    async def _expect_unavailable(self, task: asyncio.Future[Any], what: str) -> None:
        """``task`` raises ``Unavailable`` in time: not silently, not raw."""
        try:
            result = await asyncio.wait_for(task, timeout=self.outage_seconds)
        except Unavailable:
            return
        except TimeoutError:
            raise AssertionError(
                f"the connection was cut and {what} neither raised nor returned "
                f"within {self.outage_seconds} s"
            ) from None
        except Exception as exc:
            raise AssertionError(
                f"the connection was cut and {what} raised {type(exc).__name__}: "
                "an outage must surface as Unavailable (map the backend's errors "
                "to InfraError subclasses, sandloom.driverkit)"
            ) from exc
        raise AssertionError(
            f"the connection was cut and {what} returned {result!r}: it must raise "
            "Unavailable, not reconnect behind the caller's back"
        )

    async def test_an_outage_during_consume_raises_unavailable_and_consuming_resumes(
        self, provider: EventTransportProvider
    ) -> None:
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
        for event_id in ("e1", "e2", "e3"):
            await provider.publish(stream, _envelope(event_id))
        # e1 acked, e2 delivered and unacked, e3 not yet read.
        first = await self._drain(
            provider,
            stream,
            group,
            "kit-out",
            until=lambda s: len(s) >= 2,
            count=2,
        )
        require(_ids(first) == ["e1", "e2"], f"the setup read {_ids(first)}")
        await provider.ack(first[0])
        if "blocking_consume" in provider.features():
            # A consume in flight: it waits on a stream without entries.
            idle = _stream()
            await provider.ensure_group(idle, group)
            inflight = asyncio.ensure_future(
                provider.consume(
                    idle, group=group, consumer="kit-out", block_ms=10 * _BLOCK_MS
                )
            )
            await asyncio.sleep(0.2)
            try:
                await self.interrupt_connections(provider)
                await self._expect_unavailable(inflight, "the blocked consume")
            finally:
                inflight.cancel()
        else:
            await self.interrupt_connections(provider)
            await self._expect_unavailable(
                asyncio.ensure_future(
                    provider.consume(stream, group=group, consumer="kit-out", count=1)
                ),
                "the next consume",
            )
        seen: list[str] = []
        published = False
        deadline = (
            time.monotonic() + 2 * self.outage_seconds + self.redelivery_timeout_seconds
        )
        while not {"e2", "e3", "e4"} <= set(seen):
            try:
                if not published:
                    await provider.publish(stream, _envelope("e4"))
                    published = True
                batch = await provider.consume(
                    stream, group=group, consumer="kit-out", count=10
                )
                seen.extend(_ids(batch))
            except Unavailable:
                pass
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS)
        require(
            {"e2", "e3", "e4"} <= set(seen),
            f"consuming must resume without loss after an outage: expected e2 "
            f"(unacked), e3 (unread) and e4 (published after), got {seen}",
        )
        require("e1" not in seen, "an entry acked before the outage came back")


__all__ = ["EventTransportConformance"]
