"""Built-in Redis cache driver.

This driver is the SOLE constructor of Redis cache clients — including the
process-wide ``default`` one (:func:`get_default_client`), so "infrastructure
is constructed only inside the driver layer" holds. The single test/ops
injection seam is :func:`get_default_client` (patch it to inject a fake). Named
instances own their own client connected from ``options['url']``.

Every cache client is BOUNDED (``CACHE_*`` settings): socket + connect
timeouts, TCP keepalive, a periodic health check, a blocking pool capped at
``CACHE_MAX_CONNECTIONS`` and a connection-error-only retry. An unreachable or
hung Redis therefore makes an operation RAISE within roughly
``(1 + CACHE_RETRY_ATTEMPTS) x CACHE_SOCKET_TIMEOUT_SECONDS`` instead of
hanging the request, so the callers' fail-open / fail-closed branches run.

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
from typing import Any

from pydantic import BaseModel, ConfigDict

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import (
    CacheProvider,
    parse_plain_decimal,
    parse_subject_state,
)
from sandloom.core.observability.metrics import cache_operation_errors_total

#: Features the Redis cache driver supports (the fused / batch ops are one
#: round trip).
REDIS_CACHE_FEATURES = frozenset(
    {"get", "set", "add", "delete", "incr", "cas", "get_many", "set_many"}
    | {"incr_window", "incr_many", "set_nx_get", "set_max", "subject_state"}
)


class RedisCacheOptions(BaseModel):
    """Config-shape schema for the ``redis`` cache driver.

    The driver only reads ``url`` (falling back to ``CACHE_URL`` when omitted);
    ``extra="forbid"`` rejects an unknown key (e.g. ``ur1`` for ``url``) at
    startup instead of silently building the flat default.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = None


logger = logging.getLogger(__name__)

#: Process-wide ``default`` cache client, built lazily from ``CACHE_URL``.
_default_client: Any | None = None
#: ``(client, loop)`` of the default client THIS driver built — a redis.asyncio
#: client (and its pool) is bound to the event loop it first ran on.
_default_client_origin: tuple[Any, asyncio.AbstractEventLoop] | None = None
#: ``read timeout -> (paired default client, transport client)`` — see
#: :func:`get_default_transport_client`.
_default_transport: dict[float | None, tuple[Any, Any]] = {}


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


def client_options() -> dict[str, Any]:
    """Bounded-client connection options from the ``CACHE_*`` settings."""
    from redis.asyncio.retry import Retry  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client
    from redis.backoff import FullJitterBackoff  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client
    from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client

    return {
        "socket_timeout": float(settings.cache_socket_timeout_seconds),
        "socket_connect_timeout": float(settings.cache_socket_connect_timeout_seconds),
        "socket_keepalive": True,
        "health_check_interval": int(settings.cache_health_check_interval_seconds),
        # Retry ONLY connection errors (a stale pooled socket after a Redis
        # restart / failover) — never timeouts, which would multiply the bound.
        "retry": Retry(
            FullJitterBackoff(cap=0.1, base=0.01),
            int(settings.cache_retry_attempts),
            supported_errors=(RedisConnectionError,),
        ),
    }


def _build_client(url: str) -> Any:
    """A bounded cache client: timeouts, keepalive, capped BLOCKING pool.

    When every pooled connection is busy a caller waits up to
    ``CACHE_POOL_TIMEOUT_SECONDS`` for one, then gets a ``ConnectionError``
    (never an unbounded pile-up of sockets).
    """
    import redis.asyncio as redis  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client

    pool: Any = redis.BlockingConnectionPool.from_url(
        url,
        max_connections=int(settings.cache_max_connections),
        timeout=float(settings.cache_pool_timeout_seconds),
        encoding="utf-8",
        decode_responses=True,
        **client_options(),
    )
    redis_cls: Any = redis.Redis  # (stubs predate ``from_pool``)
    return redis_cls.from_pool(pool)


def build_transport_client(url: str, *, read_timeout: float | None = None) -> Any:
    """A client for event-bus transport reads.

    Connect timeout, TCP keepalive and periodic health check like the cache
    client, but the read timeout is the transport's: ``None`` for pub/sub
    ``listen`` (idles indefinitely), longer than the ``BLOCK`` window for
    Streams, so a half-open socket after a failover raises instead of hanging
    the consumer forever. No command retry: ``XADD`` is not idempotent.
    """
    import redis.asyncio as redis  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client

    return redis.from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=read_timeout,
        socket_connect_timeout=float(settings.cache_socket_connect_timeout_seconds),
        socket_keepalive=True,
        health_check_interval=int(settings.cache_health_check_interval_seconds),
    )


