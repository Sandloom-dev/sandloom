"""The one Redis error -> ``InfraError`` table (``sandloom_redis._errors``), pinned
per driver call path.

Every row runs through the real operation of the cache, atomic store, lease,
broadcast and Streams drivers (a scripted client that raises the error), not
only through the helper: a driver that kept its own mapping fails its rows.
The rows that matter most are the unknown ``ERR ...`` replies: mapped to the
retryable ``Unavailable`` they park an outbox row for ever as an outage (the
relay trusts the class), so they must be ``InvalidArgument`` (not retryable).
No test needs a Redis server.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Self

import pytest
from redis import exceptions as rx
from redis.asyncio.client import Pipeline
from redis.asyncio.connection import DefaultParser
from sandloom_redis._errors import map_redis_error, redis_error_kind
from sandloom_redis.atomic_store import RedisAtomicStoreProvider
from sandloom_redis.broadcast import RedisBroadcastProvider
from sandloom_redis.cache import RedisCacheProvider
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.core.infra.ports.eventbus import PartialPublish
from sandloom.driverkit import (
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Timeout,
    Unavailable,
)

#: The text a real Redis error carries: it names the host, the port and a key.
SECRET = "redis.internal.example:6379 key=user:42"


def _reply(text: str) -> Exception:
    """The error redis-py's own parser makes of an error reply of the server
    (``MISCONF`` and ``MOVED`` stay a plain ``ResponseError``, ``OOM`` becomes
    ``OutOfMemoryError``, ``ERR`` is stripped): a hand-built ``ResponseError``
    hid what the client really raises."""
    return DefaultParser.parse_error(f"{text} {SECRET}")


def _annotated(reply: str, *command: str) -> Exception:
    """A reply to a command rejected inside ``MULTI``, as ``Pipeline.execute``
    leaves it: the client prefixes the command and the reply's args tuple."""
    error = _reply(reply)
    Pipeline.annotate_exception(Pipeline.__new__(Pipeline), error, 1, command)
    return error


