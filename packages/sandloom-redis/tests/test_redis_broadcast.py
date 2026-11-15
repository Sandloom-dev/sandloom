"""The ``redis`` broadcast driver against a real Redis (``BroadcastConformance``).

Skipped when no Redis is reachable at ``SANDLOOM_TEST_CACHE_URL`` / ``CACHE_URL``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from sandloom_redis.broadcast import RedisBroadcastProvider

from sandloom.core.testing import reload_test_env
from sandloom.driverkit import BroadcastProvider, Unavailable
from sandloom.driverkit.testing import BroadcastConformance

redis_asyncio = pytest.importorskip("redis.asyncio")

_PAYLOAD_LIMIT = 1024 * 1024


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[3] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _url() -> str:
    url = os.getenv("SANDLOOM_TEST_CACHE_URL") or os.getenv("CACHE_URL")
    if not url:
        pytest.skip("no test Redis URL: Redis broadcast kit skipped")
    return url


@pytest.fixture
async def _redis() -> AsyncIterator[Any]:
    client = redis_asyncio.from_url(_url())
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    yield client
    await client.aclose()


def _provider(**options: Any) -> RedisBroadcastProvider:
    return _TaggedProvider(
        name="default", driver="redis", options={"url": _url(), **options}
    )


def _url_on_db(db: int) -> str:
    parts = urlsplit(_url())
    return urlunsplit(parts._replace(path=f"/{db}"))


class _Proxy:
    """A TCP relay in front of the test Redis that can stop relaying without
    closing anything (a half-open connection: no RST, no FIN)."""

    def __init__(self) -> None:
        self.relayed = 0
        self._flow = asyncio.Event()
        self._flow.set()
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.port = 0

    async def start(self) -> _Proxy:
        target = urlsplit(_url())
        host, port = target.hostname or "127.0.0.1", target.port or 6379

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            self._writers += [writer, upstream_writer]
            await asyncio.gather(
                self._pump(reader, upstream_writer),
                self._pump(upstream_reader, writer),
                return_exceptions=True,
            )

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _pump(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter
    ) -> None:
        while data := await source.read(65536):
            await self._flow.wait()
            self.relayed += len(data)
            sink.write(data)
            await sink.drain()

    def url(self, db: int = 0) -> str:
        parts = urlsplit(_url())
        return urlunsplit(
            parts._replace(netloc=f"127.0.0.1:{self.port}", path=f"/{db}")
        )

    def freeze(self) -> None:
        self._flow.clear()

    async def stop(self) -> None:
        self._flow.set()
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()


class _TaggedProvider(RedisBroadcastProvider):
    """A provider whose connections carry a name of their own, so a test can
    cut exactly them (``CLIENT KILL TYPE pubsub`` cuts every pub/sub client of
    the shared test Redis: other xdist workers, the shadow and bus tests)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.connection_name = f"idkb-{uuid4().hex}"

    def _new_client(
        self, *, socket_timeout: float | None, health_check: bool = True
    ) -> Any:
        client = super()._new_client(
            socket_timeout=socket_timeout, health_check=health_check
        )
        client.connection_pool.connection_kwargs["client_name"] = self.connection_name
        return client


async def _kill_connections_named(name: str) -> int:
    """Cut the connections named ``name`` and no others."""
    client = redis_asyncio.from_url(_url())
    try:
        listing = (await client.execute_command("CLIENT", "LIST")).decode()
        ids = [
            fields["id"]
            for line in listing.splitlines()
            if (fields := dict(p.split("=", 1) for p in line.split(" ") if "=" in p))
            and fields.get("name") == name
        ]
        for client_id in ids:
            await client.execute_command("CLIENT", "KILL", "ID", client_id)
        return len(ids)
    finally:
        await client.aclose()


class TestTheRedisDriver(BroadcastConformance):
    payload_limit_bytes = _PAYLOAD_LIMIT
    health_timeout_seconds = 5.0

    @pytest.fixture
    def make_provider(self, _redis: Any) -> Callable[[], BroadcastProvider]:
        return _provider

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], BroadcastProvider]:
        marker = self.leak_marker
        return lambda: RedisBroadcastProvider(
            name="default",
            driver="redis",
            options={"url": f"redis://:{marker}@127.0.0.1:1/0"},
        )

    async def interrupt_connections(self, provider: BroadcastProvider) -> None:
        assert isinstance(provider, _TaggedProvider)
        await _kill_connections_named(provider.connection_name)


async def test_the_prefix_keeps_two_instances_apart(_redis: Any) -> None:
    first, second = _provider(prefix="one:"), _provider(prefix="two:")
    await first.aopen()
    await second.aopen()
    try:
        in_second = await second.subscribe(["same"])
        in_first = await first.subscribe(["same"])
        await first.publish("same", b"for one")
        message = await _next(in_first)
        assert message.channel == "same"
        assert message.payload == b"for one"
        assert await _quiet(in_second)
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_name_with_glob_characters_is_a_literal_prefix(_redis: Any) -> None:
    provider = _provider(prefix="a[b]*:")
    await provider.aopen()
    try:
        subscription = await provider.subscribe(["x.*"])
        await provider.publish("x.y", b"hit")
        assert (await _next(subscription)).payload == b"hit"
        other = _provider(prefix="aXb:")
        await other.aopen()
        try:
            await other.publish("x.y", b"miss")
            assert await _quiet(subscription)
        finally:
            await other.aclose()
    finally:
        await provider.aclose()


