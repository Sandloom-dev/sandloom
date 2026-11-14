"""The ``redis`` lease driver: fenced leases on one Redis (spec 5).

Each name owns two keys in one hash slot: a hash ``token`` / ``owner`` /
``fence`` with a TTL (the lease; its expiry is Redis' own clock) and a plain
counter (the fence, ``INCR``, no TTL). Acquire, renew and release are one Lua
script each, so each is atomic and one round trip; an expired key is absent.

**The fence key must outlive the lease**, so the driver refuses a Redis whose
``maxmemory-policy`` is not ``noeviction`` (read on ``aopen`` and on every
``health`` probe). ``never_evicts_check=assume`` skips only a read the server
refuses (managed services often do): a readable policy that is not
``noeviction`` is refused either way. In production the kernel admits
``assume`` only under the accepted risk ``unverified_noeviction`` (checked
where the driver is built, ``sandloom.core.drivers.bootstrap``: a driver
reads no setting).

The client is built lazily on the running loop (a redis.asyncio client is
bound to the loop it first ran on); an error a backend raises is mapped to
an :class:`~sandloom.driverkit.InfraError` carrying its type name, never its
text, which may hold the URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import time
import uuid
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import (
    HealthResult,
    InfraError,
    InvalidArgument,
    Lease,
    LeaseHolder,
    LeaseProvider,
    PermissionDenied,
    Timeout,
    Unavailable,
)

_MIN_TTL_SECONDS = 3


def _redis_errors() -> Any:
    """``redis.exceptions``, imported when the driver runs: the module imports
    without redis-py, so the registry can check the requirement first."""
    return importlib.import_module("redis.exceptions")


_MAX_NAME_LENGTH = 255
_ACQUIRE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return nil end
local fence = redis.call('INCR', KEYS[2])
redis.call('HSET', KEYS[1], 'token', ARGV[1], 'owner', ARGV[2], 'fence', fence)
redis.call('EXPIRE', KEYS[1], ARGV[3])
return fence
"""
_RENEW = """
if redis.call('HGET', KEYS[1], 'token') == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""
_RELEASE = """
if redis.call('HGET', KEYS[1], 'token') == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""
_HOLDER = "return redis.call('HMGET', KEYS[1], 'owner', 'fence')"


