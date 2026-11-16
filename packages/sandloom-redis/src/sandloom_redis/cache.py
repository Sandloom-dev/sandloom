"""The ``redis`` cache driver (entry point ``cache.redis``).

This driver is the SOLE constructor of Redis cache clients — including the
process-wide ``default`` one (:func:`get_default_client`), so "infrastructure
is constructed only inside the driver layer" holds. The single test/ops
injection seam is :func:`get_default_client` (patch it to inject a fake). The
``default`` instance uses that process-wide client, built from its options
(:func:`configure_default_client`); named instances own their own client
connected from ``options['url']``.

The driver reads no kernel setting: every value comes from the instance's
options (:class:`RedisCacheOptions`), which the kernel fills from the
``CACHE_*`` settings when the configuration leaves them out. Every cache client
is BOUNDED: socket + connect timeouts, TCP keepalive, a periodic health check,
a blocking pool capped at ``max_connections`` and a connection-error-only
retry. An unreachable or hung Redis therefore makes an operation RAISE within
roughly ``(1 + retry_attempts) x socket_timeout_seconds`` instead of hanging
the request, so the callers' fail-open / fail-closed branches run.

The event-bus transport must not share that client: blocking reads
(``XREADGROUP BLOCK``, pub/sub ``listen``) legitimately wait longer than the
cache read timeout. :func:`get_default_transport_client` hands it a sibling
client on the same URL with the transport's own read timeout — none for
pub/sub, longer than the BLOCK window for Streams — but the same connect
timeout, keepalive and health check (an injected fake default client is shared
as is, so the single injection seam still covers both).

Multi-step operations run as Lua scripts via ``EVALSHA`` (the script body is
sent once per server: a ``NOSCRIPT`` reply loads it and retries).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from functools import cache
from types import MappingProxyType
from typing import Any, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import (
    DEFAULT_INSTANCE,
    CacheProvider,
    HealthResult,
    InfraError,
    InstanceConfig,
    InvalidArgument,
    Unavailable,
    health_failure_details,
    observe,
)
from sandloom.driverkit.atomic_store import MAX_KEY_BYTES, MAX_TTL_SECONDS
from sandloom.driverkit.testing_isolation import isolated_redis_options
from sandloom_redis._errors import map_redis_error
from sandloom_redis._settings_fill import CACHE_FILL, require_url

#: Redis / socket failures of cache operations (a caller's ValueError or
#: TypeError is not counted), by instance and operation.
cache_operation_errors_total = observe.counter(
    "sandloom_cache_operation_errors_total",
    "Cache operations that raised (connection, timeout, server error)",
    ("instance", "operation"),
)

#: Features the Redis cache driver supports.
REDIS_CACHE_FEATURES = frozenset({"cas"})

#: The batch and fused operations the Redis driver does in one round trip.
REDIS_CACHE_PERFORMANCE_HINTS = MappingProxyType(
    dict.fromkeys(("get_many", "set_many", "windowed_counter"), "one_round_trip")
)


class RedisCacheOptions(BaseModel):
    """Config-shape schema for the ``redis`` cache driver.

    ``url`` is the Redis DSN; the bounds are those of the client (see the
    module docstring). The kernel fills ``url`` and every bound left out from
    the ``CACHE_*`` settings, so the defaults here only apply to a provider
    built outside the kernel. ``extra="forbid"`` rejects an unknown key (e.g.
    ``ur1`` for ``url``) at startup instead of silently building the default.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    url: str | None = None
    socket_timeout_seconds: float = Field(default=0.5, gt=0)
    socket_connect_timeout_seconds: float = Field(default=0.5, gt=0)
    health_check_interval_seconds: int = Field(default=30, ge=0)
    max_connections: int = Field(default=64, ge=1)
    pool_timeout_seconds: float = Field(default=1.0, gt=0)
    retry_attempts: int = Field(default=1, ge=0, le=5)


logger = logging.getLogger(__name__)

#: Process-wide ``default`` cache client, built lazily from
#: :data:`_default_options`.
_default_client: Any | None = None
#: ``(client, loop)`` of the default client THIS driver built — a redis.asyncio
#: client (and its pool) is bound to the event loop it first ran on.
_default_client_origin: tuple[Any, asyncio.AbstractEventLoop] | None = None
#: Options of the ``default`` instance (:func:`configure_default_client`).
_default_options: RedisCacheOptions | None = None
#: The options the driver-built default client was built from.
_default_built_from: RedisCacheOptions | None = None
#: ``(read timeout, options) -> (paired default client, transport client)`` —
#: see :func:`get_default_transport_client`.
_default_transport: dict[tuple[float | None, RedisCacheOptions], tuple[Any, Any]] = {}


