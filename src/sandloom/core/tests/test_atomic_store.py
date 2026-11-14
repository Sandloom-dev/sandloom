"""The ``atomic_store`` drivers (``memory``, ``redis``) against the kit, and what
the kit cannot know: the Redis eviction check, its operator override, the round
trips and the identity.

The Redis tests need the test Redis (``SANDLOOM_TEST_CACHE_URL``); the eviction
logic runs against a scripted client, so no server's configuration is changed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.memory_atomic_store import MemoryAtomicStoreProvider
from sandloom.core.drivers.redis_atomic_store import (
    RedisAtomicStoreProvider,
    make_redis_atomic_store_factory,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import (
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Unavailable,
)
from sandloom.core.infra.ports.atomic_store import AtomicStoreProvider
from sandloom.driverkit.testing import AtomicStoreConformance

_VALUE_LIMIT = 8192


class _Clock:
    """A monotonic clock a test moves forward."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def _memory(**options: Any) -> MemoryAtomicStoreProvider:
    options.setdefault("max_value_bytes", _VALUE_LIMIT)
    provider = MemoryAtomicStoreProvider.from_config(
        InstanceConfig("atomic_store", "default", "memory", options)
    )
    provider.clock = _Clock()
    return provider


class TestMemoryAtomicStore(AtomicStoreConformance):
    value_limit_bytes = _VALUE_LIMIT

    @pytest.fixture
    def make_provider(self) -> Callable[[], AtomicStoreProvider]:
        return _memory

    async def let_expire(self, provider: AtomicStoreProvider, ttl_seconds: int) -> None:
        clock = provider.clock  # type: ignore[attr-defined]
        assert isinstance(clock, _Clock)
        clock.offset += ttl_seconds + 1.1


async def test_the_memory_driver_declares_what_it_is() -> None:
    provider = _memory()
    assert provider.development_only is True
    assert provider.features() == {"never_evicts"}
    assert await provider.isolation_identity() == await _memory().isolation_identity()
    assert (await provider.isolation_identity() or "").startswith("memory:")


async def test_the_memory_driver_expires_a_key_in_real_time() -> None:
    provider = MemoryAtomicStoreProvider.from_config(
        InstanceConfig("atomic_store", "default", "memory", {})
    )
    await provider.set("k", "v", ttl_seconds=1)
    assert await provider.get("k") == "v"
    await asyncio.sleep(1.1)
    assert await provider.get("k") is None


# ------------------------------------------------------------------- redis
def _redis_options(**options: Any) -> dict[str, Any]:
    options.setdefault("url", settings.cache_url)
    options.setdefault("max_value_bytes", _VALUE_LIMIT)
    return options


def _redis(**options: Any) -> RedisAtomicStoreProvider:
    return RedisAtomicStoreProvider(
        name="default", driver="redis", options=_redis_options(**options)
    )


class _Refused(Exception):
    """Stands for the server refusing a command (``NOPERM``)."""


class _ScriptedClient:
    """The few calls the driver's lifecycle makes, with a scripted answer."""

    def __init__(self, policy: str | None) -> None:
        self.policy = policy  # ``None``: CONFIG GET is refused
        self.reads = 0
        self.closed = False

    async def config_get(self, name: str) -> dict[str, str]:
        from redis.exceptions import ResponseError

        self.reads += 1
        if self.policy is None:
            raise ResponseError("NOPERM this user has no permissions to run CONFIG")
        return {name: self.policy}

    async def ping(self) -> bool:
        return True

    async def info(self, section: str) -> dict[str, str]:
        return {"run_id": "scripted"}

    async def aclose(self) -> None:
        self.closed = True


def _scripted(
    policy: str | None, *, assume_allowed: bool | None = None, **options: Any
) -> tuple[RedisAtomicStoreProvider, _ScriptedClient]:
    client = _ScriptedClient(policy)
    allowed = None if assume_allowed is None else (lambda: assume_allowed)
    provider = RedisAtomicStoreProvider(
        name="default",
        driver="redis",
        options=_redis_options(**options),
        assume_allowed=allowed,
        client_factory=lambda: client,
    )
    return provider, client


class TestRedisAtomicStore(AtomicStoreConformance):
    value_limit_bytes = _VALUE_LIMIT

    @pytest.fixture
    def make_provider(self) -> Callable[[], AtomicStoreProvider]:
        return _redis

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], AtomicStoreProvider]:
        # Nothing listens on port 1; the "password" is the kit's secret.
        url = f"redis://:{self.leak_marker}@127.0.0.1:1/0"
        return lambda: _redis(url=url, retry_attempts=0)

    @pytest.fixture
    def make_evicting_provider(self) -> Callable[[], AtomicStoreProvider]:
        return lambda: _scripted("allkeys-lru")[0]


async def test_redis_declares_shared_and_never_evicts() -> None:
    provider = _redis()
    assert provider.features() == {"shared", "never_evicts"}
    assert provider.development_only is False
    assert set(provider.performance_hints) == {
        "get",
        "add_or_get",
        "set_subject_state",
        "compare_and_set",
    }


async def test_the_real_server_identity_is_its_run_id() -> None:
    provider = _redis()
    await provider.aopen()
    try:
        client = await provider._client_obj()
        info = await client.info(section="server")
        assert await provider.isolation_identity() == f"redis:{info['run_id']}"
    finally:
        await provider.aclose()


