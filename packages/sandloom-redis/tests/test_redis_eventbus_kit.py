"""The ``EventTransportConformance`` kit (spec 7) against the ``streams`` driver
of this package, on a real Redis (``resolved_cache_url()``).

The kit's own behaviour (the in-memory reference transport, the deliberately
broken transports that must fail exactly the checks that name them) is tested
in the kernel (``src/sandloom/core/tests/test_eventbus_kit.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any
from uuid import uuid4

import pytest
from _redis_required import cache_url_or_unavailable, redis_unavailable
from redis import asyncio as redis_asyncio
from sandloom_redis.eventbus.driver import StreamsEventBusProvider

from sandloom.core.infra.ports.eventbus import EventTransportProvider, Retention
from sandloom.driverkit.testing import EventTransportConformance


def _redis_url() -> str:
    url = cache_url_or_unavailable()
    if not url:
        redis_unavailable("no test Redis URL: the streams kit is skipped")
    return str(url)


class _DlqSwitch:
    """The Redis client of a streams provider; while ``owner.dlq_down`` it
    refuses appends to a dead-letter queue and nothing else."""

    def __init__(self, client: Any, owner: _StreamsUnderTest) -> None:
        self._client = client
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def xadd(self, key: str, *args: Any, **kwargs: Any) -> Any:
        if self._owner.dlq_down and str(key).endswith(".dlq"):
            raise ConnectionError("the dead-letter queue is down")
        return await self._client.xadd(key, *args, **kwargs)


class _StreamsUnderTest(StreamsEventBusProvider):
    """The ``streams`` driver with the tuning the kit asks for (its delivery
    budget, no retry backoff, a short idle time before a dead consumer's
    entries are reclaimed) and a client of its own."""

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: Mapping[str, Any],
        client: object | None = None,
        client_factory: object | None = None,
    ) -> None:
        # ``from_config`` builds this class too (the kit's renamed provider); the
        # client it passes is not used: the kit's own ``_connect`` is.
        del client, client_factory
        super().__init__(
            name=name, driver=driver, options=options, client_factory=self._connect
        )
        self._max_deliveries = EventTransportConformance.max_deliveries
        self._retry_base_ms = 0
        self._min_idle_ms = 500
        self._reclaim_interval = 0.0
        self._redis: _DlqSwitch | None = None
        self.dlq_down = False
        #: Names this provider's connections, so an outage cuts only them.
        self.connection_name = f"idke-{uuid4().hex}"

    async def _connect(self) -> Any:
        if self._redis is None:
            raw = redis_asyncio.from_url(
                self.options["url"],
                encoding="utf-8",
                decode_responses=True,
                client_name=self.connection_name,
            )
            self._redis = _DlqSwitch(raw, self)
        return self._redis

    async def redis(self) -> Any:
        return await self._connect()

    async def aclose(self) -> None:
        if self._redis is not None:
            client, self._redis = self._redis, None
            with suppress(Exception):
                await client.aclose()
        await super().aclose()


@asynccontextmanager
async def _a_key_that_is_not_a_stream(
    provider: _StreamsUnderTest, stream: str
) -> AsyncIterator[None]:
    """Appends to ``stream`` fail at execution time (``WRONGTYPE``)."""
    client = await provider.redis()
    key = provider.key(stream)
    await client.set(key, "not a stream")
    try:
        yield
    finally:
        await client.delete(key)


async def _kill_connections_named(url: str, name: str) -> int:
    """Cut the Redis connections named ``name`` and no others."""
    client: Any = redis_asyncio.from_url(url)
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


@asynccontextmanager
async def _streams_dlq_down(provider: _StreamsUnderTest) -> AsyncIterator[None]:
    provider.dlq_down = True
    try:
        yield
    finally:
        provider.dlq_down = False


def _streams(**options: Any) -> _StreamsUnderTest:
    options.setdefault("url", _redis_url())
    return _StreamsUnderTest(name="kit", driver="streams", options=options)


class TestTheStreamsDriver(EventTransportConformance):
    redelivery_timeout_seconds = 15.0
    settle_seconds = 0.6
    # The streams driver is of the stable tier: every hook is given.
    require_stable_hooks = True

    @pytest.fixture(autouse=True)
    async def _clean(self) -> AsyncIterator[None]:
        yield
        provider = _streams()
        client = await provider.redis()
        try:
            for pattern in ("kit.*", "sandloom:group-owner:kit.*"):
                async for key in client.scan_iter(match=pattern):
                    await client.delete(key)
        finally:
            await provider.aclose()

    @pytest.fixture
    def make_provider(self) -> Callable[[], EventTransportProvider]:
        _redis_url()
        return _streams

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], EventTransportProvider]:
        # Nothing listens on port 1; the "password" is the kit's secret.
        url = f"redis://:{self.leak_marker}@127.0.0.1:1/0"
        return lambda: _streams(url=url)

    def refused_stream(
        self, provider: EventTransportProvider, stream: str
    ) -> AbstractAsyncContextManager[None]:
        assert isinstance(provider, _StreamsUnderTest)
        return _a_key_that_is_not_a_stream(provider, stream)

    def failing_dlq_append(
        self, provider: EventTransportProvider
    ) -> AbstractAsyncContextManager[None]:
        assert isinstance(provider, _StreamsUnderTest)
        return _streams_dlq_down(provider)

    async def interrupt_connections(self, provider: EventTransportProvider) -> None:
        assert isinstance(provider, _StreamsUnderTest)
        await _kill_connections_named(provider.options["url"], provider.connection_name)

    def retention_cases(self) -> Sequence[tuple[Mapping[str, Any], Retention]]:
        return [
            (
                {"retention_seconds": 3600, "max_length": 500},
                Retention(max_age_seconds=3600, max_length=500),
            ),
            (
                {"retention_seconds": 0, "max_length": 0},
                Retention(max_age_seconds=None, max_length=None),
            ),
        ]
