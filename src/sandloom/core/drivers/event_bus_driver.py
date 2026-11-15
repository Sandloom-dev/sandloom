"""The built-in ``streams`` event-bus driver: a durable ``EventTransportProvider``.

``streams`` is the transport shape of the ``eventbus`` capability (spec 7):
this class IS the transport (Redis Streams) and the kernel builds its durable
bus over it. The volatile buses (``memory``, ``redis``) are kernel-internal
(:mod:`sandloom.core.eventing.volatile_provider`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

import sandloom.core.drivers.redis_cache as _redis_cache
from sandloom.core.descriptors import HealthResult
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.infra.config import (
    DEFAULT_EVENT_STREAM_RETENTION_SECONDS,
    DEFAULT_INSTANCE,
    InstanceConfig,
)
from sandloom.core.infra.ports._base import health_failure_details
from sandloom.core.infra.registry import DriverFactory
from sandloom.driverkit import EventTransportProvider, InfraProvider
from sandloom.driverkit.testing_isolation import isolated_redis_options


class EventBusOptions(BaseModel):
    """Config-shape schema of the ``streams`` driver.

    ``url`` is the Redis DSN, with the connect timeout and health-check
    interval of its client. The kernel fills all three from ``CACHE_URL`` and
    the ``CACHE_*`` settings when the configuration leaves them out. The
    ``default`` instance shares the process-wide default transport client (a
    sibling of the default cache client on its own ``url``); a named instance
    connects its own client. ``extra="forbid"`` rejects a typo'd key at
    startup.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = None
    socket_connect_timeout_seconds: float = Field(default=0.5, gt=0)
    health_check_interval_seconds: int = Field(default=30, ge=0)
    #: Retention of every stream (``retention()``): entries older than
    #: ``retention_seconds`` are trimmed at publish, and, when ``max_length`` is
    #: positive, entries beyond that count. Zero disables the respective bound.
    #: The kernel fills both from ``EVENT_STREAM_RETENTION_SECONDS`` and
    #: ``EVENT_STREAM_MAXLEN`` until those settings become options.
    retention_seconds: int = Field(default=DEFAULT_EVENT_STREAM_RETENTION_SECONDS, ge=0)
    max_length: int = Field(default=0, ge=0)


class _LazyRedisClient:
    """A lazily-connected, instance-owned Redis client with explicit teardown.

    A named eventbus instance with its own ``url`` connects its own client on
    first use and caches it — a bounded transport client (connect timeout,
    keepalive, health check, the bus's read timeout; see
    ``redis_cache.build_transport_client``). Because the client is owned by
    this instance (not the shared default), it must be closed on shutdown —
    :meth:`aclose` does that, and :class:`StreamsEventBusProvider` calls it
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


class StreamsEventBusProvider(RedisStreamsTransport, EventTransportProvider):
    """``eventbus`` / ``streams``: the durable Redis Streams transport.

    The kernel builds its durable bus (``StreamsEventBus``) over an instance;
    the transport, its features and its lifecycle are this one class.
    """

    contract_version = 2
    options_schema = EventBusOptions
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: Mapping[str, Any],
        client: _LazyRedisClient | None = None,
        client_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        InfraProvider.__init__(self, name=name, driver=driver, options=options)
        retention = EventBusOptions.model_validate(options)
        RedisStreamsTransport.__init__(
            self,
            client_factory=client_factory,
            retention_seconds=retention.retention_seconds,
            maxlen=retention.max_length,
        )
        self._owned_client = client

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """Another database index of the configured URL for each xdist worker."""
        return isolated_redis_options(options, worker_id)

    async def health(self) -> HealthResult:
        try:
            client = await self._client()
            await client.ping()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            # The type only: the text can carry the broker URL.
            return HealthResult(status="fail", details=health_failure_details(exc))
        return HealthResult(status="ok")

    async def aclose(self) -> None:
        # Close the instance-owned Redis client (if any) so its connection pool
        # does not leak. The shared default client has no _LazyRedisClient and
        # is owned by the cache lifecycle.
        if self._owned_client is not None:
            await self._owned_client.aclose()


def make_streams_driver() -> DriverFactory:
    """The factory of the ``streams`` driver (it needs no schema registry: the
    kernel builds the bus over the provider)."""

    def _streams(cfg: InstanceConfig) -> InfraProvider:
        factory, closable = _redis_client_factory(
            cfg, read_timeout=_streams_read_timeout()
        )
        return StreamsEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            client=closable,
            client_factory=factory,
        )

    return _streams


__all__ = [
    "EventBusOptions",
    "StreamsEventBusProvider",
    "make_streams_driver",
]
