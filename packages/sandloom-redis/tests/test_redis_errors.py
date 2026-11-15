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


def _reply(text: str) -> rx.ResponseError:
    return rx.ResponseError(f"{text} {SECRET}")


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
    }
    assert not redis_error_kind(_reply("ERR something new")).retryable


@pytest.mark.parametrize("driver", ["cache", "atomic_store", "streams"])
async def test_a_programming_error_is_not_mapped(driver: str) -> None:
    """Only backend errors become ``InfraError``: a bug propagates as itself
    (the lease maps every ``Exception`` of a backend call, as before)."""
    raised = await DRIVERS[driver](KeyError("bug"))
    assert type(raised) is KeyError


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
