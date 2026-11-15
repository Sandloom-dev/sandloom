"""The ``streams`` driver maps the redis client's errors at its boundary.

Every public operation of the transport answers a backend failure with an
``InfraError`` (``Unavailable``, ``Timeout``, ``PermissionDenied``), carrying
the error's type and never its text (which can hold the broker URL). The table
of operations below is checked against the transport's own public coroutines,
so an operation added without its mapping fails here.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from sandloom.core.drivers import event_bus_driver
from sandloom.core.drivers.redis_eventbus import streams_groups, streams_transport
from sandloom.core.drivers.redis_eventbus.streams_transport import RedisStreamsTransport
from sandloom.core.tasks.relay import is_broker_unavailable
from sandloom.driverkit import (
    ConsumerInfo,
    Delivery,
    EventEnvelope,
    GroupOwner,
    InfraError,
    PartialPublish,
    PermissionDenied,
    Timeout,
    Unavailable,
)

redis_exceptions = pytest.importorskip("redis.exceptions")
prometheus_client = pytest.importorskip("prometheus_client")

SECRET = "hunter2-the-broker-password"
ENVELOPE: EventEnvelope = {
    "id": "e1",
    "channel": "x.event.v1",
    "schema_version": "v1",
    "trace_id": None,
    "emitted_at": "2026-01-01T00:00:00+00:00",
    "payload": {},
}
DELIVERY = Delivery(id="1-0", stream="s", group="g", envelope=ENVELOPE)
OWNER = GroupOwner(module="m", database="db:one", service=None)


class _FailingPipeline:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> _FailingPipeline:
        raise self._error

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FailingClient:
    """A client whose every command raises ``error``."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __getattr__(self, command: str) -> Callable[..., Awaitable[Any]]:
        async def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise self._error

        return fail

    def pipeline(self, **_options: Any) -> _FailingPipeline:
        return _FailingPipeline(self._error)

    def scan_iter(self, **_options: Any) -> AsyncIterator[Any]:
        return self._scan()

    async def _scan(self) -> AsyncIterator[Any]:
        for _ in ():
            yield _  # pragma: no cover - makes this an async generator
        raise self._error


def _transport(error: BaseException) -> RedisStreamsTransport:
    client = _FailingClient(error)

    async def factory() -> Any:
        return client

    return RedisStreamsTransport(client_factory=factory, retry_base_ms=0)


#: Every public coroutine of the transport, called with arguments that reach
#: the client (an empty batch would return before any I/O).
OPERATIONS: dict[str, Callable[[RedisStreamsTransport], Awaitable[Any]]] = {
    "publish": lambda t: t.publish("s", ENVELOPE),
    "publish_many": lambda t: t.publish_many([("s", ENVELOPE), ("s", ENVELOPE)]),
    "list_dlq": lambda t: t.list_dlq("s"),
    "replay_dlq": lambda t: t.replay_dlq("s"),
    "consume": lambda t: t.consume("s", group="g", consumer="c"),
    "ack": lambda t: t.ack(DELIVERY),
    "ack_many": lambda t: t.ack_many([DELIVERY]),
    "renew_pending": lambda t: t.renew_pending([DELIVERY], "c"),
    "dead_letter": lambda t: t.dead_letter(DELIVERY, reason="x"),
    "stats": lambda t: t.stats("s", "g"),
    "ensure_group": lambda t: t.ensure_group("s", "g"),
    "group_info": lambda t: t.group_info("s"),
    "stream_info": lambda t: t.stream_info("s"),
    "oldest_unacked_age_seconds": lambda t: t.oldest_unacked_age_seconds("s", "g"),
    "claim_group_owner": lambda t: t.claim_group_owner("s", "g", OWNER),
    "group_owner": lambda t: t.group_owner("s", "g"),
    "release_group_owner": lambda t: t.release_group_owner("s", "g"),
    "list_group_owners": lambda t: t.list_group_owners(),
    "transfer_group_owner": lambda t: t.transfer_group_owner(
        "s", "g", expected=OWNER, owner=OWNER
    ),
    "register_consumer": lambda t: t.register_consumer(
        "s",
        "g",
        ConsumerInfo(stream="s", group="g", module="m", database="db:one"),
    ),
    "list_consumers": lambda t: t.list_consumers(),
    "forget_consumer": lambda t: t.forget_consumer("s", "g"),
}


def test_the_table_covers_every_public_operation_of_the_transport() -> None:
    public = {
        name
        for name, member in inspect.getmembers(RedisStreamsTransport)
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }
    assert public == set(OPERATIONS)


#: ``dead_letter`` keeps its documented answer to a failing dead-letter append:
#: it logs, counts ``streams_dlq_append_failures_total`` and leaves the entry
#: pending for a later retry (never acked into oblivion), so it raises nothing.
RAISES = sorted(set(OPERATIONS) - {"dead_letter"})


