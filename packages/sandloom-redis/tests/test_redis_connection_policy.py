"""A real redis-py reconnect is judged before it carries a command (a real Redis).

``CLIENT KILL`` drops the pool's sockets; the next operation makes redis-py dial
again, silently, inside the operation. The pool's connect function must read
``maxmemory-policy`` on that new socket before the operation's command is sent.
The server's eviction policy is shared by every test, so it is never changed:
an ACL user that loses ``CONFIG`` stands for a server whose policy can no longer
be verified, and the refusal follows ``never_evicts_check`` exactly as a
readable evicting policy does (the scripted tests in
``test_redis_atomic_store_first_use.py`` cover the readable one).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from _redis_required import require_redis
from redis.asyncio import Redis
from sandloom_redis.atomic_store import RedisAtomicStoreProvider
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.core.config.settings import settings
from sandloom.driverkit import PermissionDenied, Unavailable

_PASSWORD = "conn-check-pw"


@dataclass(frozen=True)
class _Account:
    admin: Redis
    user: str
    url: str

    async def lose_config(self) -> None:
        """The user may no longer ``CONFIG GET``; its sockets are dropped."""
        await self.admin.execute_command("ACL", "SETUSER", self.user, "-config")
        await self.admin.execute_command("CLIENT", "KILL", "USER", self.user)

    async def regain_config(self) -> None:
        await self.admin.execute_command("ACL", "SETUSER", self.user, "+config")

    async def denied_config_reads(self) -> int:
        """How many ``CONFIG GET`` the server refused this user (its ACL log
        groups equal entries, each with a count): every one is a dial's check."""
        entries = await self.admin.acl_log()
        return sum(
            int(entry["count"])
            for entry in entries
            if entry["username"] == self.user
            and str(entry["object"]).lower().startswith("config")
        )


@pytest.fixture
async def account() -> AsyncIterator[_Account]:
    await require_redis(settings.cache_url)
    admin = Redis.from_url(settings.cache_url, decode_responses=True)
    user = f"conncheck_{uuid4().hex[:12]}"
    await admin.execute_command(
        "ACL", "SETUSER", user, "on", f">{_PASSWORD}", "~*", "&*", "+@all"
    )
    parts = urlsplit(settings.cache_url)
    url = f"redis://{user}:{_PASSWORD}@{parts.hostname}:{parts.port}{parts.path}"
    try:
        yield _Account(admin, user, url)
    finally:
        await admin.execute_command("ACL", "DELUSER", user)
        await admin.aclose()


async def test_a_reconnected_store_socket_is_judged_before_its_command(
    account: _Account,
) -> None:
    store = RedisAtomicStoreProvider(
        name="security", driver="redis", options={"url": account.url}
    )
    key = f"conncheck:{uuid4().hex}"
    try:
        await store.set(f"{key}:before", "v")  # the first socket: judged, fit
        await account.lose_config()
        with pytest.raises(PermissionDenied, match="CONFIG GET"):
            await store.set(f"{key}:retrying", "v")  # the retrying client
        with pytest.raises(PermissionDenied, match="CONFIG GET"):
            await store.add(f"{key}:once", "v")  # the client that never re-sends
        assert await account.admin.mget(f"{key}:retrying", f"{key}:once") == [
            None,
            None,
        ]  # no command reached the unverified server
        await account.regain_config()
        await store.set(f"{key}:after", "v")  # judged again, fit again
        assert await account.admin.get(f"{key}:after") == "v"
    finally:
        await store.aclose()
        await account.admin.delete(*(f"{key}:{part}" for part in ("before", "after")))


async def test_assume_keeps_a_reconnected_socket_whose_policy_is_unreadable(
    account: _Account,
) -> None:
    store = RedisAtomicStoreProvider(
        name="security",
        driver="redis",
        options={"url": account.url, "never_evicts_check": "assume"},
        assume_allowed=lambda: True,
    )
    key = f"conncheck:{uuid4().hex}"
    try:
        await store.set(key, "v")
        assert await account.denied_config_reads() == 0
        await account.lose_config()
        await store.set(key, "w")  # reconnects; the operator vouched
        assert await account.admin.get(key) == "w"
        # The hook did run on the new socket: it asked, was refused, and assume
        # let the socket through (without the hook nothing would have asked).
        assert await account.denied_config_reads() >= 1
    finally:
        await store.aclose()
        await account.admin.delete(key)


async def test_a_reconnected_lease_socket_is_judged_before_its_command(
    account: _Account,
) -> None:
    lease = RedisLeaseProvider(
        name="beat", driver="redis", options={"url": account.url}
    )
    name = f"conncheck-{uuid4().hex}"
    try:
        held = await lease.acquire(name, ttl_seconds=30, owner="o")
        assert held is not None
        await account.lose_config()
        # The lease pool has no Retry: a socket CLIENT KILL dropped may not be
        # noticed until a command fails on it (Unavailable, the socket is then
        # discarded). So the first call absorbs that race and the second dials
        # afresh, which the pool must judge before its command.
        with pytest.raises((Unavailable, PermissionDenied)):
            await lease.release(held)
        with pytest.raises(PermissionDenied, match="CONFIG GET"):
            await lease.release(held)
        await account.regain_config()
        assert await lease.release(held) is True  # still held: nothing ran
    finally:
        await lease.aclose()


async def test_a_lease_health_probe_on_a_refused_socket_reports_the_reason(
    account: _Account,
) -> None:
    """Through the real pool: the socket CLIENT KILL forced a new dial of is
    refused by the connect function, and health answers the policy's reason,
    not the error type of the refusal."""
    lease = RedisLeaseProvider(
        name="beat", driver="redis", options={"url": account.url}
    )
    try:
        await lease.aopen()
        await account.lose_config()
        first = await lease.health()  # may meet the dropped socket (Unavailable)
        assert first.status == "fail"
        second = await lease.health()  # a fresh dial: judged by the pool
        assert (second.status, second.details) == (
            "fail",
            {"reason": "noeviction_unverifiable"},
        )
        await account.regain_config()
        assert (await lease.health()).status == "ok"
    finally:
        await lease.aclose()