def _script_sha(script: str) -> str:
    return hashlib.sha1(  # Redis EVALSHA key, not security
        script.encode("utf-8"), usedforsecurity=False
    ).hexdigest()


async def _run_script(
    client: Any, script: str, sha: str, keys: Sequence[str], args: Sequence[Any]
) -> Any:
    """``EVALSHA`` the script; on ``NOSCRIPT`` load it once and retry.

    A client without scripting (test double) raises ``AttributeError`` /
    ``NotImplementedError`` so the caller falls back to plain commands.
    """
    from redis.exceptions import NoScriptError  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client

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
    one is built — no "Event loop is closed" on the next call. A client
    injected by assigning ``_default_client`` directly is never replaced.
    """
    global _default_client, _default_client_origin
    loop = _running_loop()
    if (
        _default_client is not None
        and _default_client_origin is not None
        and _default_client_origin[0] is _default_client
        and loop is not None
        and _default_client_origin[1] is not loop
    ):
        stale, _default_client, _default_client_origin = _default_client, None, None
        await _close_quietly(stale)
    if _default_client is None:
        _default_client = _build_client(settings.cache_url)
        _default_client_origin = (_default_client, loop) if loop is not None else None
    return _default_client


async def get_default_transport_client(read_timeout: float | None = None) -> Any:
    """Client for the default event-bus transport (blocking reads allowed).

    Resolves :func:`get_default_client` (the single injection seam): an
    injected / patched client is returned as is, while the driver-built
    default gets a sibling client on the same URL (one per ``read_timeout``,
    see :func:`build_transport_client`) — a blocking ``XREADGROUP`` / pub-sub
    ``listen`` must not trip the cache's short socket timeout. The sibling
    follows the default's loop affinity.
    """

    main = await get_default_client()
    origin = _default_client_origin
    if origin is None or origin[0] is not main:
        return main  # injected / patched: share it
    paired = _default_transport.get(read_timeout)
    if paired is not None and paired[0] is main:
        return paired[1]
    # Check, build and publish with no await in between: a concurrent caller
    # then finds this sibling instead of building (and leaking) its own. The
    # stale sibling is closed only after the replacement is published.
    transport = build_transport_client(settings.cache_url, read_timeout=read_timeout)
    _default_transport[read_timeout] = (main, transport)
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


class RedisCacheProvider(CacheProvider):
    """A single Redis cache instance."""

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

    # INCR (+EXPIRE on create) every key in one round trip.
    _INCR_MANY_LUA = """
    local out = {}
    for i, key in ipairs(KEYS) do
        local count = redis.call('INCR', key)
        if count == 1 and tonumber(ARGV[1]) > 0 then
            redis.call('EXPIRE', key, ARGV[1])
        end
        out[i] = count
    end
    return out
    """
    _INCR_MANY_SHA = _script_sha(_INCR_MANY_LUA)

    #: Set once the server rejected ``SET … NX GET`` (Redis < 7.0).
    _nx_get_unsupported = False
    #: Set once a client without scripting made ``set_max`` fall back (logged once).
    _set_max_fallback_warned = False

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
            url = str(self.options.get("url") or settings.cache_url)
            self._client = _build_client(url)
            self._client_loop = loop
        return self._client

    def features(self) -> frozenset[str]:
        return REDIS_CACHE_FEATURES

    async def raw_client(self) -> Any:
        """Redis exposes its client for transport-level needs (pub/sub)."""
        return await self._client_obj()

    # ---- portable operations ------------------------------------------------
    async def get(self, key: str) -> str | None:
        try:
            client = await self._client_obj()
            value = await client.get(key)
            return value if value is None else str(value)
        except Exception as exc:
            _note_error(self.name, "get", exc)
            raise

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        try:
            client = await self._client_obj()
            if ttl_seconds is not None and ttl_seconds > 0:
                await client.set(key, value, ex=ttl_seconds)
            else:
                await client.set(key, value)
        except Exception as exc:
            _note_error(self.name, "set", exc)
            raise

    async def get_many(self, keys: Sequence[str]) -> list[str | None]:
        """One ``MGET`` round trip for every key."""
        try:
            if not keys:
                return []
            client = await self._client_obj()
            values = await client.mget(list(keys))
            return [v if v is None else str(v) for v in values]
        except Exception as exc:
            _note_error(self.name, "get_many", exc)
            raise

    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None:
        """Every ``SET`` in ONE pipelined round trip (not a MULTI transaction)."""
        try:
            if not items:
                return
            client = await self._client_obj()
            ttl = ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else None
            pipeline = getattr(client, "pipeline", None)
            if pipeline is None:  # test doubles without pipelining
                for key, value in items.items():
                    await self.set(key, value, ttl_seconds=ttl_seconds)
                return
            async with pipeline(transaction=False) as pipe:
                for key, value in items.items():
                    if ttl is not None:
                        pipe.set(key, value, ex=ttl)
                    else:
                        pipe.set(key, value)
                await pipe.execute()
        except Exception as exc:
            _note_error(self.name, "set_many", exc)
            raise

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        try:
            client = await self._client_obj()
            if ttl_seconds is not None and ttl_seconds > 0:
                stored = await client.set(key, value, nx=True, ex=ttl_seconds)
            else:
                stored = await client.set(key, value, nx=True)
            return bool(stored)
        except Exception as exc:
            _note_error(self.name, "add", exc)
            raise

    async def delete(self, key: str) -> None:
        try:
            client = await self._client_obj()
            await client.delete(key)
        except Exception as exc:
            _note_error(self.name, "delete", exc)
            raise

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        try:
            client = await self._client_obj()
            ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0
            # Atomic INCR+EXPIRE (Lua); fall back to two ops for fakes/mocks that
            # lack script support (test doubles).
            try:
                return int(
                    await _run_script(
                        client,
                        self._INCR_WITH_TTL_LUA,
                        self._INCR_WITH_TTL_SHA,
                        [key],
                        [ttl],
                    )
                )
            except (AttributeError, NotImplementedError):
                count = int(await client.incr(key))
                if count == 1 and ttl:
                    await client.expire(key, ttl)
                return count
        except Exception as exc:
            _note_error(self.name, "incr", exc)
            raise

    async def incr_window(
        self, key: str, previous_key: str, *, ttl_seconds: int | None = None
    ) -> tuple[int, str | None]:
        try:
            client = await self._client_obj()
            ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0
            try:
                count, previous = await _run_script(
                    client,
                    self._INCR_WINDOW_LUA,
                    self._INCR_WINDOW_SHA,
                    [key, previous_key],
                    [ttl],
                )
            except (AttributeError, NotImplementedError):
                return await super().incr_window(
                    key, previous_key, ttl_seconds=ttl_seconds
                )
            return int(count), (None if previous is None else str(previous))
        except Exception as exc:
            _note_error(self.name, "incr_window", exc)
            raise

    async def incr_many(
        self, keys: Sequence[str], *, ttl_seconds: int | None = None
    ) -> list[int]:
        try:
            if not keys:
                return []
            client = await self._client_obj()
            ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0
            try:
                counts = await _run_script(
                    client, self._INCR_MANY_LUA, self._INCR_MANY_SHA, list(keys), [ttl]
                )
            except (AttributeError, NotImplementedError):
                return await super().incr_many(keys, ttl_seconds=ttl_seconds)
            return [int(c) for c in counts]
        except Exception as exc:
            _note_error(self.name, "incr_many", exc)
            raise

    async def set_nx_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]:
        try:
            if RedisCacheProvider._nx_get_unsupported:
                return await super().set_nx_get(key, value, ttl_seconds=ttl_seconds)
            from redis.exceptions import ResponseError  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client

            client = await self._client_obj()
            ex = ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else None
            try:
                # SET key value NX GET [EX ttl] (Redis >= 7.0): nil = we created it.
                existing = await client.set(key, value, nx=True, get=True, ex=ex)
            except ResponseError as exc:
                if "syntax" not in str(exc).lower():
                    raise
                # Redis < 7.0 rejects NX together with GET: degrade for good.
                RedisCacheProvider._nx_get_unsupported = True
                logger.info("Redis has no SET NX GET (< 7.0); using SET NX + GET")
                return await super().set_nx_get(key, value, ttl_seconds=ttl_seconds)
            except TypeError:  # test double without the ``get`` flag
                return await super().set_nx_get(key, value, ttl_seconds=ttl_seconds)
            if existing is None:
                return True, None
            return False, str(existing)
        except Exception as exc:
            _note_error(self.name, "set_nx_get", exc)
            raise

    # Monotonic "set if greater" (CacheProvider.set_max) in ONE atomic script.
    # Values are plain non-negative decimals compared EXACTLY as digit strings
    # (Lua numbers are doubles: 16+ significant digits would round), byte by
    # byte (``<`` on strings is locale-collated in Lua 5.1). A stored value
    # that is not a plain decimal is corrupt and replaced. The TTL is applied
    # only when ARGV[1] is written; a winning stored value keeps its own.
    _SET_MAX_LUA = """
    local function parse(s)
        if not s then return nil end
        local i, f = string.match(s, '^%s*(%d+)%.?(%d*)%s*$')
        if not i then return nil end
        i = (string.gsub(i, '^0+', ''))
        f = (string.gsub(f, '0+$', ''))
        return i, f
    end
    local function digits_less(a, b)
        for k = 1, #a do
            local x, y = string.byte(a, k), string.byte(b, k)
            if x ~= y then return x < y end
        end
        return false
    end
    local function less(ai, af, bi, bf)
        if #ai ~= #bi then return #ai < #bi end
        if ai ~= bi then return digits_less(ai, bi) end
        local n = math.max(#af, #bf)
        af = af .. string.rep('0', n - #af)
        bf = bf .. string.rep('0', n - #bf)
        return digits_less(af, bf)
    end
    local oi, of = parse(ARGV[1])
    if not oi then
        return redis.error_reply('set_max: value is not a plain decimal')
    end
    local cur = redis.call('GET', KEYS[1])
    if cur then
        local ci, cf = parse(cur)
        if ci and not less(ci, cf, oi, of) then return cur end
    end
    if tonumber(ARGV[2]) > 0 then
        redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    else
        redis.call('SET', KEYS[1], ARGV[1])
    end
    return ARGV[1]
    """
    _SET_MAX_SHA = _script_sha(_SET_MAX_LUA)

    async def set_max(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        try:
            if parse_plain_decimal(value) is None:
                raise ValueError(
                    f"set_max value must be a plain non-negative decimal: {value!r}"
                )
            client = await self._client_obj()
            if not callable(getattr(client, "evalsha", None)):
                # Only a client WITHOUT scripting (a test double) takes the
                # portable loop; an error raised inside the script path is a bug
                # and propagates instead of silently switching implementations.
                if not RedisCacheProvider._set_max_fallback_warned:
                    RedisCacheProvider._set_max_fallback_warned = True
                    logger.warning(
                        "Redis client %s has no Lua scripting (evalsha): set_max "
                        "uses the portable read / compare-and-set loop",
                        type(client).__name__,
                    )
                return await super().set_max(key, value, ttl_seconds=ttl_seconds)
            ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0
            stored = await _run_script(
                client, self._SET_MAX_LUA, self._SET_MAX_SHA, [key], [value, ttl]
            )
            if isinstance(stored, bytes):
                return stored.decode("utf-8", "replace")
            return str(stored)
        except Exception as exc:
            _note_error(self.name, "set_max", exc)
            raise

    # The subject-state record (CacheProvider.set_subject_state) in ONE atomic
    # script: values ``v1:after:<decimal>`` / ``v1:blocked:<decimal>``; every
    # ``blocked`` ranks above every ``after``, within a kind the decimal is
    # compared exactly (the set_max digit comparison). A stored value outside
    # the domain is corrupt and replaced. The TTL applies only when ARGV[1] is
    # written (``0``: none, which also clears an older value's TTL).
    _SUBJECT_STATE_LUA = """
    local function parse(s)
        if not s then return nil end
        local k, i, f = string.match(s, '^v1:(%a+):(%d+)%.?(%d*)$')
        if not k then return nil end
        local rank
        if k == 'after' then rank = 0 elseif k == 'blocked' then rank = 1
        else return nil end
        i = (string.gsub(i, '^0+', ''))
        f = (string.gsub(f, '0+$', ''))
        return rank, i, f
    end
    local function digits_less(a, b)
        for k = 1, #a do
            local x, y = string.byte(a, k), string.byte(b, k)
            if x ~= y then return x < y end
        end
        return false
    end
    local function less(ar, ai, af, br, bi, bf)
        if ar ~= br then return ar < br end
        if #ai ~= #bi then return #ai < #bi end
        if ai ~= bi then return digits_less(ai, bi) end
        local n = math.max(#af, #bf)
        af = af .. string.rep('0', n - #af)
        bf = bf .. string.rep('0', n - #bf)
        return digits_less(af, bf)
    end
    local orank, oi, of = parse(ARGV[1])
    if not orank then
        return redis.error_reply('set_subject_state: not a subject-state value')
    end
    local cur = redis.call('GET', KEYS[1])
    if cur then
        local crank, ci, cf = parse(cur)
        if crank and not less(crank, ci, cf, orank, oi, of) then return cur end
    end
    if tonumber(ARGV[2]) > 0 then
        redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    else
        redis.call('SET', KEYS[1], ARGV[1])
    end
    return ARGV[1]
    """
    _SUBJECT_STATE_SHA = _script_sha(_SUBJECT_STATE_LUA)

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        try:
            if parse_subject_state(value) is None:
                raise ValueError(f"not a subject-state value: {value!r}")
            client = await self._client_obj()
            if not callable(getattr(client, "evalsha", None)):
                # A client WITHOUT scripting (a test double): the portable loop.
                return await super().set_subject_state(
                    key, value, ttl_seconds=ttl_seconds
                )
            ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0
            stored = await _run_script(
                client,
                self._SUBJECT_STATE_LUA,
                self._SUBJECT_STATE_SHA,
                [key],
                [value, ttl],
            )
            if isinstance(stored, bytes):
                return stored.decode("utf-8", "replace")
            return str(stored)
        except Exception as exc:
            _note_error(self.name, "set_subject_state", exc)
            raise

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
        try:
            client = await self._client_obj()
            ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0
            try:
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
            except (AttributeError, NotImplementedError):
                # Test doubles without scripting: best-effort, non-atomic.
                if await self.get(key) != expected:
                    return False
                await self.set(key, value, ttl_seconds=ttl_seconds)
                return True
        except Exception as exc:
            _note_error(self.name, "compare_and_set", exc)
            raise

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        try:
            client = await self._client_obj()
            try:
                return bool(
                    int(
                        await _run_script(
                            client, self._CAD_LUA, self._CAD_SHA, [key], [expected]
                        )
                    )
                )
            except (AttributeError, NotImplementedError):
                if await self.get(key) != expected:
                    return False
                await self.delete(key)
                return True
        except Exception as exc:
            _note_error(self.name, "compare_and_delete", exc)
            raise

    async def health(self) -> HealthResult:
        try:
            client = await self._client_obj()
            pong = await client.ping()
            if pong:
                return HealthResult(status="ok")
            return HealthResult(status="fail", details={"error": "no-pong"})
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            return HealthResult(status="fail", details={"error": str(exc)})

    async def deployment_identity(self) -> str:
        """Attest the reached Redis process, including through DNS aliases.

        Requires the Redis ACL permission for ``INFO server``. This is a
        startup diagnostic, never a per-request security-state operation.
        """
        client = await self._client_obj()
        info = await client.info(section="server")
        identity = info.get("run_id") or info.get(b"run_id")
        if isinstance(identity, bytes):
            identity = identity.decode("ascii")
        if not isinstance(identity, str) or not identity:
            raise RuntimeError("Redis INFO server returned no process identity")
        return identity

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


def _note_error(instance: str, operation: str, exc: BaseException) -> None:
    """Count a Redis / socket failure (not a caller's ValueError / TypeError).

    Called from an ``except`` clause in each operation: a ``try`` costs
    nothing on success, unlike a wrapping coroutine (one frame per call).
    """
    from redis.exceptions import RedisError  # noqa: PLC0415 - cold: redis-py is imported when the driver first builds a client

    if isinstance(exc, RedisError | OSError | TimeoutError):
        cache_operation_errors_total.labels(instance, operation).inc()


def create_redis_cache(cfg: InstanceConfig) -> RedisCacheProvider:
    """Driver factory for ``cache`` capability / ``redis`` driver."""
    url = str(cfg.options.get("url") or settings.cache_url)
    shared = cfg.name == DEFAULT_INSTANCE and url == settings.cache_url
    return RedisCacheProvider(
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
    "get_default_client",
    "build_transport_client",
    "get_default_transport_client",
    "close_default_client",
]
