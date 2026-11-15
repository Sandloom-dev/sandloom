"""``EventTransportConformance``: what an ``eventbus`` transport driver must do
(spec 7), core groups.

A driver's tests subclass the kit and override ``make_provider`` (an unopened
:class:`EventTransportProvider`; the harness checks that it opens, closes twice,
declares its features, reports health and its isolation domain before these
groups). Every test uses stream names of its own: entries outlive a test on a
shared backend.

What the kit cannot learn from the provider, the driver's tests state:

* the delivery budget: build the provider with ``max_deliveries`` equal to
  :attr:`EventTransportConformance.max_deliveries` (default 3) and the shortest
  retry backoff the driver allows (a few milliseconds), and, when the driver
  reclaims a dead consumer's pending entries after an idle time, an idle time
  of about a second;
* how to make the backend misbehave (:meth:`~EventTransportConformance.refused_stream`,
  :meth:`~EventTransportConformance.failing_dlq_append`): a check that needs a
  hook the driver does not give is skipped, never passed;
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
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import AlreadyExists, InfraError
from sandloom.core.infra.ports.eventbus import (
    EVENTBUS_CAPABILITY,
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

    # ------------------------------------------------------------- the hooks
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
        is acked only after the dead-letter append is skipped."""
        return None

    def retention_cases(self) -> Sequence[tuple[Mapping[str, Any], Retention]]:
        """``(options, retention)`` pairs: a provider built with ``options``
        (merged over the one under test) must report ``retention``."""
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
            wait_seconds=10.0,
        )
        require(
            len(seen) == wanted,
            f"{wanted} entries were published and consume handed out "
            f"{len(seen)} within 10 seconds",
        )
        return seen

    async def _dead_letter_ids(
        self, provider: EventTransportProvider, stream: str
    ) -> list[str] | None:
        """The event ids parked in ``stream``'s DLQ, or ``None`` when the driver
        has no ``dlq_ops`` to read them with."""
        if "dlq_ops" not in provider.features():
            return None
        listed = await provider.list_dlq(stream, count=1000)
        return [entry.event_id for entry in listed if entry.event_id is not None]

    async def _require_parked(
        self, provider: EventTransportProvider, stream: str, event_id: str, how: str
    ) -> None:
        """Fail unless ``event_id`` is in the DLQ (only when it can be read)."""
        if "dlq_ops" not in provider.features():
            return
        deadline = time.monotonic() + self.redelivery_timeout_seconds
        parked = await self._dead_letter_ids(provider, stream)
        while parked is not None and event_id not in parked:
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS)
            parked = await self._dead_letter_ids(provider, stream)
        require(
            parked is not None and event_id in parked,
            f"{how}: the entry is not in {stream}.dlq (list_dlq: {parked})",
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
        await provider.ack_many(in_one)
        in_other = await self._first_deliveries(provider, stream, other, "kit-1", 3)
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
            pytest.skip("no refused_stream hook: a failing publish_many is not checked")
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
        stream, group = _stream(), _group()
        failing = self.failing_dlq_append(provider)
        if failing is None:
            pytest.skip("no failing_dlq_append hook: the DLQ ordering is not checked")
        await provider.ensure_group(stream, group)
        await provider.publish(stream, _envelope("e1"))
        (delivery,) = await self._first_deliveries(provider, stream, group, "kit-1", 1)
        async with failing:
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
        stream, group = _stream(), _group()
        await provider.ensure_group(stream, group)
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
        for options, expected in self.retention_cases():
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


__all__ = ["EventTransportConformance"]