class RedisLeaseOptions(BaseModel):
    """Config-shape schema of the ``redis`` lease driver (``extra="forbid"``
    rejects a typo'd key at startup)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    url: str | None = None
    #: ``verify`` reads ``maxmemory-policy``; ``assume`` skips a refused read.
    never_evicts_check: Literal["verify", "assume"] = "verify"
    key_prefix: str = Field(default="sandloom:lease:", min_length=1, max_length=64)
    socket_timeout_seconds: float = Field(default=1.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=1.0, gt=0)
    max_connections: int = Field(default=16, ge=1)
    #: Seconds a call waits for a free connection of the pool.
    pool_timeout_seconds: float = Field(default=1.0, gt=0)


class RedisLeaseProvider(LeaseProvider):
    """Fenced leases on one Redis."""

    contract_version = 1
    supported_features: ClassVar[frozenset[str]] = frozenset({"shared"})
    options_schema = RedisLeaseOptions
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"

    def __init__(self, *, name: str, driver: str, options: dict[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._opts = RedisLeaseOptions(**options)
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._policy_checked = False

    # ------------------------------------------------------------ plumbing
    def _fail(self, exc: BaseException, what: str) -> InfraError:
        """The ``InfraError`` of a backend failure: its type name only."""
        if isinstance(exc, InfraError):
            return exc
        label = f"redis lease {what} failed: {type(exc).__name__}"
        redis_errors = _redis_errors()
        if isinstance(exc, TimeoutError | redis_errors.TimeoutError):
            return Timeout(label, capability=self.capability, instance=self.name)
        if isinstance(exc, redis_errors.AuthenticationError):
            return PermissionDenied(
                label, capability=self.capability, instance=self.name
            )
        if isinstance(exc, redis_errors.ResponseError) and "NOPERM" in str(exc):
            return PermissionDenied(
                label, capability=self.capability, instance=self.name
            )
        return Unavailable(label, capability=self.capability, instance=self.name)

    async def _redis(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._client is not None and self._loop is loop:
            return self._client
        if not self._opts.url:
            raise InvalidArgument(
                "the redis lease driver needs the option 'url'",
                capability=self.capability,
                instance=self.name,
            )
        redis = importlib.import_module("redis.asyncio")
        if self._client is not None:  # another loop: the old client is unusable
            self._client = None
        pool: Any = redis.BlockingConnectionPool.from_url(
            self._opts.url,
            decode_responses=True,
            socket_timeout=self._opts.socket_timeout_seconds,
            socket_connect_timeout=self._opts.socket_connect_timeout_seconds,
            max_connections=self._opts.max_connections,
            timeout=self._opts.pool_timeout_seconds,
        )
        self._client = redis.Redis(connection_pool=pool)
        self._loop = loop
        self._policy_checked = False
        return self._client

    def _keys(self, name: str) -> list[str]:
        base = f"{self._opts.key_prefix}{{{name}}}"
        return [f"{base}:lease", f"{base}:fence"]

    def _check_name(self, name: str) -> None:
        if not isinstance(name, str) or not name or len(name) > _MAX_NAME_LENGTH:
            raise InvalidArgument(
                f"a lease name is 1..{_MAX_NAME_LENGTH} characters",
                capability=self.capability,
                instance=self.name,
            )

    async def _policy_refused_read(self, client: Any) -> bool:
        """Read ``maxmemory-policy``: ``True`` when the read was refused and
        ``assume`` skips it; raises on a policy other than ``noeviction`` and
        on a refused read under ``verify``."""
        try:
            answer = await client.config_get("maxmemory-policy")
        except _redis_errors().ResponseError as exc:
            if self._opts.never_evicts_check == "assume":
                return True
            raise PermissionDenied(
                "the server refused CONFIG GET maxmemory-policy: set the option "
                "never_evicts_check=assume to vouch for noeviction (production "
                "needs the accepted risk unverified_noeviction)",
                capability=self.capability,
                instance=self.name,
            ) from exc
        policy = str(answer.get("maxmemory-policy", ""))
        if policy != "noeviction":
            raise PermissionDenied(
                f"the server's maxmemory-policy is {policy!r}, not 'noeviction': "
                "the fence counter could be evicted and a fence would repeat",
                capability=self.capability,
                instance=self.name,
            )
        return False

    async def _ready(self) -> Any:
        client = await self._redis()
        if not self._policy_checked:
            try:
                await self._policy_refused_read(client)
            except InfraError:
                raise
            except Exception as exc:
                raise self._fail(exc, "policy check") from exc
            self._policy_checked = True
        return client

    async def _guarded(self, what: str, call: Any) -> Any:
        """``await call``, a backend failure mapped to an ``InfraError``."""
        try:
            return await call
        except InfraError:
            raise
        except Exception as exc:
            raise self._fail(exc, what) from exc

    async def _script(self, script: str, name: str, *args: object, what: str) -> Any:
        try:
            client = await self._ready()
            keys = self._keys(name)
            return await client.eval(script, len(keys), *keys, *args)
        except InfraError:
            raise
        except Exception as exc:
            raise self._fail(exc, what) from exc

    # ----------------------------------------------------------- lifecycle
    async def aopen(self) -> None:
        await self._ready()

    async def aclose(self) -> None:
        client, self._client = self._client, None
        self._loop = None
        self._policy_checked = False
        if client is not None:
            with contextlib.suppress(Exception):  # closing is best effort
                await client.aclose(close_connection_pool=True)

    async def health(self) -> HealthResult:
        started = time.monotonic()
        try:
            client = await self._redis()
            await self._guarded("ping", client.ping())
            refused = await self._guarded(
                "policy check", self._policy_refused_read(client)
            )
        except InfraError as exc:  # a failed probe, by its type only
            return HealthResult(status="fail", details={"error": type(exc).__name__})
        latency = round((time.monotonic() - started) * 1000, 1)
        if refused:
            return HealthResult(
                status="degraded",
                details={"reason": "unverified_noeviction", "latency_ms": latency},
            )
        return HealthResult(status="ok", details={"latency_ms": latency})

    async def isolation_identity(self) -> str | None:
        client = await self._redis()
        info = await self._guarded("probe", client.info(section="server"))
        run_id = info.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return None
        return f"{self.driver}:{run_id}"

    # ---------------------------------------------------------- operations
    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        self._check_name(name)
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds < _MIN_TTL_SECONDS
        ):
            raise InvalidArgument(
                f"a lease TTL is a whole number of seconds >= {_MIN_TTL_SECONDS}",
                capability=self.capability,
                instance=self.name,
            )
        if not isinstance(owner, str) or len(owner) > _MAX_NAME_LENGTH:
            raise InvalidArgument(
                f"a lease owner is a label of up to {_MAX_NAME_LENGTH} characters",
                capability=self.capability,
                instance=self.name,
            )
        token = uuid.uuid4().hex
        fence = await self._script(
            _ACQUIRE, name, token, owner, ttl_seconds, what="acquire"
        )
        if fence is None:
            return None
        return Lease(
            name=name,
            owner=owner,
            token=token,
            fence=int(fence),
            ttl_seconds=ttl_seconds,
        )

    async def renew(self, lease: Lease) -> bool:
        self._check_name(lease.name)
        kept = await self._script(
            _RENEW, lease.name, lease.token, lease.ttl_seconds, what="renew"
        )
        return bool(kept)

    async def release(self, lease: Lease) -> bool:
        self._check_name(lease.name)
        ended = await self._script(_RELEASE, lease.name, lease.token, what="release")
        return bool(ended)

    async def holder(self, name: str) -> LeaseHolder | None:
        self._check_name(name)
        owner, fence = await self._script(_HOLDER, name, what="holder")
        if owner is None or fence is None:
            return None
        return LeaseHolder(owner=str(owner), fence=int(fence))


__all__ = ["RedisLeaseOptions", "RedisLeaseProvider"]