async def test_the_test_isolation_hook_prefixes_the_channels() -> None:
    options = RedisBroadcastProvider.test_isolation({"url": "redis://x/4"}, "gw3")
    assert options == {"url": "redis://x/4", "prefix": "db4:wgw3:"}
    stated = RedisBroadcastProvider.test_isolation(
        {"url": "redis://x", "prefix": "a:"}, "1"
    )
    assert stated["prefix"] == "a:w1:"


@pytest.mark.parametrize(
    ("url", "prefix"),
    [
        ("redis://h:6379", "db0:"),
        ("redis://h:6379/0", "db0:"),
        ("rediss://:pw@h/7", "db7:"),
        ("redis://h/?db=3", "db3:"),
        ("unix:///run/redis.sock?db=2", "db2:"),
        ("redis://h/notanumber", "db0:"),
    ],
)
def test_the_default_prefix_is_the_database_index_of_the_url(
    url: str, prefix: str
) -> None:
    provider = RedisBroadcastProvider(name="x", driver="redis", options={"url": url})
    assert provider._prefix == prefix


async def _next(subscription: Any) -> Any:
    return await asyncio.wait_for(subscription.__aiter__().__anext__(), timeout=5)


async def _quiet(subscription: Any, seconds: float = 0.3) -> bool:
    try:
        await asyncio.wait_for(subscription.__aiter__().__anext__(), timeout=seconds)
    except TimeoutError:
        return True
    return False


async def test_an_interruption_cuts_only_its_providers_connections(
    _redis: Any,
) -> None:
    """The kit's outage check must not cut the pub/sub clients of other tests
    on the shared Redis (a bystander's subscription survives it)."""
    bystander = _provider(prefix="bystander:")
    victim = _provider(prefix="victim:")
    await bystander.aopen()
    await victim.aopen()
    try:
        watching = await bystander.subscribe(["x"])
        doomed = await victim.subscribe(["x"])
        await TestTheRedisDriver().interrupt_connections(victim)
        with pytest.raises(Unavailable):
            await asyncio.wait_for(doomed.__aiter__().__anext__(), timeout=5)
        await bystander.publish("x", b"still here")
        assert (await _next(watching)).payload == b"still here"
    finally:
        await bystander.aclose()
        await victim.aclose()


async def test_apps_on_other_database_indexes_do_not_see_each_other(
    _redis: Any,
) -> None:
    """Pub/Sub ignores the database index, so without a ``prefix`` the driver
    puts its channels under ``db<N>:`` of its URL: two apps that share one
    Redis server by database index keep their events apart."""
    first = RedisBroadcastProvider(
        name="one", driver="redis", options={"url": _url_on_db(1)}
    )
    second = RedisBroadcastProvider(
        name="two", driver="redis", options={"url": _url_on_db(2)}
    )
    same_db = RedisBroadcastProvider(
        name="three", driver="redis", options={"url": _url_on_db(1)}
    )
    for provider in (first, second, same_db):
        await provider.aopen()
    channel = f"iso.{uuid4().hex[:8]}"
    try:
        in_second = await second.subscribe([channel])
        in_pattern = await second.subscribe(["iso.*"])
        in_same_db = await same_db.subscribe([channel])
        await first.publish(channel, b"db one")
        assert (await _next(in_same_db)).payload == b"db one"  # same index: shared
        assert await _quiet(in_second)
        assert await _quiet(in_pattern)
    finally:
        for provider in (first, second, same_db):
            await provider.aclose()


async def test_an_explicit_prefix_overrides_the_database_index(_redis: Any) -> None:
    first = RedisBroadcastProvider(
        name="one", driver="redis", options={"url": _url_on_db(1), "prefix": "shared:"}
    )
    second = RedisBroadcastProvider(
        name="two", driver="redis", options={"url": _url_on_db(2), "prefix": "shared:"}
    )
    await first.aopen()
    await second.aopen()
    try:
        subscription = await second.subscribe(["same"])
        await first.publish("same", b"crosses")
        assert (await _next(subscription)).payload == b"crosses"
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_half_open_connection_ends_the_subscription_with_unavailable(
    _redis: Any,
) -> None:
    """A proxy that stops relaying (no RST, no FIN) leaves a silent socket: the
    driver's own ping gets no pong within ``socket_timeout_seconds`` and the
    iterator raises, instead of waiting for a message that never comes."""
    proxy = await _Proxy().start()
    provider = RedisBroadcastProvider(
        name="half-open",
        driver="redis",
        options={
            "url": proxy.url(),
            "prefix": f"halfopen{uuid4().hex[:8]}:",
            "socket_timeout_seconds": 1.0,
            "health_check_interval_seconds": 1,
        },
    )
    await provider.aopen()
    try:
        subscription = await provider.subscribe(["x"])
        await provider.publish("x", b"alive")
        assert (await _next(subscription)).payload == b"alive"
        proxy.freeze()
        with pytest.raises(Unavailable):
            await asyncio.wait_for(subscription.__aiter__().__anext__(), timeout=10)
    finally:
        await provider.aclose()
        await proxy.stop()


async def test_a_healthy_idle_subscription_survives_its_pings(_redis: Any) -> None:
    provider = _provider(
        prefix=f"idle{uuid4().hex[:8]}:",
        socket_timeout_seconds=1.0,
        health_check_interval_seconds=1,
    )
    await provider.aopen()
    try:
        subscription = await provider.subscribe(["x"])
        assert await _quiet(subscription, seconds=3.5)  # several ping rounds
        await provider.publish("x", b"after")
        assert (await _next(subscription)).payload == b"after"
    finally:
        await provider.aclose()


async def test_the_payload_limit_defaults_to_the_one_mebibyte_floor() -> None:
    provider = _provider()
    assert provider._options.max_payload_bytes == 1024 * 1024