def configure_default_client(
    options: RedisCacheOptions, *, replace: bool = True
) -> None:
    """Set the options the process-wide ``default`` client is built from.

    The ``default`` cache instance sets them when it is built (``replace``);
    the ``default`` event bus instance sets them only when nothing has
    (``replace=False``). A default client built from other options is
    rebuilt on its next use.
    """
    global _default_options
    if _default_options is None or (replace and options != _default_options):
        _default_options = options


def default_client_url() -> str | None:
    """The url the process-wide ``default`` client is configured with, or
    ``None`` (no options yet, or options with no url)."""
    return _default_options.url if _default_options is not None else None


def has_default_client() -> bool:
    """Whether a default client already exists (one injected for a test or an
    operator, or built): it needs no url of its own."""
    return _default_client is not None


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:  # called outside a loop (sync caller)
        return None


async def _close_quietly(client: Any) -> None:
    """Best-effort close of a client whose loop may be gone (never raises)."""
    try:
        closer = getattr(client, "aclose", None) or client.close
        await asyncio.wait_for(closer(), timeout=1.0)
    except Exception:  # its loop is closed; just drop it
        logger.debug("stale redis client close failed", exc_info=True)


def client_options(options: RedisCacheOptions) -> dict[str, Any]:
    """Bounded-client connection options from an instance's ``options``."""
    from redis.asyncio.retry import Retry
    from redis.backoff import FullJitterBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError

    return {
        "socket_timeout": float(options.socket_timeout_seconds),
        "socket_connect_timeout": float(options.socket_connect_timeout_seconds),
        "socket_keepalive": True,
        "health_check_interval": int(options.health_check_interval_seconds),
        # Retry ONLY connection errors (a stale pooled socket after a Redis
        # restart / failover) — never timeouts, which would multiply the bound.
        "retry": Retry(
            FullJitterBackoff(cap=0.1, base=0.01),
            int(options.retry_attempts),
            supported_errors=(RedisConnectionError,),
        ),
    }


def _url_of(options: RedisCacheOptions, instance: str = DEFAULT_INSTANCE) -> str:
    return require_url("cache", instance, options.url)


def _build_client(options: RedisCacheOptions) -> Any:
    """A bounded cache client: timeouts, keepalive, capped BLOCKING pool.

    When every pooled connection is busy a caller waits up to
    ``pool_timeout_seconds`` for one, then gets a ``ConnectionError`` (never an
    unbounded pile-up of sockets).
    """
    import redis.asyncio as redis

    pool: Any = redis.BlockingConnectionPool.from_url(
        _url_of(options),
        max_connections=int(options.max_connections),
        timeout=float(options.pool_timeout_seconds),
        encoding="utf-8",
        decode_responses=True,
        **client_options(options),
    )
    redis_cls: Any = redis.Redis  # (stubs predate ``from_pool``)
    return redis_cls.from_pool(pool)


def build_transport_client(
    url: str,
    *,
    read_timeout: float | None = None,
    socket_connect_timeout_seconds: float = 0.5,
    health_check_interval_seconds: int = 30,
) -> Any:
    """A client for event-bus transport reads.

    Connect timeout, TCP keepalive and periodic health check like the cache
    client, but the read timeout is the transport's: ``None`` for pub/sub
    ``listen`` (idles indefinitely), longer than the ``BLOCK`` window for
    Streams, so a half-open socket after a failover raises instead of hanging
    the consumer forever. No command retry: ``XADD`` is not idempotent.
    """
    import redis.asyncio as redis

    return redis.from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=read_timeout,
        socket_connect_timeout=float(socket_connect_timeout_seconds),
        socket_keepalive=True,
        health_check_interval=int(health_check_interval_seconds),
    )


def _script_sha(script: str) -> str:
    return hashlib.sha1(  # Redis EVALSHA key, not security
        script.encode("utf-8"), usedforsecurity=False
    ).hexdigest()


#: The no-op script ``health`` runs to prove the server accepts scripting.
_PROBE_LUA = "return 1"
_PROBE_SHA = _script_sha(_PROBE_LUA)


