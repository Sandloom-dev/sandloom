"""Built-in event-bus drivers (``memory`` and ``redis``).

The bus needs the schema registry at construction time, so these driver
factories are produced by :func:`make_event_bus_drivers`, which the bootstrap
calls with the application's schema registry. The resulting provider wraps an
``EventBusProtocol`` instance and participates in the infra lifecycle
(``aclose`` closes the bus).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

import sandloom.core.drivers.redis_cache as _redis_cache
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.descriptors import HealthResult
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import RedisEventBus
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import EventBusProvider
from sandloom.core.infra.ports._base import (
    health_failure_details,
    isolated_redis_options,
)
from sandloom.core.infra.registry import DriverFactory


class EventBusOptions(BaseModel):
    """Config-shape schema shared by the ``memory``/``redis``/``streams`` buses.

    ``url`` is the Redis DSN of the ``streams`` driver (``memory`` ignores
    every option), with the connect timeout and health-check interval of its
    client. The kernel fills all three from ``CACHE_URL`` and the ``CACHE_*``
    settings when the configuration leaves them out. The ``default`` instance
    shares the process-wide default transport client (a sibling of the default
    cache client on its own ``url``); a named instance connects its own client.

    The ``redis`` driver (the volatile bus) has no client of its own: it runs
    over the ``broadcast`` instance named by ``broadcast`` (default
    ``default``), on that instance's Redis, TLS, credentials and channel
    namespace. A ``url`` on a ``redis`` bus is refused at startup. ``extra="forbid"``
    rejects a typo'd key at startup.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = None
    #: The ``broadcast`` instance of the ``redis`` driver.
    broadcast: str = "default"
    socket_connect_timeout_seconds: float = Field(default=0.5, gt=0)
    health_check_interval_seconds: int = Field(default=30, ge=0)