@pytest.mark.parametrize("operation", RAISES)
@pytest.mark.parametrize(
    ("raw", "mapped"),
    [
        (redis_exceptions.ConnectionError(SECRET), Unavailable),
        (redis_exceptions.BusyLoadingError(SECRET), Unavailable),
        (redis_exceptions.ReadOnlyError(SECRET), Unavailable),
        (OSError(SECRET), Unavailable),
        (redis_exceptions.TimeoutError(SECRET), Timeout),
        (TimeoutError(SECRET), Timeout),
        (redis_exceptions.AuthenticationError(SECRET), PermissionDenied),
        (redis_exceptions.NoPermissionError(SECRET), PermissionDenied),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else None,
)
async def test_an_operation_raises_an_infra_error_never_the_clients(
    operation: str, raw: BaseException, mapped: type[InfraError]
) -> None:
    transport = _transport(raw)
    with pytest.raises(InfraError) as caught:
        await OPERATIONS[operation](transport)
    error = caught.value
    if isinstance(error, PartialPublish):  # publish_many: nothing was appended
        assert list(error.published) == []
    else:
        assert type(error) is mapped
        assert error.capability == "eventbus"
    assert SECRET not in str(error)


@pytest.mark.parametrize("operation", ["publish", "ack", "claim_group_owner"])
async def test_a_refused_command_on_a_write_is_unavailable(operation: str) -> None:
    transport = _transport(redis_exceptions.ResponseError(f"WRONGTYPE {SECRET}"))
    with pytest.raises(Unavailable) as caught:
        await OPERATIONS[operation](transport)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        redis_exceptions.ConnectionError(SECRET),
        redis_exceptions.TimeoutError(SECRET),
        redis_exceptions.BusyLoadingError(SECRET),
        OSError(SECRET),
    ],
    ids=lambda error: type(error).__name__,
)
@pytest.mark.parametrize("operation", ["publish", "publish_many"])
async def test_the_relay_still_reads_a_mapped_outage_as_an_outage(
    operation: str, raw: BaseException
) -> None:
    # An outbox row is not charged an attempt for a broker outage; the relay
    # tells the two apart by walking the error's chain.
    transport = _transport(raw)
    with pytest.raises(InfraError) as caught:
        await OPERATIONS[operation](transport)
    assert is_broker_unavailable(caught.value)


async def test_a_failing_dead_letter_append_leaves_the_entry_pending() -> None:
    transport = _transport(redis_exceptions.ConnectionError(SECRET))
    await OPERATIONS["dead_letter"](transport)  # logs and counts, raises nothing


async def test_the_instance_name_of_a_provider_is_on_the_error() -> None:
    async def factory() -> Any:
        return _FailingClient(redis_exceptions.ConnectionError(SECRET))

    provider = event_bus_driver.StreamsEventBusProvider(
        name="bus-two", driver="streams", options={}, client_factory=factory
    )
    with pytest.raises(Unavailable) as caught:
        await provider.ack(DELIVERY)
    assert caught.value.instance == "bus-two"


async def test_a_reply_that_is_not_a_backend_failure_is_not_mapped() -> None:
    transport = _transport(ValueError("a bug of the caller, not of the backend"))
    with pytest.raises(ValueError, match="a bug of the caller"):
        await transport.ack(DELIVERY)


async def test_a_missing_stream_is_still_empty_not_an_error() -> None:
    transport = _transport(redis_exceptions.ResponseError("ERR no such key"))
    assert await transport.group_info("missing") == []
    assert await transport.stats("missing", "g") == {"pending": 0, "dlq": 0}


@pytest.mark.parametrize(
    "error",
    [
        redis_exceptions.ConnectionError(SECRET),
        redis_exceptions.NoPermissionError(SECRET),
        redis_exceptions.ReadOnlyError(SECRET),
    ],
    ids=lambda error: type(error).__name__,
)
@pytest.mark.parametrize(
    "operation", ["group_info", "stats", "ensure_group", "stream_info"]
)
async def test_a_server_in_trouble_is_never_read_as_an_empty_answer(
    operation: str, error: BaseException
) -> None:
    transport = _transport(error)
    with pytest.raises(InfraError):
        await OPERATIONS[operation](transport)


def test_the_recorded_metrics_keep_their_names_and_labels() -> None:
    cases = [
        (streams_groups._METRIC_GROUP_RECREATED, "streams_group_recreated_total"),
        (
            streams_groups._METRIC_GROUP_RECREATED_AT_TAIL,
            "streams_group_recreated_at_tail_total",
        ),
        (streams_transport._METRIC_DLQ_REPLAYED, "streams_dlq_replayed_total"),
    ]
    for metric, name in cases:
        assert metric.name == name
        assert metric.labelnames == ("stream",)
        labels = {"stream": f"error-mapping.{name}"}
        before = prometheus_client.REGISTRY.get_sample_value(name, labels)
        metric.labels(**labels).inc()
        after = prometheus_client.REGISTRY.get_sample_value(name, labels)
        assert after == (before or 0) + 1