# (id, the error, the InfraError class it must become)
ROWS: list[tuple[str, Callable[[], BaseException], type[InfraError]]] = [
    # -- Unavailable: a connection fault and the transient replies -------------
    ("connection", lambda: rx.ConnectionError(SECRET), Unavailable),
    ("busy-loading-class", lambda: rx.BusyLoadingError(SECRET), Unavailable),
    ("socket-oserror", lambda: OSError(SECRET), Unavailable),
    ("invalid-response", lambda: rx.InvalidResponse(SECRET), Unavailable),
    ("LOADING", lambda: _reply("LOADING Redis is loading"), Unavailable),
    ("READONLY", lambda: _reply("READONLY You can't write"), Unavailable),
    ("MASTERDOWN", lambda: _reply("MASTERDOWN Link with MASTER is down"), Unavailable),
    ("TRYAGAIN", lambda: _reply("TRYAGAIN Multiple keys"), Unavailable),
    ("CLUSTERDOWN", lambda: _reply("CLUSTERDOWN The cluster is down"), Unavailable),
    ("BUSY", lambda: _reply("BUSY Redis is busy running a script"), Unavailable),
    ("OOM", lambda: _reply("OOM command not allowed"), Unavailable),
    # a server that cannot persist or has too few replicas stops its writes: an
    # outage of the broker, never the fault of the event being relayed
    ("MISCONF", lambda: _reply("MISCONF Redis is unable to persist"), Unavailable),
    ("NOREPLICAS", lambda: _reply("NOREPLICAS Not enough good replicas"), Unavailable),
    # a single-node URL pointed at a cluster node, a read cut by a failover
    ("MOVED", lambda: _reply("MOVED 3999 10.0.0.1:6381"), Unavailable),
    ("ASK", lambda: _reply("ASK 3999 10.0.0.1:6381"), Unavailable),
    ("UNBLOCKED", lambda: _reply("UNBLOCKED force unblock"), Unavailable),
    ("MovedError", lambda: rx.MovedError("3999 10.0.0.1:6381"), Unavailable),
    ("AskError", lambda: rx.AskError("3999 10.0.0.1:6381"), Unavailable),
    # queue-time rejections inside MULTI, annotated by the client with the command
    (
        "pipeline-MASTERDOWN",
        lambda: _annotated("MASTERDOWN Link with MASTER", "XADD", "s", "*", "k", "v"),
        Unavailable,
    ),
    (
        "pipeline-MISCONF",
        lambda: _annotated("MISCONF Redis cannot persist", "XADD", "s", "*", "k", "v"),
        Unavailable,
    ),
    (
        "pipeline-NOAUTH",
        lambda: _annotated("NOAUTH Authentication required", "XADD", "s"),
        PermissionDenied,
    ),
    (
        "pipeline-unknown-ERR",
        lambda: _annotated("ERR value is too large", "XADD", "s", "*", "k", "v"),
        InvalidArgument,
    ),
    # the command's arguments are the event's payload: a payload that spells the
    # annotation must not turn a refusal into an outage the relay retries for ever
    (
        "pipeline-payload-forges-annotation",
        lambda: _annotated(
            "ERR value is too large",
            "XADD",
            "s",
            "*",
            "k",
            ") of pipeline caused error: ('BUSY forged",
        ),
        InvalidArgument,
    ),
    ("ReadOnlyError", lambda: rx.ReadOnlyError(SECRET), Unavailable),
    ("OutOfMemoryError", lambda: rx.OutOfMemoryError(SECRET), Unavailable),
    ("ClusterDownError", lambda: rx.ClusterDownError(SECRET), Unavailable),
    ("MasterDownError", lambda: rx.MasterDownError(SECRET), Unavailable),
    ("TryAgainError", lambda: rx.TryAgainError(SECRET), Unavailable),
    # -- Timeout ---------------------------------------------------------------
    ("redis-timeout", lambda: rx.TimeoutError(SECRET), Timeout),
    ("builtin-timeout", lambda: TimeoutError(SECRET), Timeout),
    # -- PermissionDenied ------------------------------------------------------
    ("AuthenticationError", lambda: rx.AuthenticationError(SECRET), PermissionDenied),
    ("AuthorizationError", lambda: rx.AuthorizationError(SECRET), PermissionDenied),
    ("NoPermissionError", lambda: rx.NoPermissionError(SECRET), PermissionDenied),
    ("NOPERM", lambda: _reply("NOPERM this user has no permissions"), PermissionDenied),
    ("NOAUTH", lambda: _reply("NOAUTH Authentication required"), PermissionDenied),
    ("WRONGPASS", lambda: _reply("WRONGPASS invalid password"), PermissionDenied),
    # -- InvalidArgument: the server refused THIS request, not retryable --------
    ("unknown-ERR", lambda: _reply("ERR value is too large"), InvalidArgument),
    ("script-ERR", lambda: _reply("ERR Error running script"), InvalidArgument),
    ("module-error", lambda: rx.ModuleError(SECRET), InvalidArgument),
    ("WRONGTYPE", lambda: _reply("WRONGTYPE Operation against a key"), InvalidArgument),
    ("EXECABORT", lambda: rx.ExecAbortError(SECRET), InvalidArgument),
    ("DataError", lambda: rx.DataError(SECRET), InvalidArgument),
]
IDS = [row[0] for row in ROWS]