async def test_the_real_server_passes_the_eviction_check() -> None:
    provider = _redis()
    await provider.aopen()
    try:
        assert (await provider.health()).status == "ok"
    finally:
        await provider.aclose()


@pytest.mark.parametrize("mode", ["verify", "assume"])
@pytest.mark.parametrize("policy", ["allkeys-lru", "volatile-ttl", "allkeys-random"])
async def test_a_readable_evicting_policy_is_refused_in_both_modes(
    policy: str, mode: str
) -> None:
    provider, _ = _scripted(policy, assume_allowed=True, never_evicts_check=mode)
    with pytest.raises(InvalidArgument, match="noeviction"):
        await provider.aopen()
    await provider.aclose()


async def test_verify_refuses_an_unreadable_policy_and_names_the_option() -> None:
    provider, _ = _scripted(None)
    with pytest.raises(PermissionDenied, match="never_evicts_check") as caught:
        await provider.aopen()
    assert "assume" in str(caught.value)


async def test_assume_opens_on_an_unreadable_policy_and_health_keeps_trying() -> None:
    provider, client = _scripted(None, assume_allowed=True, never_evicts_check="assume")
    await provider.aopen()
    assert client.reads == 1
    first = await provider.health()
    second = await provider.health()
    assert (first.status, second.status) == ("degraded", "degraded")
    assert first.details == {"reason": "noeviction_unverified"}
    assert client.reads == 3  # every probe tries the read again
    client.policy = "noeviction"  # the operator grants CONFIG GET
    assert (await provider.health()).status == "ok"
    client.policy = "allkeys-lru"
    assert (await provider.health()).status == "fail"
    await provider.aclose()
    assert client.closed


async def test_verify_health_fails_when_the_policy_becomes_unreadable() -> None:
    provider, client = _scripted("noeviction")
    await provider.aopen()
    assert (await provider.health()).status == "ok"
    client.policy = None
    assert (await provider.health()).status == "fail"
    await provider.aclose()


@pytest.mark.parametrize("allowed", [None, False])
async def test_assume_needs_the_accepted_risk_where_the_kernel_says_so(
    allowed: bool | None,
) -> None:
    provider, client = _scripted(
        None, assume_allowed=allowed, never_evicts_check="assume"
    )
    with pytest.raises(PermissionDenied, match="unverified_noeviction"):
        await provider.aopen()
    assert client.reads == 0  # refused before the server is asked


async def test_the_kernel_factory_carries_the_assume_policy() -> None:
    config = InstanceConfig(
        "atomic_store",
        "default",
        "redis",
        _redis_options(never_evicts_check="assume"),
    )
    refused = make_redis_atomic_store_factory(assume_allowed=lambda: False)(config)
    with pytest.raises(PermissionDenied, match="unverified_noeviction"):
        await refused.aopen()
    allowed = make_redis_atomic_store_factory(assume_allowed=lambda: True)(config)
    await allowed.aopen()  # a real server: the policy is readable and noeviction
    await allowed.aclose()


async def test_an_unreachable_server_is_unavailable_at_aopen() -> None:
    provider = _redis(url="redis://127.0.0.1:1/0", retry_attempts=0)
    with pytest.raises(Unavailable):
        await provider.aopen()
    await provider.aclose()


async def test_a_missing_url_is_an_invalid_argument() -> None:
    provider = RedisAtomicStoreProvider(name="x", driver="redis", options={})
    with pytest.raises(InvalidArgument, match="url"):
        await provider.aopen()


@pytest.mark.parametrize(
    "operation",
    ["get", "add_or_get", "set_subject_state", "compare_and_set"],
)
async def test_each_one_round_trip_operation_is_one_command(operation: str) -> None:
    provider = _redis()
    await provider.aopen()
    try:
        key = f"kit:{uuid4().hex}:round-trip"
        calls: dict[str, Callable[[], Any]] = {
            "get": lambda: provider.get(key),
            "add_or_get": lambda: provider.add_or_get(key, "v", ttl_seconds=5),
            "set_subject_state": lambda: provider.set_subject_state(
                key, "v1:after:1", ttl_seconds=5
            ),
            "compare_and_set": lambda: provider.compare_and_set(key, "a", "b"),
        }
        await calls[operation]()  # warms the script cache (NOSCRIPT + load)
        client = await provider._client_obj()
        sent: list[Any] = []
        real = client.execute_command

        async def counting(*args: Any, **kwargs: Any) -> Any:
            sent.append(args[0])
            return await real(*args, **kwargs)

        client.execute_command = counting
        try:
            await calls[operation]()
        finally:
            del client.execute_command
        assert len(sent) == 1, sent
    finally:
        await provider.aclose()


async def test_concurrent_adds_across_connections_have_one_winner() -> None:
    first, second = _redis(), _redis()
    await first.aopen()
    await second.aopen()
    try:
        key = f"kit:{uuid4().hex}:two-clients"
        answers = await asyncio.gather(
            *(
                (first if i % 2 else second).add_or_get(key, f"racer-{i}")
                for i in range(50)
            )
        )
        assert sum(1 for created, _ in answers if created) == 1
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_backend_failure_never_escapes_as_a_foreign_error() -> None:
    provider = _redis(url="redis://127.0.0.1:1/0", retry_attempts=0)
    for call in (
        lambda: provider.get("k"),
        lambda: provider.set("k", "v"),
        lambda: provider.add_or_get("k", "v"),
        lambda: provider.set_subject_state("k", "v1:after:1"),
    ):
        with pytest.raises(InfraError):
            await call()
    await provider.aclose()
