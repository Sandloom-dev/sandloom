"""The eviction check of the Redis drivers does not wait for ``aopen``.

Only the API lifespan runs ``aopen_all``: a worker or a CLI process writes
revocation marks, blocks and idempotency reservations without ever opening the
instance. So the first operation judges ``maxmemory-policy`` itself, and a
``noeviction`` lost at runtime (a failed ``health`` probe, a lost connection)
refuses the next operation. A scripted client stands in for the server: no
server configuration is changed, and the tests need no Redis.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import redis.exceptions as redis_errors
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