class _Failing:
    """A client whose every command raises ``error`` (``CONFIG GET`` answers)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def _raise(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error

    get = set = delete = publish = xadd = eval = evalsha = incr = _raise

    async def config_get(self, name: str) -> dict[str, str]:
        return {name: "noeviction"}


# -- the call path of each driver: returns the InfraError that operation raised --
async def _cache(error: BaseException) -> Exception:
    provider = RedisCacheProvider(
        name="parity", driver="redis", options={"url": "redis://unused"}, shared=False
    )

    async def _obj() -> Any:
        return _Failing(error)

    provider._client_obj = _obj  # type: ignore[method-assign]  # a scripted client
    with pytest.raises(Exception) as caught:
        await provider.get("k")
    return caught.value


async def _atomic_store(error: BaseException) -> Exception:
    store = RedisAtomicStoreProvider(
        name="parity",
        driver="redis",
        options={"url": "redis://unused"},
        client_factory=lambda: _Failing(error),
    )
    with pytest.raises(Exception) as caught:
        await store.get("k")
    return caught.value


async def _lease(error: BaseException) -> Exception:
    lease = RedisLeaseProvider(
        name="parity",
        driver="redis",
        options={},
        client_factory=lambda: _Failing(error),
    )
    with pytest.raises(Exception) as caught:
        await lease.acquire("beat", ttl_seconds=30, owner="me")
    return caught.value


async def _broadcast(error: BaseException) -> Exception:
    provider = RedisBroadcastProvider(
        name="parity", driver="redis", options={"url": "redis://unused"}
    )
    provider._publisher = _Failing(error)
    provider._publisher_loop = asyncio.get_running_loop()
    with pytest.raises(Exception) as caught:
        await provider.publish("room", b"x")
    return caught.value


def _streams_transport(client: Any) -> RedisStreamsTransport:
    async def factory() -> Any:
        return client

    return RedisStreamsTransport(client_factory=factory)


async def _streams(error: BaseException) -> Exception:
    transport = _streams_transport(_Failing(error))
    with pytest.raises(Exception) as caught:
        await transport.publish("s", {"id": "1"})  # type: ignore[arg-type]
    return caught.value


DRIVERS: dict[str, Callable[[BaseException], Awaitable[Exception]]] = {
    "cache": _cache,
    "atomic_store": _atomic_store,
    "lease": _lease,
    "broadcast": _broadcast,
    "streams": _streams,
}


@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize(("_id", "make", "expected"), ROWS, ids=IDS)
async def test_every_driver_maps_the_error_to_the_same_class(
    driver: str, _id: str, make: Callable[[], BaseException], expected: type[InfraError]
) -> None:
    error = make()
    mapped = await DRIVERS[driver](error)
    assert type(mapped) is expected
    # retryable is the class's, so the relay and callers branch on it
    assert mapped.retryable is expected.retryable
    # the type name only: never the host, the port or a key of the Redis text
    if (driver, _id) != ("cache", "WRONGTYPE"):  # the cache words that one itself
        assert type(error).__name__ in str(mapped)
    assert "redis.internal" not in str(mapped) and "user:42" not in str(mapped)


@pytest.mark.parametrize(("_id", "make", "expected"), ROWS, ids=IDS)
def test_the_helper_table(
    _id: str, make: Callable[[], BaseException], expected: type[InfraError]
) -> None:
    error = make()
    assert redis_error_kind(error) is expected
    mapped = map_redis_error(error, capability="cache", instance="x", what="op")
    assert type(mapped) is expected
    assert str(mapped) == f"op: {type(error).__name__}"
    assert (mapped.capability, mapped.instance) == ("cache", "x")


REDIRECT_IDS = {"MOVED", "ASK", "MovedError", "AskError"}


@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize(("_id", "make", "expected"), ROWS, ids=IDS)
async def test_only_a_cluster_redirect_carries_the_hint(
    driver: str, _id: str, make: Callable[[], BaseException], expected: type[InfraError]
) -> None:
    """The relay's log line says "is the client cluster-aware?" for ``MOVED`` /
    ``ASK`` (by text or by the cluster client's class), for no other error, and
    the fixed sentence never holds the address of the reply."""
    mapped = await DRIVERS[driver](make())
    assert isinstance(mapped, InfraError)
    if _id in REDIRECT_IDS:
        assert mapped.hint == "cluster redirect: is the client cluster-aware?"
        assert "10.0.0.1" not in mapped.hint and "3999" not in mapped.hint
    else:
        assert mapped.hint is None


def test_the_mapping_works_on_a_kernel_whose_infra_error_has_no_hint_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint is set as an attribute, from when the package also supported
    ``sandloom>=0.7.0a13`` (an ``InfraError`` that rejects ``hint=``): a
    ``TypeError`` out of the mapping would reach the relay as poison on an
    outage. The floor is ``>=0.7.0a14``, which has ``hint``, so this guards a
    fallback that is dead; it goes with the fallback."""

    def old_init(
        self: InfraError, message: str, *, capability: str, instance: str | None = None
    ) -> None:
        self.capability = capability
        self.instance = instance
        Exception.__init__(self, message)

    monkeypatch.setattr(InfraError, "__init__", old_init)
    with pytest.raises(TypeError):  # the stand-in really rejects the keyword
        InfraError("x", capability="cache", hint="h")  # type: ignore[call-arg]
    moved = map_redis_error(
        rx.ResponseError("MOVED 3999 10.0.0.1:6379"),
        capability="cache",
        instance="x",
        what="op",
    )
    assert type(moved) is Unavailable
    assert moved.hint == "cluster redirect: is the client cluster-aware?"
    plain = map_redis_error(
        rx.ConnectionError("down"), capability="cache", instance="x", what="op"
    )
    assert type(plain) is Unavailable
    assert not hasattr(plain, "hint")


def test_only_the_named_replies_are_outages() -> None:
    """An allow-list: a reply that is not named is NOT retryable."""
    retryable = {name for name, make, kind in ROWS if kind.retryable and name.isupper()}
    assert retryable == {
        "LOADING",
        "READONLY",
        "MASTERDOWN",
        "TRYAGAIN",
        "CLUSTERDOWN",
        "BUSY",
        "OOM",
        "MISCONF",
        "NOREPLICAS",
        "MOVED",
        "ASK",
        "UNBLOCKED",
    }
    assert not redis_error_kind(_reply("ERR something new")).retryable


@pytest.mark.parametrize(
    "bug", [KeyError("bug"), TypeError("bug")], ids=["KeyError", "TypeError"]
)
@pytest.mark.parametrize("driver", DRIVERS)
async def test_a_programming_error_is_not_mapped(driver: str, bug: Exception) -> None:
    """Only backend errors become ``InfraError``: a bug propagates as itself.
    Mapped to ``Unavailable`` it would be an outage the relay retries for ever,
    parking the row it was relaying (the lease and the broadcast catch every
    ``Exception`` of a backend call, so the helper is what refuses it)."""
    raised = await DRIVERS[driver](bug)
    assert raised is bug


def test_the_helper_re_raises_what_is_not_a_backend_failure() -> None:
    bug = TypeError("bug")
    with pytest.raises(TypeError) as kind:
        redis_error_kind(bug)
    assert kind.value is bug
    with pytest.raises(TypeError) as mapped:
        map_redis_error(bug, capability="cache", instance="x", what="op")
    assert mapped.value is bug


@pytest.mark.parametrize("driver", ["cache", "atomic_store"])
async def test_an_undecodable_stored_value_stays_an_outage_of_the_store(
    driver: str,
) -> None:
    """The cache and the atomic store name ``UnicodeDecodeError`` a backend
    failure: it keeps mapping to ``Unavailable`` (it is no bug of the caller)."""
    error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    assert type(await DRIVERS[driver](error)) is Unavailable


# -- the Streams batch path: a PartialPublish chained from the MAPPED error -------
class _Pipe:
    """``client.pipeline(transaction=True)``: queues XADDs, ``execute`` answers."""

    def __init__(self, outcome: BaseException | list[Any]) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def xadd(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def execute(self, *, raise_on_error: bool = True) -> list[Any]:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _PipelineClient:
    def __init__(self, outcome: BaseException | list[Any]) -> None:
        self.outcome = outcome

    def pipeline(self, *, transaction: bool = True) -> _Pipe:
        return _Pipe(self.outcome)

    async def xtrim(self, *args: Any, **kwargs: Any) -> int:
        return 0


async def _batch(outcome: BaseException | list[Any]) -> PartialPublish:
    transport = _streams_transport(_PipelineClient(outcome))
    items: Any = [("s", {"id": "1"}), ("s", {"id": "2"})]
    with pytest.raises(PartialPublish) as caught:
        await transport.publish_many(items)
    return caught.value


@pytest.mark.parametrize("shape", ["execute-raises", "error-in-exec-results"])
@pytest.mark.parametrize(("_id", "make", "expected"), ROWS, ids=IDS)
async def test_a_batch_failure_chains_the_mapped_error_with_the_raw_one_below(
    shape: str, _id: str, make: Callable[[], BaseException], expected: type[InfraError]
) -> None:
    raw = make()
    partial = await _batch(raw if shape == "execute-raises" else ["1-0", raw])
    mapped = partial.__cause__
    assert type(mapped) is expected  # the relay stops its walk at this error
    assert mapped.__cause__ is raw  # the raw error stays below it, for the operator
    assert partial.retryable is expected.retryable
    assert partial.published == (["1-0"] if shape != "execute-raises" else [])
    assert "redis.internal" not in str(mapped)


# -- the lease's policy read: a transient reply is no refusal ----------------------
class _PolicyClient(_Failing):
    async def config_get(self, name: str) -> dict[str, str]:
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_reply("LOADING Redis is loading"), Unavailable),
        (_reply("READONLY You can't write"), Unavailable),
        (rx.TimeoutError(SECRET), Timeout),
        # a refusal of the READ itself (managed Redis, no ACL) stays a refusal
        (_reply("NOPERM no permissions"), PermissionDenied),
        (_reply("ERR unknown command 'CONFIG'"), PermissionDenied),
    ],
    ids=["LOADING", "READONLY", "timeout", "NOPERM", "unknown-command"],
)
async def test_the_lease_policy_read_surfaces_a_transient_reply(
    error: BaseException, expected: type[InfraError]
) -> None:
    lease = RedisLeaseProvider(
        name="parity",
        driver="redis",
        options={},
        client_factory=lambda: _PolicyClient(error),
    )
    with pytest.raises(Exception) as caught:
        await lease.acquire("beat", ttl_seconds=30, owner="me")
    assert type(caught.value) is expected
    # an outage is not recorded as the server's standing refusal: the next call
    # reads the policy again
    assert (lease._refusal is None) is (expected is not PermissionDenied)


