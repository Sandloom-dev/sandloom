"""Built-in event-bus drivers (``memory`` and ``redis``).

The bus needs the schema registry at construction time, so these driver
factories are produced by :func:`make_event_bus_drivers`, which the bootstrap
calls with the application's schema registry. The resulting provider wraps an
``EventBusProtocol`` instance and participates in the infra lifecycle
(``aclose`` closes the bus).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from pydantic import BaseModel, ConfigDict

import sandloom.core.drivers.redis_cache as _redis_cache
from sandloom.core.config.settings import settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.descriptors import HealthResult
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import RedisEventBus
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import EventBusProvider
from sandloom.core.infra.registry import DriverFactory


class EventBusOptions(BaseModel):
    """Config-shape schema shared by the ``memory``/``redis``/``streams`` buses.

    The only option any built-in bus driver reads is ``url`` (the Redis DSN the
    ``redis``/``streams`` drivers connect their own client from; ``memory``
    ignores it). Optional — without it (the synthesized ``default`` instance)
    the instance shares the process-wide default transport client.
    ``extra="forbid"`` rejects a typo'd key at startup.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = None


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

    def __init__(self, url: str, *, read_timeout: float | None = None) -> None:
        self._url = url
        self._read_timeout = read_timeout
        self._client: Any | None = None

    async def __call__(self) -> Any:
        if self._client is None:
            self._client = _redis_cache.build_transport_client(
                self._url, read_timeout=self._read_timeout
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
    reach back into the driver themselves. A named instance with its own ``url``
    gets its own lazily-connected client (the closable, shut down on teardown);
    without a ``url`` (the ``default`` instance) it shares the process-wide
    default transport client (the driver's ``get_default_transport_client``: a
    bounded sibling of the default cache client with ``read_timeout`` instead
    of the cache read timeout, owned by the cache lifecycle — nothing for this
    provider to close).
    """
    url = cfg.options.get("url")
    if not url:
        return partial(_redis_cache.get_default_transport_client, read_timeout), None
    lazy = _LazyRedisClient(str(url), read_timeout=read_timeout)
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
    """Adapts an EventBusProtocol instance to the infra provider contract."""

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        bus_instance: Any,
        client: _LazyRedisClient | None = None,
        client_factory: Callable[[], Awaitable[Any]] | None = None,
        features: frozenset[str] | None = None,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._bus = bus_instance
        self._client = client
        self._health_client_factory = client_factory
        self._features = (
            features
            if features is not None
            else EVENTBUS_DRIVER_FEATURES.get(
                driver, frozenset({"publish", "subscribe"})
            )
        )

    def bus(self) -> Any:
        return self._bus

    def features(self) -> frozenset[str]:
        return self._features

    async def health(self) -> HealthResult:
        if self.driver != "memory" and self._health_client_factory is not None:
            try:
                client = await self._health_client_factory()
                await client.ping()
            except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
                return HealthResult(status="fail", details={"error": str(exc)})
        return HealthResult(status="ok")

    async def aclose(self) -> None:
        close = getattr(self._bus, "close", None)
        if callable(close):
            await close()
        # Close the instance-owned Redis client (if any) so its connection pool
        # does not leak. The shared default client has no _LazyRedisClient and
        # is owned by the cache lifecycle.
        if self._client is not None:
            await self._client.aclose()


def make_event_bus_drivers(
    schema_registry: SchemaRegistryContract,
) -> dict[str, DriverFactory]:
    """Return ``{driver_name: factory}`` for the memory, Redis and Streams buses."""

    def _refuse_volatile_in_prod(cfg: InstanceConfig) -> None:
        if settings.is_prod and not settings.accepts_risk("volatile_event_bus"):
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
        return WrappedEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            bus_instance=InMemoryEventBus(schema_registry),
        )

    def _redis(cfg: InstanceConfig) -> WrappedEventBusProvider:
        _refuse_volatile_in_prod(cfg)
        factory, closable = _redis_client_factory(cfg)
        return WrappedEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            bus_instance=RedisEventBus(schema_registry, client_factory=factory),
            client=closable,
            client_factory=factory,
        )

    def _streams(cfg: InstanceConfig) -> WrappedEventBusProvider:
        factory, closable = _redis_client_factory(
            cfg, read_timeout=_streams_read_timeout()
        )
        return WrappedEventBusProvider(
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


__all__ = ["WrappedEventBusProvider", "EventBusOptions", "make_event_bus_drivers"]
