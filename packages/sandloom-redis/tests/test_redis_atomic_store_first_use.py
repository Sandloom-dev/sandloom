"""The eviction check of the Redis drivers does not wait for ``aopen``.

Only the API lifespan runs ``aopen_all``: a worker or a CLI process writes
revocation marks, blocks and idempotency reservations without ever opening the
instance. So the first operation judges ``maxmemory-policy`` itself, and a
``noeviction`` lost at runtime (a failed ``health`` probe, a lost connection)
refuses the next operation. A scripted client stands in for the server: no
server configuration is changed, and the tests need no Redis.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import redis.exceptions as redis_errors
from sandloom_redis._eviction_guard import refusal_of
from sandloom_redis.atomic_store import RedisAtomicStoreProvider
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.driverkit import InvalidArgument, PermissionDenied, Unavailable

_URL = "redis://127.0.0.1:1/0"  # never dialled: the client is the scripted one


class _Server:
    """What a Redis answers: ``policy`` is ``CONFIG GET maxmemory-policy``
    (``None``: refused); ``calls`` lists every command received."""

    def __init__(self, policy: str | None) -> None:
        self.policy = policy
        self.calls: list[str] = []
        self.down = False

    def _record(self, command: str) -> None:
        self.calls.append(command)
        if self.down:
            raise redis_errors.ConnectionError("down")

    @property
    def reads(self) -> int:
        return self.calls.count("config_get")

    @property
    def data_calls(self) -> list[str]:
        return [c for c in self.calls if c not in ("config_get", "ping")]

    async def config_get(self, name: str) -> dict[str, str]:
        self._record("config_get")
        if self.policy is None:
            raise redis_errors.NoPermissionError("NOPERM CONFIG")
        return {name: self.policy}

    async def ping(self) -> bool:
        self._record("ping")
        return True

    async def get(self, key: str) -> None:
        self._record("get")

    async def set(self, key: str, value: str, **_: object) -> bool:
        self._record("set")
        return True

    async def delete(self, key: str) -> int:
        self._record("delete")
        return 1

    async def evalsha(self, *_: object) -> list[object]:
        self._record("evalsha")
        return [1, None]

    async def eval(self, *_: object) -> int:
        self._record("eval")
        return 1

    async def aclose(self, **_: object) -> None:
        return None


def _store(
    policy: str | None, *, allowed: bool | None = None, **options: Any
) -> tuple[RedisAtomicStoreProvider, _Server]:
    server = _Server(policy)
    provider = RedisAtomicStoreProvider(
        name="security",
        driver="redis",
        options={"url": _URL, **options},
        assume_allowed=None if allowed is None else (lambda: allowed),
        client_factory=lambda: server,
    )
    return provider, server


def _lease(policy: str | None) -> tuple[RedisLeaseProvider, _Server]:
    server = _Server(policy)
    provider = RedisLeaseProvider(
        name="beat",
        driver="redis",
        options={"url": _URL},
        client_factory=lambda: server,
    )
    return provider, server


def _operations(store: RedisAtomicStoreProvider) -> list[Callable[[], Any]]:
    return [
        lambda: store.get("k"),
        lambda: store.set("k", "v"),
        lambda: store.delete("k"),
        lambda: store.add("k", "v"),
        lambda: store.add_or_get("k", "v"),
        lambda: store.compare_and_set("k", "a", "b"),
        lambda: store.compare_and_delete("k", "a"),
        lambda: store.set_subject_state("k", "v1:after:1"),
    ]


@pytest.mark.parametrize("index", range(8))
async def test_a_store_that_never_had_aopen_refuses_on_an_evicting_server(
    index: int,
) -> None:
    store, server = _store("allkeys-lru")
    with pytest.raises(InvalidArgument, match="noeviction"):
        await _operations(store)[index]()
    assert server.data_calls == []  # nothing was written to the evicting server


async def test_the_first_operation_of_a_never_opened_store_checks_once() -> None:
    store, server = _store("noeviction")
    assert await store.add("k", "v", ttl_seconds=10) is True
    await store.get("k")
    assert server.reads == 1  # judged on the first operation, not on every one
    assert server.data_calls == ["set", "get"]


async def test_an_unreadable_policy_is_refused_on_first_use_under_verify() -> None:
    store, server = _store(None)
    with pytest.raises(PermissionDenied, match="never_evicts_check"):
        await store.set("k", "v")
    assert server.data_calls == []


async def test_assume_vouches_for_an_unreadable_policy_on_first_use() -> None:
    store, server = _store(None, allowed=True, never_evicts_check="assume")
    await store.set("k", "v")
    assert server.data_calls == ["set"]


@pytest.mark.parametrize("allowed", [None, False])
async def test_assume_without_the_accepted_risk_is_refused_on_first_use(
    allowed: bool | None,
) -> None:
    store, server = _store(None, allowed=allowed, never_evicts_check="assume")
    with pytest.raises(PermissionDenied, match="unverified_noeviction"):
        await store.set("k", "v")
    assert server.calls == []  # refused before the server is asked


async def test_a_refused_first_use_is_judged_again_once_the_policy_is_fixed() -> None:
    store, server = _store("allkeys-lru")
    with pytest.raises(InvalidArgument):
        await store.get("k")
    server.policy = "noeviction"
    assert await store.get("k") is None
    assert server.data_calls == ["get"]


async def test_a_failed_check_does_not_pass_the_next_operation() -> None:
    """A connection failure while reading the policy is ``Unavailable``, and the
    store does not count it as a passed check."""
    store, server = _store("allkeys-lru")
    server.down = True
    with pytest.raises(Unavailable):
        await store.set("k", "v")
    server.down = False
    with pytest.raises(InvalidArgument, match="noeviction"):
        await store.set("k", "v")
    assert server.data_calls == []


async def test_a_noeviction_lost_at_runtime_refuses_the_next_operation() -> None:
    store, server = _store("noeviction")
    await store.aopen()
    await store.set("k", "v")
    server.policy = "allkeys-lru"
    # Before a probe the check stays passed (one read, no per-operation cost).
    await store.set("k", "v")
    assert (await store.health()).status == "fail"
    with pytest.raises(InvalidArgument, match="noeviction"):
        await store.set("k", "v")
    server.policy = "noeviction"
    assert (await store.health()).status == "ok"
    await store.set("k", "v")
    assert server.data_calls == ["set", "set", "set"]


async def test_a_lost_connection_makes_the_next_operation_read_the_policy() -> None:
    store, server = _store("noeviction")
    await store.set("k", "v")
    assert server.reads == 1
    server.down = True
    with pytest.raises(Unavailable):
        await store.set("k", "v")
    server.down = False
    server.policy = "allkeys-lru"  # the server came back reconfigured
    with pytest.raises(InvalidArgument, match="noeviction"):
        await store.set("k", "v")


async def test_the_lease_refuses_after_health_finds_the_policy_lost() -> None:
    provider, server = _lease("noeviction")
    await provider.aopen()
    assert await provider.acquire("n", ttl_seconds=30, owner="o") is not None
    server.policy = "allkeys-lru"
    health = await provider.health()
    assert health.status == "fail"
    assert health.details == {"reason": "evicting_policy"}
    calls = len(server.calls)
    with pytest.raises(InvalidArgument, match="noeviction"):
        await provider.acquire("n", ttl_seconds=30, owner="o")
    assert "eval" not in server.calls[calls:]  # no fence was drawn
    server.policy = "noeviction"
    assert await provider.acquire("n", ttl_seconds=30, owner="o") is not None


async def test_a_health_probe_that_fails_on_the_connection_reopens_the_check() -> None:
    """The probe itself failing (the retrying client may have reconnected
    silently before) is a reason to judge the policy again, whatever it was."""
    store, server = _store("noeviction")
    await store.aopen()
    server.down = True
    assert (await store.health()).status == "fail"
    server.down = False
    server.policy = "allkeys-lru"  # the server came back reconfigured
    with pytest.raises(InvalidArgument, match="noeviction"):
        await store.set("k", "v")
    assert server.data_calls == []


class _Connection:
    """A pooled connection that never dials: it records what the pool's connect
    function sends and answers ``CONFIG GET`` with ``reply`` (an exception
    instance is raised, as redis-py raises an error reply)."""

    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.sent: list[tuple[object, ...]] = []

    async def on_connect(self) -> None:
        self.sent.append(("on_connect",))

    async def send_command(self, *args: object) -> None:
        self.sent.append(args)

    async def read_response(self) -> object:
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def _answer(policy: str) -> list[str]:
    return ["maxmemory-policy", policy]  # a RESP2 CONFIG GET reply


_CONFIG_GET = ("CONFIG GET", "maxmemory-policy")


async def _pool_connect_func(
    provider: RedisAtomicStoreProvider | RedisLeaseProvider, *, retrying: bool = True
) -> Any:
    """The ``redis_connect_func`` the provider put on the pool of its client."""
    if isinstance(provider, RedisAtomicStoreProvider):
        client = await provider._client_of(retrying=retrying)
    else:
        client = await provider._redis()
    try:
        return client.connection_pool.connection_kwargs["redis_connect_func"]
    finally:
        await client.aclose(close_connection_pool=True)


def _url_store(**options: Any) -> RedisAtomicStoreProvider:
    return RedisAtomicStoreProvider(
        name="security",
        driver="redis",
        options={"url": _URL, **options},
        assume_allowed=lambda: True,
    )


def _url_lease(**options: Any) -> RedisLeaseProvider:
    return RedisLeaseProvider(
        name="beat",
        driver="redis",
        options={"url": _URL, **options},
        assume_allowed=lambda: True,
    )


@pytest.mark.parametrize("kind", ["store-retrying", "store-once", "lease"])
async def test_every_connect_of_a_pool_is_judged_on_that_connection(kind: str) -> None:
    """The first connect and each reconnect of the same socket: `on_connect`
    (auth, SELECT) first, then CONFIG GET on the connection itself, before any
    command. A dropped socket dialled again is judged again."""
    if kind == "lease":
        provider: RedisAtomicStoreProvider | RedisLeaseProvider = _url_lease()
    else:
        provider = _url_store()
    connect = await _pool_connect_func(provider, retrying=kind != "store-once")
    connection = _Connection(_answer("noeviction"))
    await connect(connection)
    await connect(connection)  # the same socket, dialled again after a drop
    assert connection.sent == [("on_connect",), _CONFIG_GET] * 2


@pytest.mark.parametrize("kind", ["store", "lease"])
@pytest.mark.parametrize(
    "reply", [_answer("allkeys-lru"), {"maxmemory-policy": "allkeys-lru"}]
)
async def test_a_connection_to_an_evicting_server_is_refused_before_any_command(
    kind: str, reply: object
) -> None:
    """RESP2 list and RESP3 mapping alike. The check does not read the flag:
    one that a racing task set to True cannot wave a socket through."""
    provider: RedisAtomicStoreProvider | RedisLeaseProvider = (
        _url_store() if kind == "store" else _url_lease()
    )
    connect = await _pool_connect_func(provider)
    provider._policy_checked = True
    connection = _Connection(reply)
    with pytest.raises(redis_errors.ConnectionError) as caught:
        await connect(connection)
    refusal = refusal_of(caught.value)
    assert isinstance(refusal, InvalidArgument)
    assert "allkeys-lru" in str(refusal)
    assert connection.sent == [("on_connect",), _CONFIG_GET]  # nothing else sent


async def test_a_refused_read_follows_never_evicts_check_on_every_connect() -> None:
    refused = redis_errors.NoPermissionError("NOPERM CONFIG")
    verify = await _pool_connect_func(_url_store(never_evicts_check="verify"))
    with pytest.raises(redis_errors.ConnectionError) as caught:
        await verify(_Connection(refused))
    assert isinstance(refusal_of(caught.value), PermissionDenied)
    assume = await _pool_connect_func(_url_store(never_evicts_check="assume"))
    await assume(_Connection(refused))  # vouched for: the socket is used
    lease_verify = await _pool_connect_func(_url_lease())
    with pytest.raises(redis_errors.ConnectionError) as caught:
        await lease_verify(_Connection(refused))
    assert isinstance(refusal_of(caught.value), PermissionDenied)


async def test_assume_without_the_accepted_risk_refuses_every_connect() -> None:
    store = RedisAtomicStoreProvider(
        name="security",
        driver="redis",
        options={"url": _URL, "never_evicts_check": "assume"},
    )
    connect = await _pool_connect_func(store)
    with pytest.raises(redis_errors.ConnectionError) as caught:
        await connect(_Connection(_answer("noeviction")))
    refusal = refusal_of(caught.value)
    assert isinstance(refusal, PermissionDenied)
    assert "unverified_noeviction" in str(refusal)


@pytest.mark.parametrize(
    "failure",
    [
        redis_errors.ResponseError("LOADING Redis is loading the dataset"),
        redis_errors.ConnectionError("reset"),
        redis_errors.TimeoutError("slow"),
    ],
)
async def test_a_failed_read_of_the_policy_is_no_refusal(failure: Exception) -> None:
    """An outage while reading the policy is not a verdict: it propagates as
    itself (and maps to Unavailable / Timeout), never as a standing refusal."""
    connect = await _pool_connect_func(_url_store())
    with pytest.raises(type(failure)) as caught:
        await connect(_Connection(failure))
    assert caught.value is failure
    assert refusal_of(caught.value) is None


async def test_a_refusal_of_a_pooled_connection_is_the_operations_error() -> None:
    """The carried refusal, not ``Unavailable``, is what the operation raises,
    and it opens the check; any other connection error stays ``Unavailable``."""
    store, server = _store("noeviction")
    connect = await _pool_connect_func(_url_store())
    with pytest.raises(redis_errors.ConnectionError) as caught:
        await connect(_Connection(_answer("volatile-ttl")))
    refused = caught.value

    async def refuse(*_: object, **__: object) -> bool:
        raise refused

    await store.set("k", "v")  # judged and passed
    server.set = refuse  # type: ignore[method-assign]
    with pytest.raises(InvalidArgument, match="volatile-ttl"):
        await store.set("k", "v")
    assert store._policy_checked is False

    async def down(*_: object, **__: object) -> bool:
        raise redis_errors.ConnectionError("down")

    server.set = down  # type: ignore[method-assign]
    with pytest.raises(Unavailable):
        await store.set("k", "v")


async def test_a_refusal_of_a_pooled_connection_is_the_leases_error() -> None:
    provider, server = _lease("noeviction")
    connect = await _pool_connect_func(_url_lease())
    with pytest.raises(redis_errors.ConnectionError) as caught:
        await connect(_Connection(_answer("allkeys-lfu")))
    refused = caught.value
    await provider.aopen()

    async def refuse(*_: object) -> int:
        raise refused

    server.eval = refuse  # type: ignore[method-assign]
    with pytest.raises(InvalidArgument, match="allkeys-lfu"):
        await provider.acquire("n", ttl_seconds=30, owner="o")
    assert provider._policy_checked is False


async def test_a_health_probe_refused_by_the_pool_reports_the_reason() -> None:
    store, server = _store("noeviction")
    connect = await _pool_connect_func(_url_store())
    with pytest.raises(redis_errors.ConnectionError) as caught:
        await connect(_Connection(_answer("allkeys-random")))
    refused = caught.value

    async def refuse() -> bool:
        raise refused

    server.ping = refuse  # type: ignore[method-assign]
    await store.aopen()
    health = await store.health()
    assert (health.status, health.details) == ("fail", {"reason": "evicting_policy"})
    assert store._policy_checked is False


def test_a_client_rebuilt_for_another_loop_reopens_the_check() -> None:
    """A loop change rebuilds both clients: the new pool judges each of its
    sockets, and the flag opens so the next operation reads the policy too."""
    store = _url_store()
    clients: list[Any] = []

    async def first_loop() -> None:
        clients.append(await store._client_of(retrying=True))
        store._policy_checked = True

    async def second_loop() -> None:
        clients.append(await store._client_of(retrying=True))
        await clients[1].aclose(close_connection_pool=True)

    asyncio.run(first_loop())
    asyncio.run(second_loop())
    assert clients[0] is not clients[1]
    assert store._policy_checked is False
    assert clients[1].connection_pool.connection_kwargs["redis_connect_func"]