class _LazyRedisClient:
    """A lazily-connected, instance-owned Redis client with explicit teardown.

    A named eventbus instance with its own ``url`` connects its own client on
    first use and caches it — a bounded transport client (connect timeout,
    keepalive, health check, the bus's read timeout; see
    ``redis_cache.build_transport_client``). Because the client is owned by
    this instance (not the shared default), it must be closed on shutdown —
    :meth:`aclose` does that, and :class:`WrappedEventBusProvider` calls it
    from ``aclose`` so the dedicated connection pool does not leak.
    """

    def __init__(
        self, options: EventBusOptions, *, read_timeout: float | None = None
    ) -> None:
        self._options = options
        self._read_timeout = read_timeout
        self._client: Any | None = None

    async def __call__(self) -> Any:
        if self._client is None:
            options = self._options
            if not options.url:
                raise ValueError("eventbus instance has no 'url' option")
            self._client = _redis_cache.build_transport_client(
                options.url,
                read_timeout=self._read_timeout,
                socket_connect_timeout_seconds=options.socket_connect_timeout_seconds,
                health_check_interval_seconds=options.health_check_interval_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        try:
            await client.aclose()
        except AttributeError:  # pragma: no cover - older redis-py
            await client.close()
        except Exception:  # noqa: BLE001, S110 - best-effort teardown
            pass


#: Slack over the Streams BLOCK window before a socket read times out.
_STREAMS_READ_MARGIN_SECONDS = 10.0


def _streams_read_timeout() -> float:
    """Socket read timeout of a Streams bus client.

    Its ``XREADGROUP BLOCK`` window plus a generous margin: no reply
    legitimately takes longer, so a half-open socket (Redis failover) raises
    and the consumer reconnects instead of hanging forever.
    """

    return StreamsEventBus._BLOCK_MS / 1000 + _STREAMS_READ_MARGIN_SECONDS


def _redis_client_factory(
    cfg: InstanceConfig, *, read_timeout: float | None = None
) -> tuple[Callable[[], Awaitable[Any]], _LazyRedisClient | None]:
    """Return ``(client_factory, closable)`` for THIS eventbus instance.

    The driver is the only layer that knows where the Redis client comes from,
    so it ALWAYS hands the bus/transport a factory — the transport classes never
    reach back into the driver themselves. A named instance with its own
    ``url`` gets its own lazily-connected client (the closable, shut down on
    teardown); the ``default`` instance, and one without a ``url``, shares the
    process-wide default transport client (the driver's
    ``get_default_transport_client``: a bounded sibling of the default cache
    client — on this instance's ``url`` when it has one — with ``read_timeout``
    instead of the cache read timeout, owned by the cache lifecycle — nothing
    for this provider to close).
    """
    options = EventBusOptions.model_validate(cfg.options)
    if not options.url:
        return partial(_redis_cache.get_default_transport_client, read_timeout), None
    if cfg.name == DEFAULT_INSTANCE:
        transport = _redis_cache.RedisCacheOptions(
            url=options.url,
            socket_connect_timeout_seconds=options.socket_connect_timeout_seconds,
            health_check_interval_seconds=options.health_check_interval_seconds,
        )
        # The default client may not be configured yet (the cache instance is
        # built lazily); never replaces what the default cache instance set.
        _redis_cache.configure_default_client(transport, replace=False)
        return (
            partial(_redis_cache.get_default_transport_client, read_timeout, transport),
            None,
        )
    lazy = _LazyRedisClient(options, read_timeout=read_timeout)
    return lazy, lazy


#: Per-driver delivery features. The in-memory and Redis pub/sub buses are
#: volatile (no persistence, fan-out to live subscribers, wildcard patterns);
#: the Redis Streams bus is durable, at-least-once and supports consumer groups
#: (but rejects wildcard subscriptions). A module declares the delivery
#: guarantee it needs so it never silently runs on a volatile bus.
EVENTBUS_DRIVER_FEATURES: dict[str, frozenset[str]] = {
    "memory": frozenset({"publish", "subscribe", "volatile", "wildcard_subscribe"}),
    "redis": frozenset({"publish", "subscribe", "volatile", "wildcard_subscribe"}),
    "streams": frozenset(
        {"publish", "subscribe", "durable", "at_least_once", "consumer_groups"}
    ),
}


class WrappedEventBusProvider(EventBusProvider):
    """Adapts an EventBusProtocol instance to the infra provider contract.

    Each driver name is its own subclass declaring its features; the bus needs
    the application's schema registry, so the registry builds them through
    :func:`make_event_bus_drivers` rather than ``from_config``.
    """

    contract_version = 1
    options_schema = EventBusOptions

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        bus_instance: Any,
        client: _LazyRedisClient | None = None,
        client_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._bus = bus_instance
        self._client = client
        self._health_client_factory = client_factory

    def bus(self) -> Any:
        return self._bus

    async def _probe(self) -> HealthResult | None:
        """One probe of the backend; ``None`` is healthy. A failure raises."""
        if self.driver != "memory" and self._health_client_factory is not None:
            client = await self._health_client_factory()
            await client.ping()
        return None

    async def health(self) -> HealthResult:
        try:
            result = await self._probe()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            # The type only: the text can carry the broker URL.
            return HealthResult(status="fail", details=health_failure_details(exc))
        return result or HealthResult(status="ok")

    async def aclose(self) -> None:
        close = getattr(self._bus, "close", None)
        if callable(close):
            await close()
        # Close the instance-owned Redis client (if any) so its connection pool
        # does not leak. The shared default client has no _LazyRedisClient and
        # is owned by the cache lifecycle.
        if self._client is not None:
            await self._client.aclose()


class MemoryEventBusProvider(WrappedEventBusProvider):
    """``eventbus`` / ``memory``: the in-process volatile bus."""

    supported_features = EVENTBUS_DRIVER_FEATURES["memory"]


class RedisEventBusProvider(WrappedEventBusProvider):
    """``eventbus`` / ``redis``: the volatile bus over a ``broadcast`` instance."""

    supported_features = EVENTBUS_DRIVER_FEATURES["redis"]
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """The bus has no ``url`` (it is refused): the ``broadcast`` instance it
        names isolates each xdist worker by channel prefix, so options pass
        through; kept so every Redis driver exposes the same hook."""
        return isolated_redis_options(options, worker_id)

    async def aopen(self) -> None:
        """Refuse a ``broadcast`` instance without ``pattern_subscribe``."""
        self._bus.check_broadcast()

    async def _probe(self) -> HealthResult | None:
        """The health of the ``broadcast`` instance the bus runs on."""
        probe = getattr(self._bus.broadcast_instance(), "health", None)
        if probe is None:
            return None
        result: HealthResult = await probe()
        return result


class StreamsEventBusProvider(WrappedEventBusProvider):
    """``eventbus`` / ``streams``: the durable Redis Streams bus."""

    supported_features = EVENTBUS_DRIVER_FEATURES["streams"]
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """Another database index of the configured URL for each xdist worker."""
        return isolated_redis_options(options, worker_id)


def make_event_bus_drivers(
    schema_registry: SchemaRegistryContract,
    *,
    volatile_allowed: Callable[[], bool],
) -> dict[str, DriverFactory]:
    """Return ``{driver_name: factory}`` for the memory, Redis and Streams buses.

    ``volatile_allowed`` is the kernel's policy, asked each time a volatile
    bus (``memory``, ``redis``) is built: ``False`` refuses it (production
    without the accepted risk ``volatile_event_bus``). It has no default, so
    no caller builds the drivers without stating it.
    """

    def _refuse_volatile_in_prod(cfg: InstanceConfig) -> None:
        if not volatile_allowed():
            raise RuntimeError(
                f"eventbus instance {cfg.name!r}: driver {cfg.driver!r} is "
                "volatile (no persistence; a Pub/Sub PUBLISH succeeds with zero "
                "subscribers, so the outbox would mark undelivered events as "
                "delivered). fix: use driver 'streams' (EVENT_BUS_BACKEND=streams) "
                "or accept lossy delivery explicitly with "
                'SANDLOOM_ACCEPTED_RISKS=["volatile_event_bus"]'
            )

    def _memory(cfg: InstanceConfig) -> WrappedEventBusProvider:
        _refuse_volatile_in_prod(cfg)
        return MemoryEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            bus_instance=InMemoryEventBus(schema_registry),
        )

    def _redis(cfg: InstanceConfig) -> WrappedEventBusProvider:
        _refuse_volatile_in_prod(cfg)
        options = EventBusOptions.model_validate(cfg.options)
        if options.url:
            raise RuntimeError(
                f"eventbus instance {cfg.name!r}: driver 'redis' takes its Redis "
                "from a broadcast instance, not from a 'url' (the bus would "
                "otherwise run outside that Redis's TLS, credentials and "
                "network). fix: declare INFRA broadcast.<name> with this url "
                'and set "broadcast": "<name>" on this instance'
            )
        return RedisEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            bus_instance=RedisEventBus(schema_registry, broadcast=options.broadcast),
        )

    def _streams(cfg: InstanceConfig) -> WrappedEventBusProvider:
        factory, closable = _redis_client_factory(
            cfg, read_timeout=_streams_read_timeout()
        )
        return StreamsEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            bus_instance=StreamsEventBus(
                schema_registry,
                RedisStreamsTransport(client_factory=factory),
            ),
            client=closable,
            client_factory=factory,
        )

    return {"memory": _memory, "redis": _redis, "streams": _streams}


__all__ = [
    "EventBusOptions",
    "MemoryEventBusProvider",
    "RedisEventBusProvider",
    "StreamsEventBusProvider",
    "WrappedEventBusProvider",
    "make_event_bus_drivers",
]
