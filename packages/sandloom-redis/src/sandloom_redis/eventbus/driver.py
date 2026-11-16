"""The built-in ``streams`` event-bus driver: a durable ``EventTransportProvider``.

``streams`` is the transport shape of the ``eventbus`` capability (spec 7):
this class IS the transport (Redis Streams) and the kernel builds its durable
bus over it. The volatile buses (``memory``, ``redis``) are kernel-internal
(:mod:`sandloom.core.eventing.volatile_provider`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any, Protocol, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field

import sandloom_redis.cache as _redis_cache
from sandloom.driverkit import (
    EventTransportProvider,
    HealthResult,
    InfraProvider,
    InstanceConfig,
    Unavailable,
    health_failure_details,
)
from sandloom.driverkit._builtin_streams import BUS_BLOCK_MS
from sandloom.driverkit.eventbus_support import (
    DEFAULT_EVENT_STREAM_RETENTION_SECONDS,
)
from sandloom.driverkit.shared_endpoints import redis_endpoint
from sandloom.driverkit.shared_endpoints import register as register_endpoint
from sandloom.driverkit.shared_endpoints import unregister as unregister_endpoint
from sandloom.driverkit.testing_isolation import isolated_redis_options
from sandloom_redis._settings_fill import require_url
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport
from sandloom_redis.eventbus.streams_transport_base import _mapped

#: The name of the ``default`` instance of a capability (the kernel's
#: ``DEFAULT_INSTANCE``): it shares the process-wide default transport client.
_DEFAULT_INSTANCE = "default"


class _InstanceLike(Protocol):
    name: str
    driver: str

    @property
    def options(self) -> Mapping[str, Any]: ...


class EventBusOptions(BaseModel):
    """Config-shape schema of the ``streams`` driver.

    ``url`` is the Redis DSN, with the connect timeout and health-check
    interval of its client (``INFRA.eventbus.<name>``; no setting fills them).
    The ``default`` instance shares the process-wide default transport client (a
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
    #: Both are written on the INFRA instance (``INFRA.eventbus.<name>``).
    retention_seconds: int = Field(default=DEFAULT_EVENT_STREAM_RETENTION_SECONDS, ge=0)
    max_length: int = Field(default=0, ge=0)
    #: Namespace prepended to every Streams key (streams, dead letters, group
    #: ownership, inventory) so several environments can share one Redis.
    #: Changing it starts a separate namespace.
    prefix: str = ""


class _LazyRedisClient:
    """A lazily-connected, instance-owned Redis client with explicit teardown.

    A named eventbus instance with its own ``url`` connects its own client on
    first use and caches it — a bounded transport client (connect timeout,
    keepalive, health check, the bus's read timeout; see
    ``sandloom_redis.cache.build_transport_client``). Because the
    client is owned by this instance (not the shared default), it must be
    closed on shutdown — :meth:`aclose` does that, and
    :class:`StreamsEventBusProvider` calls it from its own ``aclose`` so the
    dedicated connection pool does not leak.
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

    return BUS_BLOCK_MS / 1000 + _STREAMS_READ_MARGIN_SECONDS


def _redis_client_factory(
    cfg: _InstanceLike, *, read_timeout: float | None = None
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
    if cfg.name == _DEFAULT_INSTANCE:
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
    url_option = "url"
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"
    # Beta, the floor for a driver of this repository (spec 2.5): the
    # redis-drivers job runs the event-bus kit against a real Redis. The
    # entry point has no registration call, so an undeclared tier would be
    # experimental and refused in production (SANDLOOM-B023).
    tier = "beta"
    #: Set by the entry-point build (``_build_provider``) of an instance with no
    #: ``url`` of its own: it runs on the default cache client, which must have
    #: one (``aopen``). A provider built by hand brings its own client.
    _uses_default_client = False

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
            prefix=retention.prefix,
        )
        self._owned_client = client

    @classmethod
    def from_config(cls, config: InstanceConfig) -> Self:
        """The entry-point build: the instance's Redis client comes from the
        same builder the kernel's row uses (:func:`make_streams_driver`), and
        the result is ``cls``, so a subclass's ``from_config`` returns it."""
        return _build_provider(cls, config)

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """Another database index of the configured URL for each xdist worker."""
        return isolated_redis_options(options, worker_id)

    async def aopen(self) -> None:
        # An instance with no ``url`` runs on the default cache client (see
        # ``_redis_client_factory``): startup refuses only when that client has
        # no url either, naming this instance's INFRA option, before the first
        # connection (``InfraRegistry.aopen_all``). Nothing is opened here.
        if self._uses_default_client and not _redis_cache.has_default_client():
            require_url(
                "eventbus",
                self.name,
                _redis_cache.default_client_url(),
                fallback="the default cache client",
            )
        own = EventBusOptions.model_validate(self.options).url
        register_endpoint(
            "eventbus",
            self.name,
            redis_endpoint(
                _redis_cache.default_client_url() if self._uses_default_client else own
            ),
            "persistent",
            option=f"eventbus.{self.name}.url",
            declared=not self.implicit,
        )

    async def health(self) -> HealthResult:
        try:
            client = await self._client()
            await client.ping()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            # The type only: the text can carry the broker URL.
            return HealthResult(status="fail", details=health_failure_details(exc))
        return HealthResult(status="ok")

    @_mapped
    async def isolation_identity(self) -> str:
        """``streams:<run_id>``: the ``run_id`` of the reached Redis server from
        ``INFO server``, behind the driver name every identity starts with
        (spec 2.4, 2.7 item 6). Two buses on one server answer the same, however
        they reach it. Needs the Redis ACL permission for ``INFO server``. A
        startup diagnostic, never a per-request operation."""
        client = await self._client()
        info = await client.info(section="server")
        run_id = info.get("run_id") or info.get(b"run_id")
        if isinstance(run_id, bytes):
            run_id = run_id.decode("ascii", "replace")
        if not isinstance(run_id, str) or not run_id:
            raise Unavailable(
                f"eventbus {self.name!r}: INFO server returned no process identity",
                capability=self.capability,
                instance=self.name,
            )
        return f"{self.driver}:{run_id}"

    async def aclose(self) -> None:
        # Close the instance-owned Redis client (if any) so its connection pool
        # does not leak. The shared default client has no _LazyRedisClient and
        # is owned by the cache lifecycle.
        unregister_endpoint("eventbus", self.name, "persistent")
        if self._owned_client is not None:
            await self._owned_client.aclose()


_P = TypeVar("_P", bound=StreamsEventBusProvider)


def _build_provider(cls: type[_P], cfg: _InstanceLike) -> _P:
    """``cls`` over the Redis client the instance's options describe."""
    factory, closable = _redis_client_factory(cfg, read_timeout=_streams_read_timeout())
    provider = cls(
        name=cfg.name,
        driver=cfg.driver,
        options=dict(cfg.options),
        client=closable,
        client_factory=factory,
    )
    provider._uses_default_client = not EventBusOptions.model_validate(cfg.options).url
    return provider


def make_streams_driver() -> Callable[[_InstanceLike], InfraProvider]:
    """The factory of the ``streams`` driver (it needs no schema registry: the
    kernel builds the bus over the provider)."""
    return partial(_build_provider, StreamsEventBusProvider)


__all__ = [
    "EventBusOptions",
    "StreamsEventBusProvider",
    "make_streams_driver",
]