async def _run_script(
    client: Any, script: str, sha: str, keys: Sequence[str], args: Sequence[Any]
) -> Any:
    """``EVALSHA`` the script; on ``NOSCRIPT`` load it once and retry.

    There is no non-atomic fallback: a client without scripting fails here, so
    a test double must implement ``evalsha`` and ``script_load``.
    """
    from redis.exceptions import NoScriptError

    evalsha = client.evalsha
    try:
        return await evalsha(sha, len(keys), *keys, *args)
    except NoScriptError:
        await client.script_load(script)
        return await evalsha(sha, len(keys), *keys, *args)


async def get_default_client() -> Any:
    """Return the process-wide ``default`` Redis client (built once per loop).

    The single construction site for the default cache backend and the single
    test/ops injection seam — patch this function to substitute a fake.

    Loop-aware: a redis.asyncio client is bound to the event loop it first
    ran on, so when the running loop differs from the one the cached client
    was built on (each pytest-asyncio test gets its own loop; a CLI may call
    ``asyncio.run`` twice) the stale client is closed best-effort and a fresh
    one is built — no "Event loop is closed" on the next call. So is a client
    built from options other than :func:`configure_default_client` set last.
    A client injected by assigning ``_default_client`` directly is never
    replaced.
    """
    global _default_client, _default_client_origin, _default_built_from
    loop = _running_loop()
    if (
        _default_client is not None
        and _default_client_origin is not None
        and _default_client_origin[0] is _default_client
        and (
            (loop is not None and _default_client_origin[1] is not loop)
            or _default_built_from is not _default_options
        )
    ):
        stale, _default_client, _default_client_origin = _default_client, None, None
        await _close_quietly(stale)
    if _default_client is None:
        options = _default_options
        if options is None:
            raise RuntimeError(
                "the default redis cache client has no options: the kernel sets "
                "them when it builds the default cache instance "
                "(configure_default_client)"
            )
        _default_client = _build_client(options)
        _default_built_from = options
        _default_client_origin = (_default_client, loop) if loop is not None else None
    return _default_client


async def get_default_transport_client(
    read_timeout: float | None = None, options: RedisCacheOptions | None = None
) -> Any:
    """Client for the default event-bus transport (blocking reads allowed).

    Resolves :func:`get_default_client` (the single injection seam): an
    injected / patched client is returned as is, while the driver-built
    default gets a sibling client (one per ``read_timeout`` and ``options``,
    see :func:`build_transport_client`) — a blocking ``XREADGROUP`` / pub-sub
    ``listen`` must not trip the cache's short socket timeout. The sibling
    connects to ``options.url`` with its connect timeout and health check
    (the default client's options when ``None``) and follows the default's
    loop affinity.
    """

    main = await get_default_client()
    origin = _default_client_origin
    if origin is None or origin[0] is not main:
        return main  # injected / patched: share it
    own = options if options is not None else _default_built_from
    if own is None:  # pragma: no cover - set whenever the driver built main
        return main
    key = (read_timeout, own)
    paired = _default_transport.get(key)
    if paired is not None and paired[0] is main:
        return paired[1]
    # Check, build and publish with no await in between: a concurrent caller
    # then finds this sibling instead of building (and leaking) its own. The
    # stale sibling is closed only after the replacement is published.
    transport = build_transport_client(
        _url_of(own),
        read_timeout=read_timeout,
        socket_connect_timeout_seconds=own.socket_connect_timeout_seconds,
        health_check_interval_seconds=own.health_check_interval_seconds,
    )
    _default_transport[key] = (main, transport)
    if paired is not None:
        await _close_quietly(paired[1])
    return transport


async def close_default_client() -> None:
    """Dispose the process-wide ``default`` cache client (idempotent)."""
    global _default_client, _default_client_origin
    while _default_transport:
        _, (_, transport) = _default_transport.popitem()
        await _close_quietly(transport)
    if _default_client is not None:
        client, _default_client = _default_client, None
        origin, _default_client_origin = _default_client_origin, None
        if (
            origin is not None
            and origin[0] is client
            and origin[1] is not (_running_loop())
        ):
            # Built on another (likely closed) loop: it cannot be closed
            # cleanly from here — release it best-effort.
            await _close_quietly(client)
            return
        try:
            await client.aclose()
        except AttributeError:  # pragma: no cover - older redis-py
            await client.close()


