"""The ``redis`` atomic_store driver against the kit, and what the kit cannot
know: the eviction check, its operator override, the round trips and the
identity.

The tests need the test Redis (``SANDLOOM_TEST_CACHE_URL``); the eviction logic
runs against a scripted client, so no server's configuration is changed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from functools import partial
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sandloom_redis._settings_fill import MissingUrlError
from sandloom_redis.atomic_store import RedisAtomicStoreProvider

from sandloom.core.config.settings import settings
from sandloom.driverkit import (
    AtomicStoreProvider,
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Unavailable,
)
from sandloom.driverkit.testing import AtomicStoreConformance

_VALUE_LIMIT = 8192


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
        # The kit's races open 50 connections at once: the production defaults
        # (0.5 s socket and connect timeouts) time out on a loaded CI runner.
        return partial(
            _redis,
            socket_timeout_seconds=5.0,
            socket_connect_timeout_seconds=5.0,
            pool_timeout_seconds=5.0,
        )

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], AtomicStoreProvider]:
        # Nothing listens on port 1; the "password" is the kit's secret.
        url = f"redis://:{self.leak_marker}@127.0.0.1:1/0"
        return lambda: _redis(url=url, retry_attempts=0)

    @pytest.fixture
    def make_evicting_provider(self) -> Callable[[], AtomicStoreProvider]:
        return lambda: _scripted("allkeys-lru")[0]

    @pytest.fixture
    def make_evicting_assume_provider(self) -> Callable[[], AtomicStoreProvider]:
        return lambda: _scripted(
            "allkeys-lru", assume_allowed=True, never_evicts_check="assume"
        )[0]


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


async def test_the_constructor_carries_the_assume_policy() -> None:
    options = _redis_options(never_evicts_check="assume")
    refused = RedisAtomicStoreProvider(
        name="default", driver="redis", options=options, assume_allowed=lambda: False
    )
    with pytest.raises(PermissionDenied, match="unverified_noeviction"):
        await refused.aopen()
    allowed = RedisAtomicStoreProvider(
        name="default", driver="redis", options=options, assume_allowed=lambda: True
    )
    await allowed.aopen()  # a real server: the policy is readable and noeviction
    await allowed.aclose()


def test_the_driver_declares_its_tier_and_requirements() -> None:
    assert RedisAtomicStoreProvider.tier == "beta"
    assert RedisAtomicStoreProvider.required_imports == ("redis",)


async def test_an_unreachable_server_is_unavailable_at_aopen() -> None:
    provider = _redis(url="redis://127.0.0.1:1/0", retry_attempts=0)
    with pytest.raises(Unavailable):
        await provider.aopen()
    await provider.aclose()


async def test_a_missing_url_refuses_startup_naming_the_infra_option() -> None:
    provider = RedisAtomicStoreProvider(name="x", driver="redis", options={})
    with pytest.raises(MissingUrlError, match=r"atomic_store\.x\.url") as caught:
        await provider.aopen()
    assert caught.value.fatal_at_startup is True


async def test_an_operation_with_no_url_is_still_an_invalid_argument() -> None:
    provider = RedisAtomicStoreProvider(name="x", driver="redis", options={})
    with pytest.raises(InvalidArgument, match="url"):
        await provider.get("k")


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
        sent: list[Any] = []
        clients = {
            id(c): c
            for c in (
                await provider._operation_client(idempotent=True),
                await provider._operation_client(idempotent=False),
            )
        }
        for client in clients.values():
            real = client.execute_command

            async def counting(*args: Any, _real: Any = real, **kwargs: Any) -> Any:
                sent.append(args[0])
                return await _real(*args, **kwargs)

            client.execute_command = counting
        try:
            await calls[operation]()
        finally:
            for client in clients.values():
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


class _DroppingProxy:
    """A TCP proxy in front of the real server that, once armed for a command
    name, executes the next such command upstream and closes the client's
    connection instead of relaying its reply: the lost reply of a command the
    server ran. Other traffic (the handshake of a fresh connection, a
    ``SCRIPT LOAD``) passes untouched, so the drop never lands before the
    target command was sent."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self.upstream = upstream
        self.target: bytes | None = None
        self.dropped = 0
        self.connections = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _DroppingProxy:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def arm(self, command: bytes) -> None:
        """Drop the reply of the next ``command`` (its RESP name, upper case)."""
        self.target = command

    @property
    def url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        # The worker's own database index: under xdist it is not /0.
        db = urlsplit(settings.cache_url).path or "/0"
        return f"redis://127.0.0.1:{port}{db}"

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        up_reader, up_writer = await asyncio.open_connection(*self.upstream)

        pending = False
        matched: bytes | None = None

        async def forward() -> None:
            nonlocal pending, matched
            while data := await reader.read(65536):
                # A command is ``*N\r\n$len\r\nNAME\r\n...``: match the name only,
                # never a script body or a value that happens to contain it.
                parts = data.split(b"\r\n", 3)
                if (
                    self.target is not None
                    and len(parts) > 3
                    and parts[2].upper() == self.target
                ):
                    matched = self.target
                    self.target = None
                    pending = True
                up_writer.write(data)
                await up_writer.drain()

        async def back() -> None:
            nonlocal pending
            while data := await up_reader.read(65536):
                # A NOSCRIPT error means the command did not run: let the
                # client load the script and re-send, then drop that reply.
                if pending and data.startswith(b"-NOSCRIPT"):
                    pending = False
                    self.target = matched
                if pending:
                    pending = False
                    self.dropped += 1
                    return  # the server ran the command; the reply is lost
                writer.write(data)
                await writer.drain()

        tasks = [asyncio.ensure_future(forward()), asyncio.ensure_future(back())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            writer.close()
            up_writer.close()


def _upstream() -> tuple[str, int]:
    parts = urlsplit(settings.cache_url)
    return parts.hostname or "127.0.0.1", parts.port or 6379


async def test_a_lost_reply_of_a_non_idempotent_operation_is_not_sent_twice() -> None:
    """A command whose reply was lost has run: re-sending it would answer
    ``False`` / the caller's own value to the caller that won."""
    async with _DroppingProxy(_upstream()) as proxy:
        direct = _redis()
        through = _redis(url=proxy.url, retry_attempts=1)
        await direct.aopen()
        await through.aopen()
        try:
            key = f"kit:{uuid4().hex}:lost-reply"
            calls: dict[str, Callable[[], Any]] = {
                "add": lambda: through.add(key + ":add", "v", ttl_seconds=30),
                "add_or_get": lambda: through.add_or_get(
                    key + ":aog", "v", ttl_seconds=30
                ),
                "compare_and_set": lambda: through.compare_and_set(
                    key + ":cas", "a", "b", ttl_seconds=30
                ),
                "compare_and_delete": lambda: through.compare_and_delete(
                    key + ":cad", "a"
                ),
            }
            commands = {
                "add": b"SET",
                "add_or_get": b"EVALSHA",
                "compare_and_set": b"EVALSHA",
                "compare_and_delete": b"EVALSHA",
            }
            await direct.set(key + ":cas", "a", ttl_seconds=30)
            await direct.set(key + ":cad", "a", ttl_seconds=30)
            for name, call in calls.items():
                await through.get(key)  # a pooled connection to drop on
                await through.add(f"{key}:warm:{name}", "w", ttl_seconds=30)
                proxy.arm(commands[name])
                with pytest.raises(Unavailable):
                    await call()
                assert proxy.dropped == 1, name
                proxy.dropped = 0
            # Each command ran exactly once, on the server.
            assert await direct.get(key + ":add") == "v"
            assert await direct.get(key + ":aog") == "v"
            assert await direct.get(key + ":cas") == "b"
            assert await direct.get(key + ":cad") is None
        finally:
            await through.aclose()
            await direct.aclose()


async def test_a_lost_reply_of_an_idempotent_operation_is_retried() -> None:
    async with _DroppingProxy(_upstream()) as proxy:
        through = _redis(url=proxy.url, retry_attempts=1)
        await through.aopen()
        try:
            key = f"kit:{uuid4().hex}:idempotent"
            await through.set(key, "v", ttl_seconds=30)
            cases: list[tuple[bytes, Callable[[], Any], Any]] = [
                (b"GET", lambda: through.get(key), "v"),
                (b"SET", lambda: through.set(key, "w", ttl_seconds=30), None),
                (b"DEL", lambda: through.delete(key + ":other"), None),
                (
                    b"EVALSHA",
                    lambda: through.set_subject_state(
                        key + ":s", "v1:after:1", ttl_seconds=30
                    ),
                    "v1:after:1",
                ),
            ]
            for command, call, expected in cases:
                proxy.arm(command)
                assert await call() == expected
                assert proxy.dropped == 1, command
                proxy.dropped = 0
        finally:
            await through.aclose()


async def test_a_long_corrupt_stored_value_is_replaced_without_a_scan() -> None:
    """The Lua pattern must not backtrack on a corrupt value (a 40k-char one
    held a Redis server for seconds)."""
    provider = _redis(max_value_bytes=1_048_576)
    await provider.aopen()
    try:
        key = f"kit:{uuid4().hex}:corrupt-long"
        await provider.set(key, "v1:after:" + "1" * 40_000 + "x", ttl_seconds=30)
        started = time.monotonic()
        held = await provider.set_subject_state(key, "v1:after:5", ttl_seconds=30)
        assert time.monotonic() - started < 0.5
        assert held == "v1:after:5"
        assert await provider.get(key) == "v1:after:5"
    finally:
        await provider.aclose()


async def test_operations_refuse_until_the_eviction_check_has_passed() -> None:
    """Outside production the registry only logs a refused ``aopen``: the
    provider it still serves must not run an operation on an evicting store."""
    provider, _ = _scripted("allkeys-lru")
    with pytest.raises(InvalidArgument, match="noeviction"):
        await provider.aopen()
    calls: list[Callable[[], Any]] = [
        lambda: provider.get("k"),
        lambda: provider.set("k", "v"),
        lambda: provider.delete("k"),
        lambda: provider.add("k", "v"),
        lambda: provider.add_or_get("k", "v"),
        lambda: provider.compare_and_set("k", "a", "b"),
        lambda: provider.compare_and_delete("k", "a"),
        lambda: provider.set_subject_state("k", "v1:after:1"),
    ]
    for call in calls:
        with pytest.raises(InvalidArgument, match="noeviction"):
            await call()
    assert (await provider.health()).status == "fail"
    await provider.aclose()