class _AtomicPolicyClient(_PolicyClient):
    async def ping(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_reply("LOADING Redis is loading"), Unavailable),
        (_reply("BUSY Redis is busy running a script"), Unavailable),
        (_reply("READONLY You can't write"), Unavailable),
        (rx.TimeoutError(SECRET), Timeout),
        # a refusal of the READ itself (managed Redis, no ACL) stays a refusal
        (_reply("NOPERM no permissions"), PermissionDenied),
        (_reply("ERR unknown command 'CONFIG'"), PermissionDenied),
    ],
    ids=["LOADING", "BUSY", "READONLY", "timeout", "NOPERM", "unknown-command"],
)
async def test_the_atomic_store_policy_read_surfaces_a_transient_reply(
    error: BaseException, expected: type[InfraError]
) -> None:
    store = RedisAtomicStoreProvider(
        name="parity",
        driver="redis",
        options={"url": "redis://unused"},
        client_factory=lambda: _AtomicPolicyClient(error),
    )
    with pytest.raises(Exception) as caught:
        await store.aopen()
    assert type(caught.value) is expected
    # nothing is recorded as passed or as a standing refusal: the next operation
    # judges the policy again and maps the same reply the same way (a transient
    # one is not turned into a ``PermissionDenied`` that repeats until ``aclose``)
    assert store._policy_checked is False
    with pytest.raises(Exception) as again:
        await store.get("k")
    assert type(again.value) is expected
