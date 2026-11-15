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
import sandloom_redis.eventbus.driver as event_bus_driver
from sandloom_redis.eventbus import streams_groups, streams_transport
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport
from sandloom_redis.eventbus.streams_transport_base import _PendingEntry

from sandloom.core.observability.unreachable import CODE, connection_failure
from sandloom.core.tasks.relay import is_broker_unavailable
from sandloom.driverkit import (
    ConsumerInfo,
    Delivery,
    EventEnvelope,
    GroupOwner,
    InfraError,
    InvalidArgument,
    PartialPublish,
    PermissionDenied,
    Timeout,
    Unavailable,
)
from sandloom.driverkit.eventbus_support import (
    DLQ_REASON_MAX_DELIVERIES,
    DLQ_REASON_SOURCE_MISSING,
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


#: What a healthy Redis answers each command with when a test does not script
#: it: nothing there, nothing pending.
_HEALTHY: dict[str, Any] = {
    "xgroup_create": True,
    "xinfo_groups": [],
    "xautoclaim": ("0-0", [], []),
    "xpending_range": [],
    "xpending": {"pending": 0},
    "xreadgroup": [],
    "xrevrange": [],
    "xrange": [],
    "xlen": 0,
    "hget": None,
    "xadd": "1-0",
    "xack": 1,
}


class _ScriptedClient:
    """A client that answers the commands a test scripts and fails ONE chosen
    command: every other command answers like a healthy, empty Redis.

    A scripted value is returned as is; an exception is raised; a callable is
    called with the command's arguments (and may raise). Every call is kept in
    ``calls`` as ``(command, args, kwargs)``."""

    def __init__(self, **replies: Any) -> None:
        self._replies = {**_HEALTHY, **replies}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def count(self, command: str) -> int:
        return sum(1 for name, _args, _kwargs in self.calls if name == command)

    def __getattr__(self, command: str) -> Callable[..., Awaitable[Any]]:
        async def run(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((command, args, kwargs))
            reply = self._replies[command]
            if isinstance(reply, BaseException):
                raise reply
            return reply(*args, **kwargs) if callable(reply) else reply

        return run


def _nogroup_once() -> Callable[..., Any]:
    """An ``XREADGROUP`` that loses its group once, then answers empty."""
    seen: list[None] = []

    def reply(*_args: Any, **_kwargs: Any) -> list[Any]:
        if not seen:
            seen.append(None)
            raise redis_exceptions.ResponseError(
                "NOGROUP No such key 's' or consumer group 'g' in XREADGROUP"
            )
        return []

    return reply


def _transport_of(client: Any) -> RedisStreamsTransport:
    async def factory() -> Any:
        return client

    return RedisStreamsTransport(client_factory=factory, retry_base_ms=0)


def _transport(error: BaseException) -> RedisStreamsTransport:
    return _transport_of(_FailingClient(error))


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
        (redis_exceptions.DataError(SECRET), InvalidArgument),
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
        assert error.retryable is mapped.retryable  # follows the mapped cause
    else:
        assert type(error) is mapped
        assert error.capability == "eventbus"
    assert SECRET not in str(error)


@pytest.mark.parametrize("operation", ["publish", "ack", "claim_group_owner"])
async def test_a_wrong_type_on_a_write_is_not_retryable(operation: str) -> None:
    # The key holds another type: repeating the write cannot succeed, so it is
    # ``InvalidArgument`` (the model's non-retryable class), not an outage.
    transport = _transport(redis_exceptions.ResponseError(f"WRONGTYPE {SECRET}"))
    with pytest.raises(InvalidArgument) as caught:
        await OPERATIONS[operation](transport)
    assert caught.value.retryable is False
    assert SECRET not in str(caught.value)


async def test_a_value_the_client_cannot_encode_is_not_retryable() -> None:
    transport = _transport(redis_exceptions.DataError("Invalid input of type: 'X'"))
    with pytest.raises(InvalidArgument) as caught:
        await transport.publish("s", ENVELOPE)
    assert caught.value.retryable is False


class _ExecPipeline:
    """A MULTI/EXEC whose ``EXEC`` answers ``results`` (one error per refused XADD)."""

    def __init__(self, results: list[Any]) -> None:
        self._results = results

    async def __aenter__(self) -> _ExecPipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def xadd(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def execute(self, **_options: Any) -> list[Any]:
        return self._results


class _PipelineClient:
    def __init__(self, results: list[Any]) -> None:
        self._results = results

    def pipeline(self, **_options: Any) -> _ExecPipeline:
        return _ExecPipeline(self._results)


async def test_a_refused_xadd_in_exec_is_reported_by_type_and_follows_its_class() -> (
    None
):
    wrong_type = redis_exceptions.ResponseError(f"WRONGTYPE {SECRET}")
    transport = _transport_of(_PipelineClient(["1-0", wrong_type]))
    with pytest.raises(PartialPublish) as caught:
        await transport.publish_many([("s", ENVELOPE), ("s", ENVELOPE)])
    assert caught.value.published == ["1-0"]
    assert caught.value.retryable is False
    assert SECRET not in str(caught.value)


async def test_a_permission_refusal_of_the_multi_exec_is_not_retryable() -> None:
    transport = _transport(redis_exceptions.NoPermissionError(SECRET))
    with pytest.raises(PartialPublish) as caught:
        await transport.publish_many([("s", ENVELOPE), ("s", ENVELOPE)])
    assert caught.value.retryable is False
    # chained from the MAPPED error (the relay stops there), the raw one below it
    assert isinstance(caught.value.__cause__, PermissionDenied)
    assert isinstance(
        caught.value.__cause__.__cause__, redis_exceptions.NoPermissionError
    )


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


class _InfoClient:
    """A client whose ``INFO server`` answers ``reply`` (or raises it)."""

    def __init__(self, reply: Any) -> None:
        self._reply = reply

    async def info(self, section: str) -> Any:
        assert section == "server"
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply


def _identity_provider(reply: Any, *, name: str = "bus") -> Any:
    async def factory() -> Any:
        return _InfoClient(reply)

    return event_bus_driver.StreamsEventBusProvider(
        name=name, driver="streams", options={}, client_factory=factory
    )


@pytest.mark.parametrize(
    "reply", [{"run_id": "abc123"}, {b"run_id": b"abc123"}, {"run_id": b"abc123"}]
)
async def test_the_isolation_identity_is_the_driver_and_the_server_run_id(
    reply: dict[Any, Any],
) -> None:
    """Same server, same identity, whatever the instance name; the driver name
    leads (spec 2.7 item 6)."""
    assert await _identity_provider(reply).isolation_identity() == "streams:abc123"
    assert (
        await _identity_provider(reply, name="other").isolation_identity()
        == "streams:abc123"
    )
    assert (
        await _identity_provider({"run_id": "zzz"}).isolation_identity()
        == "streams:zzz"
    )


@pytest.mark.parametrize("reply", [{}, {"run_id": ""}, {"run_id": None}, {"run_id": 7}])
async def test_a_server_without_a_run_id_refuses_its_identity(
    reply: dict[Any, Any],
) -> None:
    with pytest.raises(Unavailable) as caught:
        await _identity_provider(reply, name="bus-three").isolation_identity()
    assert caught.value.instance == "bus-three"


async def test_a_failing_info_call_is_mapped_without_its_text() -> None:
    provider = _identity_provider(redis_exceptions.ConnectionError(SECRET))
    with pytest.raises(Unavailable) as caught:
        await provider.isolation_identity()
    assert SECRET not in str(caught.value)


async def test_an_error_that_is_not_a_backend_failure_is_not_mapped_in_identity() -> (
    None
):
    provider = _identity_provider(ValueError("a bug, not the backend"))
    with pytest.raises(ValueError, match="a bug"):
        await provider.isolation_identity()


async def test_a_reply_that_is_not_a_backend_failure_is_not_mapped() -> None:
    transport = _transport(ValueError("a bug of the caller, not of the backend"))
    with pytest.raises(ValueError, match="a bug of the caller"):
        await transport.ack(DELIVERY)


async def test_a_missing_stream_is_still_empty_not_an_error() -> None:
    transport = _transport(redis_exceptions.ResponseError("ERR no such key"))
    assert await transport.group_info("missing") == []
    nogroup = redis_exceptions.ResponseError(
        "NOGROUP No such key 'missing' or consumer group 'g' in XPENDING"
    )
    wrong_type = redis_exceptions.ResponseError(
        "WRONGTYPE Operation against a key holding the wrong kind of value"
    )
    stats = _transport_of(_ScriptedClient(xpending=nogroup, xlen=wrong_type))
    assert await stats.stats("missing", "g") == {"pending": 0, "dlq": 0}
    info = _transport_of(_ScriptedClient(xlen=wrong_type))
    assert (await info.stream_info("not-a-stream")).length == 0


async def test_a_store_without_the_command_reads_as_nothing_there() -> None:
    unknown = redis_exceptions.ResponseError("unknown command 'XINFO'")
    assert (
        await _transport_of(_ScriptedClient(xinfo_groups=unknown)).group_info("s") == []
    )
    pending = _transport_of(
        _ScriptedClient(
            xpending_range=redis_exceptions.ResponseError("unknown command 'XPENDING'")
        )
    )
    assert await pending.consume("s", group="g", consumer="c") == []


async def test_a_group_lost_with_no_known_position_still_recreates_at_the_tail() -> (
    None
):
    # WRONGTYPE on the inventory hash is "no heartbeated position": the same
    # answer as a missing record, so the recovery goes on at '$'.
    wrong_type = redis_exceptions.ResponseError("WRONGTYPE Operation against a key")
    client = _ScriptedClient(xreadgroup=_nogroup_once(), hget=wrong_type)
    transport = _transport_of(client)
    assert await transport.consume("s", group="g", consumer="c") == []
    creates = [
        kwargs["id"] for name, _a, kwargs in client.calls if name == "xgroup_create"
    ]
    assert creates == ["0", "$"]


#: The replies of a server in trouble. None of them says "nothing is there":
#: each must surface as a mapped error from every read that has a narrowed
#: absence check, never turn into an empty answer or a recovery decision.
TROUBLE = [
    "MASTERDOWN Link with MASTER is down and replica-serve-stale-data is set to no",
    "BUSY Redis is busy running a script",
    "CLUSTERDOWN The cluster is down",
    "MOVED 3999 127.0.0.1:6381",
    "ASK 3999 127.0.0.1:6381",
    "NOREPLICAS Not enough good replicas to write",
    "MISCONF Redis is configured to save RDB snapshots",
    "TRYAGAIN Multiple keys request during rehashing of slot",
    "LOADING Redis is loading the dataset in memory",
    "READONLY You can't write against a read only replica",
    "OOM command not allowed when used memory > 'maxmemory'",
    "EXECABORT Transaction discarded because of previous errors",
    "NOSCRIPT No matching script",
    "ERR Error running script",
]


def _stale_position(transport: RedisStreamsTransport, envelope: str | None) -> None:
    transport._last_delivered[("s", "g")] = ("5-0", envelope)


#: One narrowed read each: the command that fails, what else the scenario
#: needs, and the public call that reaches it. A recovery case also counts the
#: ``XGROUP CREATE`` calls: only ``ensure_group``'s one may happen.
ONE_COMMAND: dict[
    str,
    tuple[
        str,
        Callable[[], dict[str, Any]],
        Callable[[RedisStreamsTransport], None],
        Callable[[RedisStreamsTransport], Awaitable[Any]],
    ],
] = {
    "ensure_group reads the groups": (
        "xinfo_groups",
        dict,
        lambda t: None,
        lambda t: t.ensure_group("s", "g"),
    ),
    "group_info reads the groups": (
        "xinfo_groups",
        dict,
        lambda t: None,
        lambda t: t.group_info("s"),
    ),
    "stream_info reads the length": (
        "xlen",
        dict,
        lambda t: None,
        lambda t: t.stream_info("s"),
    ),
    "stats reads the pending summary": (
        "xpending",
        dict,
        lambda t: None,
        lambda t: t.stats("s", "g"),
    ),
    "stats reads the dlq length": (
        "xlen",
        dict,
        lambda t: None,
        lambda t: t.stats("s", "g"),
    ),
    "consume scans the pending entries": (
        "xpending_range",
        dict,
        lambda t: None,
        lambda t: t.consume("s", group="g", consumer="c"),
    ),
    "a lost group reads the inventory position": (
        "hget",
        lambda: {"xreadgroup": _nogroup_once()},
        lambda t: None,
        lambda t: t.consume("s", group="g", consumer="c"),
    ),
    "a lost group asks whether the stream was reset": (
        "xrevrange",
        lambda: {"xreadgroup": _nogroup_once()},
        lambda t: _stale_position(t, None),
        lambda t: t.consume("s", group="g", consumer="c"),
    ),
    "a lost group fetches the entry at its position": (
        "xrange",
        lambda: {"xreadgroup": _nogroup_once(), "xrevrange": [("9-0", {})]},
        lambda t: _stale_position(t, "e1"),
        lambda t: t.consume("s", group="g", consumer="c"),
    ),
}


@pytest.mark.parametrize("reply", TROUBLE, ids=lambda reply: reply.split()[0])
@pytest.mark.parametrize("case", sorted(ONE_COMMAND))
async def test_a_server_in_trouble_fails_one_command_and_surfaces(
    case: str, reply: str
) -> None:
    command, replies, setup, call = ONE_COMMAND[case]
    client = _ScriptedClient(
        **replies(), **{command: redis_exceptions.ResponseError(reply)}
    )
    transport = _transport_of(client)
    setup(transport)
    with pytest.raises(InfraError) as caught:
        await call(transport)
    assert caught.value.capability == "eventbus"
    assert client.count(command) >= 1  # the scenario reached the failing command
    # Not read as "no position / no group": the group was NOT recreated here.
    assert client.count("xgroup_create") <= 1


async def test_a_busy_hget_does_not_recreate_the_group_at_the_tail() -> None:
    # The inventory position is unknown only when the record is absent. A BUSY
    # (or MASTERDOWN) reply must not read as "unknown" and recreate the group
    # at '$', which would skip every entry published while it was missing.
    for reply in ("BUSY Redis is busy running a script", "MASTERDOWN Link is down"):
        client = _ScriptedClient(
            xreadgroup=_nogroup_once(),
            hget=redis_exceptions.ResponseError(reply),
        )
        transport = _transport_of(client)
        with pytest.raises(Unavailable):
            await transport.consume("s", group="g", consumer="c")
        creates = [kw["id"] for name, _a, kw in client.calls if name == "xgroup_create"]
        assert "$" not in creates


async def test_an_exhausted_entry_keeps_its_payload_when_a_second_read_would_fail() -> (
    None
):
    # The entry's data comes from the one read that also checked its target:
    # a second read (which could fail or lose the entry) parked it as
    # SOURCE_MISSING without data while the source still existed.
    frames = [("1-0", {"data": '{"id": "e1"}'})]
    reads: list[None] = []

    def xrange(*_args: Any, **_kwargs: Any) -> list[Any]:
        if reads:
            raise redis_exceptions.ResponseError("MASTERDOWN Link is down")
        reads.append(None)
        return frames

    client = _ScriptedClient(xrange=xrange)
    transport = _transport_of(client)
    entry = _PendingEntry(msg_id="1-0", consumer="c", idle_ms=0, times=5)
    await transport._dead_letter_exhausted(client, "s", "s", "g", entry)
    assert client.count("xrange") == 1
    (_name, args, _kwargs) = next(c for c in client.calls if c[0] == "xadd")
    assert args[0] == "s.dlq"
    assert args[1]["data"] == '{"id": "e1"}'
    assert args[1]["reason"] == DLQ_REASON_MAX_DELIVERIES
    assert client.count("xack") == 1


async def test_an_exhausted_entry_whose_source_is_gone_is_parked_without_data() -> None:
    client = _ScriptedClient(xrange=[])
    transport = _transport_of(client)
    entry = _PendingEntry(msg_id="1-0", consumer="c", idle_ms=0, times=5)
    await transport._dead_letter_exhausted(client, "s", "s", "g", entry)
    (_name, args, _kwargs) = next(c for c in client.calls if c[0] == "xadd")
    assert "data" not in args[1]
    assert args[1]["reason"] == DLQ_REASON_SOURCE_MISSING


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (
            redis_exceptions.ConnectionError(
                "Error 111 connecting to localhost:6379. Connection refused."
            ),
            "cannot reach Redis",
        ),
        (
            redis_exceptions.TimeoutError("Timeout connecting to server"),
            "cannot reach Redis",
        ),
        (redis_exceptions.AuthenticationError("invalid password"), "refused the login"),
    ],
    ids=["Unavailable", "Timeout", "PermissionDenied"],
)
async def test_a_mapped_streams_failure_reads_as_unreachable_for_the_cli(
    raw: BaseException, fragment: str
) -> None:
    # The CLI prints one SANDLOOM-R014 line instead of a traceback only when
    # ``connection_failure`` walks into the mapped error's cause.
    transport = _transport(raw)
    with pytest.raises(InfraError) as caught:
        await transport.ack(DELIVERY)
    line = connection_failure(caught.value)
    assert line is not None
    assert fragment in line
    assert CODE in line


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