def _check_key(instance: str, key: object) -> None:
    """``InvalidArgument`` unless ``key`` is a non-empty string of at most
    ``MAX_KEY_BYTES`` UTF-8 bytes: the memory driver's rule, so both drivers
    refuse the same keys (spec 3)."""
    if not isinstance(key, str) or not key:
        raise InvalidArgument(
            "a cache key is a non-empty string", capability="cache", instance=instance
        )
    if key.isascii():  # the common case: one byte a character, no encoding
        size = len(key)
    else:
        try:
            size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            raise InvalidArgument(
                "a cache key must be encodable as UTF-8",
                capability="cache",
                instance=instance,
            ) from None
    if size > MAX_KEY_BYTES:
        raise InvalidArgument(
            f"a cache key is at most {MAX_KEY_BYTES} bytes",
            capability="cache",
            instance=instance,
        )


def _check_ttl(instance: str, ttl_seconds: object) -> None:
    """``InvalidArgument`` unless ``ttl_seconds`` is ``None`` (no expiry) or a
    whole number of seconds from 1 to ``MAX_TTL_SECONDS``: ``0`` or a negative
    TTL never means "no expiry" (the memory driver's rule, spec 3)."""
    if ttl_seconds is None:
        return
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= MAX_TTL_SECONDS
    ):
        raise InvalidArgument(
            f"a cache TTL is a whole number of seconds from 1 to {MAX_TTL_SECONDS}",
            capability="cache",
            instance=instance,
        )


_C = TypeVar("_C", bound="RedisCacheProvider")


