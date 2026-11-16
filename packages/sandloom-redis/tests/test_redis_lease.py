"""The ``redis`` lease driver: the kit against a real Redis
(``resolved_cache_url()``), and what the kit cannot know: the eviction check, its
operator override and the options.

The eviction logic runs against a scripted client, so no server's configuration
is changed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import redis.exceptions as redis_errors
from _redis_required import cache_url_or_unavailable
from sandloom_redis import lease
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.driverkit import InfraError, InvalidArgument, PermissionDenied
from sandloom.driverkit.lease_limits import (
    MAX_LEASE_NAME_LENGTH,
    MIN_LEASE_TTL_SECONDS,
)
from sandloom.driverkit.testing import LeaseConformance


class TestRedisLease(LeaseConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], RedisLeaseProvider]:
        # The harness merges SANDLOOM_TEST_CACHE_URL into INFRA: the url is the
        # test run's default cache.
        url = cache_url_or_unavailable()
        return lambda: RedisLeaseProvider(
            name="beat", driver="redis", options={"url": url}
        )

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], RedisLeaseProvider]:
        url = f"redis://:{self.leak_marker}@127.0.0.1:1/0"
        return lambda: RedisLeaseProvider(
            name="beat",
            driver="redis",
            options={"url": url, "socket_connect_timeout_seconds": 0.2},
        )


class _Redis:
    """A scripted client: ``policy`` is the answer of ``CONFIG GET
    maxmemory-policy`` (``None``: the server refuses it)."""

    def __init__(self, policy: str | None) -> None:
        self.policy = policy
        self.reads = 0
        self.broken = False

    async def config_get(self, key: str) -> dict[str, str]:
        self.reads += 1
        if self.policy is None:
            raise redis_errors.NoPermissionError("NOPERM CONFIG")
        return {key: self.policy}

    async def ping(self) -> bool:
        if self.broken:
            raise redis_errors.ConnectionError("down")
        return True

    async def eval(self, *_: object) -> int:
        if self.broken:
            raise redis_errors.ConnectionError("down")
        return 1

    async def aclose(self, **_: object) -> None:
        return None


def _reason(health: Any) -> object:
    assert health.details is not None
    return health.details["reason"]


def _scripted(
    policy: str | None, *, allowed: bool | None = None, **options: Any
) -> tuple[RedisLeaseProvider, _Redis]:
    client = _Redis(policy)
    provider = RedisLeaseProvider(
        name="beat",
        driver="redis",
        options=options,
        assume_allowed=None if allowed is None else (lambda: allowed),
        client_factory=lambda: client,
    )
    return provider, client


async def test_assume_is_refused_unless_the_kernel_allows_it() -> None:
    for allowed in (None, False):
        provider, _ = _scripted(None, allowed=allowed, never_evicts_check="assume")
        with pytest.raises(PermissionDenied, match="unverified_noeviction"):
            await provider.aopen()
        health = await provider.health()
        assert (health.status, _reason(health)) == (
            "fail",
            "eviction_check_refused",
        )
        with pytest.raises(PermissionDenied):
            await provider.acquire("n", ttl_seconds=30, owner="o")
    provider, _ = _scripted(None, allowed=True, never_evicts_check="assume")
    await provider.aopen()
    assert (await provider.acquire("n", ttl_seconds=30, owner="o")) is not None
    health = await provider.health()
    assert (health.status, _reason(health)) == (
        "degraded",
        "noeviction_unverified",
    )


async def test_a_refused_read_under_verify_is_permission_denied() -> None:
    provider, _ = _scripted(None)
    with pytest.raises(PermissionDenied, match="CONFIG GET"):
        await provider.aopen()
    health = await provider.health()
    assert _reason(health) == "eviction_check_refused"
    provider, _ = _scripted(None)
    health = await provider.health()
    assert (health.status, _reason(health)) == (
        "fail",
        "noeviction_unverifiable",
    )


@pytest.mark.parametrize("mode", ["verify", "assume"])
async def test_a_readable_evicting_policy_is_invalid_argument_in_both_modes(
    mode: str,
) -> None:
    provider, _ = _scripted("allkeys-lru", allowed=True, never_evicts_check=mode)
    with pytest.raises(InvalidArgument, match="allkeys-lru"):
        await provider.aopen()
    with pytest.raises(InvalidArgument):
        await provider.acquire("n", ttl_seconds=30, owner="o")
    health = await provider.health()
    assert _reason(health) == "eviction_check_refused"
    fresh, _ = _scripted("allkeys-lru", allowed=True, never_evicts_check=mode)
    assert _reason(await fresh.health()) == "evicting_policy"


async def test_the_policy_is_read_again_after_a_connection_failure() -> None:
    provider, client = _scripted("noeviction")
    await provider.aopen()
    await provider.acquire("n", ttl_seconds=30, owner="o")
    await provider.acquire("n", ttl_seconds=30, owner="o")
    assert client.reads == 1  # one read while the connection stays up
    client.broken = True
    with pytest.raises(InfraError):
        await provider.acquire("n", ttl_seconds=30, owner="o")
    client.broken = False
    client.policy = "allkeys-lru"  # the server came back reconfigured
    with pytest.raises(InvalidArgument, match="allkeys-lru"):
        await provider.acquire("n", ttl_seconds=30, owner="o")
    assert client.reads == 2


def test_the_redis_lease_runs_each_xdist_worker_on_its_own_database_index() -> None:
    options = {"url": "redis://127.0.0.1:6379/0", "never_evicts_check": "verify"}
    moved = RedisLeaseProvider.test_isolation(options, "gw1")
    assert moved["url"] != options["url"]
    assert moved["never_evicts_check"] == "verify"
    assert RedisLeaseProvider.test_isolation(options, "master") == options


def test_the_redis_lease_shares_the_limits_of_the_port() -> None:
    assert lease.MAX_LEASE_NAME_LENGTH is MAX_LEASE_NAME_LENGTH
    assert lease.MIN_LEASE_TTL_SECONDS is MIN_LEASE_TTL_SECONDS
    assert not hasattr(lease, "_MAX_NAME_LENGTH")
    assert not hasattr(lease, "_MIN_TTL_SECONDS")


def test_the_driver_options_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        RedisLeaseProvider(name="b", driver="redis", options={"ur1": "x"})


def test_the_driver_declares_its_tier_and_requirements() -> None:
    assert RedisLeaseProvider.tier == "beta"
    assert RedisLeaseProvider.required_imports == ("redis",)
    assert RedisLeaseProvider.features_for({}) == {"shared"}