class RedisCacheProvider(CacheProvider):
    """A single Redis cache instance."""

    contract_version = 2
    supported_features = REDIS_CACHE_FEATURES
    performance_hints = REDIS_CACHE_PERFORMANCE_HINTS
    options_schema = RedisCacheOptions
    settings_fill = CACHE_FILL
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"
    # Beta, the floor for a driver of this repository (spec 2.5): the
    # `redis-drivers` job of ci.yml runs CacheConformance against a real Redis.
    # An undeclared tier would be experimental.
    tier = "beta"

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """Another database index of the configured URL for each xdist worker."""
        return isolated_redis_options(options, worker_id)

    @classmethod
    def from_config(cls, config: InstanceConfig) -> Self:
        return _build_redis_cache(cls, config)

    async def aopen(self) -> None:
        # Startup refuses an instance with no url, naming its INFRA option,
        # before the first connection (``InfraRegistry.aopen_all``). It opens
        # no connection itself: a provider built by hand passes its url, or a
        # client, as before.
        _url_of(RedisCacheOptions.model_validate(self.options), self.name)

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        shared: bool,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._shared = shared
        self._client: Any | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    # INCR + EXPIRE in one atomic script so a crash between the two can never
    # leave a counter key without a TTL (it would leak forever).
    _INCR_WITH_TTL_LUA = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 and tonumber(ARGV[1]) > 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return count
    """
    _INCR_WITH_TTL_SHA = _script_sha(_INCR_WITH_TTL_LUA)

    # Sliding-window rate-limit step in ONE round trip: INCR (+EXPIRE on the
    # first hit) the current window and read the previous window's counter.
    _INCR_WINDOW_LUA = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 and tonumber(ARGV[1]) > 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return {count, redis.call('GET', KEYS[2])}
    """
    _INCR_WINDOW_SHA = _script_sha(_INCR_WINDOW_LUA)

    async def _client_obj(self) -> Any:
        if self._shared:
            # The default instance reuses the one process-wide client so there
            # is a single connection pool (and a single injection seam).
            return await get_default_client()
        loop = _running_loop()
        if (
            self._client is not None
            and loop is not None
            and self._client_loop is not None
            and self._client_loop is not loop
        ):
            # Same loop-affinity rule as the default client (see above).
            stale, self._client = self._client, None
            await _close_quietly(stale)
        if self._client is None:
            options = RedisCacheOptions.model_validate(self.options)
            _url_of(options, self.name)  # names this instance's INFRA option
            self._client = _build_client(options)
            self._client_loop = loop
        return self._client

    async def redis_client(self) -> Any:
        """The driver's Redis client. NOT part of the ``cache`` contract and not
        portable: for this driver's own tests and the test harness (flushing the
        test database, counting round trips), never for module or kernel code."""
        return await self._client_obj()

    # ---- portable operations ------------------------------------------------
    async def get(self, key: str) -> str | None:
        _check_key(self.name, key)
        try:
            client = await self._client_obj()
            value = await client.get(key)
            return value if value is None else str(value)
        except _backend_errors() as exc:
            raise _map_error(self.name, "get", exc) from None

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        _check_key(self.name, key)
        _check_ttl(self.name, ttl_seconds)
        try:
            client = await self._client_obj()
            if ttl_seconds is not None:
                await client.set(key, value, ex=ttl_seconds)
            else:
                await client.set(key, value)
        except _backend_errors() as exc:
            raise _map_error(self.name, "set", exc) from None

    async def get_many(self, keys: Sequence[str]) -> list[str | None]:
        """One ``MGET`` round trip for every key."""
        for key in keys:
            _check_key(self.name, key)
        try:
            if not keys:
                return []
            client = await self._client_obj()
            values = await client.mget(list(keys))
            return [v if v is None else str(v) for v in values]
        except _backend_errors() as exc:
            raise _map_error(self.name, "get_many", exc) from None

    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None:
        """Every ``SET`` in ONE pipelined round trip (not a MULTI transaction)."""
        for key in items:
            _check_key(self.name, key)
        _check_ttl(self.name, ttl_seconds)
        try:
            if not items:
                return
            client = await self._client_obj()
            ttl = ttl_seconds
            async with client.pipeline(transaction=False) as pipe:
                for key, value in items.items():
                    if ttl is not None:
                        pipe.set(key, value, ex=ttl)
                    else:
                        pipe.set(key, value)
                await pipe.execute()
        except _backend_errors() as exc:
            raise _map_error(self.name, "set_many", exc) from None

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        _check_key(self.name, key)
        _check_ttl(self.name, ttl_seconds)
        try:
            client = await self._client_obj()
            if ttl_seconds is not None:
                stored = await client.set(key, value, nx=True, ex=ttl_seconds)
            else:
                stored = await client.set(key, value, nx=True)
            return bool(stored)
        except _backend_errors() as exc:
            raise _map_error(self.name, "add", exc) from None

    async def delete(self, key: str) -> None:
        _check_key(self.name, key)
        try:
            client = await self._client_obj()
            await client.delete(key)
        except _backend_errors() as exc:
            raise _map_error(self.name, "delete", exc) from None

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        _check_key(self.name, key)
        _check_ttl(self.name, ttl_seconds)
        try:
            client = await self._client_obj()
            ttl = ttl_seconds or 0
            # Atomic INCR+EXPIRE (Lua): never two separate commands.
            return int(
                await _run_script(
                    client,
                    self._INCR_WITH_TTL_LUA,
                    self._INCR_WITH_TTL_SHA,
                    [key],
                    [ttl],
                )
            )
        except _backend_errors() as exc:
            raise _map_error(self.name, "incr", exc) from None

    async def windowed_counter(
        self, key: str, previous_key: str, *, ttl_seconds: int
    ) -> tuple[int, str | None]:
        _check_key(self.name, key)
        _check_key(self.name, previous_key)
        _check_ttl(self.name, ttl_seconds)
        try:
            client = await self._client_obj()
            ttl = ttl_seconds or 0
            count, previous = await _run_script(
                client,
                self._INCR_WINDOW_LUA,
                self._INCR_WINDOW_SHA,
                [key, previous_key],
                [ttl],
            )
            return int(count), (None if previous is None else str(previous))
        except _backend_errors() as exc:
            raise _map_error(self.name, "windowed_counter", exc) from None

    # Compare-and-set / compare-and-delete as single Lua scripts (atomic on the
    # server): the value is only replaced/removed when it still equals the
    # caller's expected value (an owner token embedded in it).
    _CAS_LUA = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        if tonumber(ARGV[3]) > 0 then
            redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
        else
            redis.call('SET', KEYS[1], ARGV[2])
        end
        return 1
    end
    return 0
    """
    _CAS_SHA = _script_sha(_CAS_LUA)
    _CAD_LUA = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    _CAD_SHA = _script_sha(_CAD_LUA)

    async def compare_and_set(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        _check_key(self.name, key)
        _check_ttl(self.name, ttl_seconds)
        try:
            client = await self._client_obj()
            ttl = ttl_seconds or 0
            return bool(
                int(
                    await _run_script(
                        client,
                        self._CAS_LUA,
                        self._CAS_SHA,
                        [key],
                        [expected, value, ttl],
                    )
                )
            )
        except _backend_errors() as exc:
            raise _map_error(self.name, "compare_and_set", exc) from None

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        _check_key(self.name, key)
        try:
            client = await self._client_obj()
            return bool(
                int(
                    await _run_script(
                        client, self._CAD_LUA, self._CAD_SHA, [key], [expected]
                    )
                )
            )
        except _backend_errors() as exc:
            raise _map_error(self.name, "compare_and_delete", exc) from None

    async def health(self) -> HealthResult:
        try:
            client = await self._client_obj()
            pong = await client.ping()
            if not pong:
                return HealthResult(status="fail", details={"error": "no-pong"})
            # ``incr``, ``windowed_counter`` and the CAS calls are scripts: a
            # server (an ACL without ``@scripting``, a proxy that blocks
            # ``EVALSHA``) that answers PING but refuses them is not healthy,
            # or the rate limits would fail open behind a green probe.
            await _run_script(client, _PROBE_LUA, _PROBE_SHA, [], [])
            return HealthResult(status="ok")
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            # The type only: a redis error text names the host (spec 2.4).
            return HealthResult(status="fail", details=health_failure_details(exc))

    async def isolation_identity(self) -> str:
        """``redis:<run_id>`` of the reached server, in the format of the Redis
        atomic store's ``isolation_identity`` (spec 12): the strict profile
        compares the two to refuse a store that shares the cache's server (and
        so its eviction policy). Attests the process, including through DNS
        aliases; needs the Redis ACL permission for ``INFO server``. A startup
        diagnostic, never a per-request operation."""
        client = await self._client_obj()
        info = await client.info(section="server")
        identity = info.get("run_id") or info.get(b"run_id")
        if isinstance(identity, bytes):
            identity = identity.decode("ascii")
        if not isinstance(identity, str) or not identity:
            raise Unavailable(
                f"cache {self.name!r}: INFO server returned no process identity",
                capability=self.capability,
                instance=self.name,
            )
        return f"redis:{identity}"

    async def aclose(self) -> None:
        if self._shared:
            # The shared process-wide client is closed via close_default_client
            # (main's lifecycle), not here, to avoid double-close.
            return
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.aclose()
            except AttributeError:  # pragma: no cover - older redis-py
                await client.close()


@cache
def _backend_errors() -> tuple[type[BaseException], ...]:
    """What a Redis backend raises: redis-py's errors, socket errors, a timeout
    and an undecodable stored value. Nothing else is caught: any other
    exception is a caller's or a driver bug and propagates (spec 2.1)."""
    from redis import exceptions

    return (exceptions.RedisError, OSError, TimeoutError, UnicodeDecodeError)


def _map_error(instance: str, operation: str, exc: BaseException) -> InfraError:
    """The ``InfraError`` for a Redis / socket failure (one of
    :func:`_backend_errors`), counted.

    The error carries its type, never the backend's text (a Redis error names
    the host). Called from an ``except`` clause in each operation: a ``try``
    costs nothing on success, unlike a wrapping coroutine (one frame per call).
    """
    from redis import exceptions

    if isinstance(exc, exceptions.ResponseError) and "not an integer" in str(exc):
        return InvalidArgument(
            f"cache {instance!r} {operation}: the stored value is not an integer",
            capability="cache",
            instance=instance,
        )
    if isinstance(exc, exceptions.ResponseError) and str(exc).startswith("WRONGTYPE"):
        # the key holds another Redis type (a list, a hash): retrying never helps
        return InvalidArgument(
            f"cache {instance!r} {operation}: the key holds a value of another type",
            capability="cache",
            instance=instance,
        )
    cache_operation_errors_total.labels(instance, operation).inc()
    # ``from None`` at the call sites: a request unit branches on the class only.
    return map_redis_error(
        exc,
        capability="cache",
        instance=instance,
        what=f"cache {instance!r} {operation}",
    )


def create_redis_cache(cfg: InstanceConfig) -> RedisCacheProvider:
    """Driver factory for ``cache`` capability / ``redis`` driver.

    The ``default`` instance uses the process-wide default client, built from
    its options; a named instance builds its own.
    """
    return _build_redis_cache(RedisCacheProvider, cfg)


def _build_redis_cache(cls: type[_C], cfg: InstanceConfig) -> _C:
    """``cls`` over the client the instance's options describe (so a
    subclass's ``from_config`` returns the subclass)."""
    shared = cfg.name == DEFAULT_INSTANCE
    if shared:
        configure_default_client(RedisCacheOptions.model_validate(cfg.options))
    return cls(
        name=cfg.name,
        driver=cfg.driver,
        options=dict(cfg.options),
        shared=shared,
    )


__all__ = [
    "RedisCacheProvider",
    "RedisCacheOptions",
    "create_redis_cache",
    "client_options",
    "configure_default_client",
    "get_default_client",
    "build_transport_client",
    "get_default_transport_client",
    "close_default_client",
]
