"""The ``redis`` lease driver: fenced leases on one Redis (spec 5).

Each name owns two keys in one hash slot: a hash ``token`` / ``owner`` /
``fence`` with a TTL (the lease; its expiry is Redis' own clock) and a plain
counter (the fence, ``INCR``, no TTL). Acquire, renew and release are one Lua
script each, so each is atomic and one round trip; an expired key is absent.

**The fence key must outlive the lease**, so the driver refuses a Redis whose
``maxmemory-policy`` is not ``noeviction`` (read on ``aopen``, on every
``health`` probe and again after a connection failure, so a server that came
back reconfigured is judged afresh). The option ``never_evicts_check`` decides
what a *refused read* means (managed services often refuse ``CONFIG GET``):

- ``verify`` (default): a refused read is ``PermissionDenied``;
- ``assume``: the operator vouches for ``noeviction``; the driver runs on a
  refused read, health is ``degraded`` (``noeviction_unverified``). A readable
  policy that is not ``noeviction`` is ``InvalidArgument`` in either mode.

A flag cannot judge a socket it did not see, and the pool reconnects a dropped
one without raising (an evicted fence key would repeat a fence). So the pool's
``redis_connect_func`` (:mod:`sandloom_redis._eviction_guard`) reads the policy
on each new socket, the first one included, before any command, under the same
rules; the price is one round trip per new socket.

The driver reads no setting, so the kernel states whether ``assume`` is allowed
(production: only under the accepted risk ``unverified_noeviction``) as the
constructor argument ``assume_allowed`` when it builds the driver; without it
``assume`` is refused.

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
from collections.abc import Callable, Mapping
from functools import cache
from typing import Any, ClassVar, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import (
    HealthResult,
    InfraError,
    InvalidArgument,
    Lease,
    LeaseHolder,
    LeaseProvider,
    PermissionDenied,
)
from sandloom.driverkit.lease_limits import (
    MAX_LEASE_NAME_LENGTH,
    MIN_LEASE_TTL_SECONDS,
)
from sandloom.driverkit.shared_endpoints import redis_endpoint
from sandloom.driverkit.shared_endpoints import register as register_endpoint
from sandloom.driverkit.shared_endpoints import unregister as unregister_endpoint
from sandloom.driverkit.testing_isolation import isolated_redis_options
from sandloom_redis._errors import map_redis_error
from sandloom_redis._eviction_guard import (
    POLICY_SETTING,
    UNVERIFIED_NOEVICTION_RISK,
    connect_func,
    eviction_refusal,
    read_policy,
    refusal_of,
    refusal_reason,
)
from sandloom_redis._settings_fill import LEASE_FILL, require_url


@cache
def _backend_errors() -> tuple[type[BaseException], ...]:
    """What a failed probe's Redis call raises: redis-py's errors (the pool's
    eviction refusal included), socket errors, a timeout and an undecodable
    reply. Imported when the driver runs: the module imports without redis-py,
    so the registry can check the requirement first."""
    redis_error = importlib.import_module("redis.exceptions").RedisError
    return (redis_error, OSError, TimeoutError, UnicodeDecodeError)


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
    settings_fill = LEASE_FILL
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"
    # Beta, the floor for a driver of this repository (spec 2.5): the lane
    # `redis-drivers` job of ci.yml runs LeaseConformance against a real
    # Redis. An undeclared tier would be experimental.
    tier = "beta"

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """Another database index of the configured URL for each xdist worker."""
        return isolated_redis_options(options, worker_id)

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, Any],
        assume_allowed: Callable[[], bool] | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """``assume_allowed`` is the kernel's policy for ``never_evicts_check=
        assume`` (production without the accepted risk: ``False``); ``None``
        refuses ``assume``. ``client_factory`` replaces the client built from
        the options (a test seam)."""
        super().__init__(name=name, driver=driver, options=options)
        self._opts = RedisLeaseOptions(**options)
        self._assume_allowed = assume_allowed
        self._client_factory = client_factory
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._policy_checked = False
        # The server's refusal, repeated by every operation until a check finds
        # the policy fit (a registry that only logs it outside production must
        # never serve an evicting Redis).
        self._refusal: InfraError | None = None

    # ------------------------------------------------------------ plumbing
    def _fail(self, exc: BaseException, what: str) -> InfraError:
        """The ``InfraError`` of a backend failure: its type name only. A failure
        of the connection (``Unavailable``, ``Timeout``) makes the next call read
        the eviction policy again: the server may have come back reconfigured."""
        if isinstance(exc, InfraError):
            return exc
        refusal = refusal_of(exc)
        if refusal is not None:
            # The pool refused a socket whose server evicts (or cannot be
            # verified) before any command: judge the policy again next time.
            self._policy_checked = False
            return refusal
        # Chained ``from exc`` at the call sites (as before): the lease is
        # called by code that may read the chain, and the raw text stays in
        # ``__cause__`` only.
        mapped = map_redis_error(
            exc,
            capability=self.capability,
            instance=self.name,
            what=f"redis lease {what} failed",
        )
        if not isinstance(mapped, PermissionDenied):
            self._policy_checked = False
        return mapped

    async def _redis(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._client is not None and self._loop is loop:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            self._loop = loop
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
            # Every socket of the pool, the first included, is judged on itself
            # before it carries a command: the pool reconnects a dropped socket
            # silently, and an evicted fence key would repeat a fence.
            redis_connect_func=connect_func(self._connection_refusal),
        )
        self._client = redis.Redis(connection_pool=pool)
        self._loop = loop
        self._policy_checked = False
        return self._client

    def _keys(self, name: str) -> list[str]:
        base = f"{self._opts.key_prefix}{{{name}}}"
        return [f"{base}:lease", f"{base}:fence"]

    def _check_name(self, name: str) -> None:
        if not isinstance(name, str) or not name or len(name) > MAX_LEASE_NAME_LENGTH:
            raise InvalidArgument(
                f"a lease name is 1..{MAX_LEASE_NAME_LENGTH} characters",
                capability=self.capability,
                instance=self.name,
            )

    def _refuse(self, refusal: InfraError) -> NoReturn:
        """Record the refusal of the server (the operations repeat it) and raise."""
        self._refusal = refusal
        raise refusal

    def _assume_refused(self) -> bool:
        return self._opts.never_evicts_check == "assume" and not (
            self._assume_allowed is not None and self._assume_allowed()
        )

    def _connection_refusal(self, policy: str | None) -> InfraError | None:
        """The refusal for the policy one connection reported (``None``: the
        read was refused), or ``None`` when the server is fit. The pool runs it
        on every new socket; ``_check_policy`` shares its rules
        (:func:`~sandloom_redis._eviction_guard.eviction_refusal`)."""
        return eviction_refusal(
            capability=self.capability,
            instance=self.name,
            policy=policy,
            never_evicts_check=self._opts.never_evicts_check,
            assume_refused=self._assume_refused(),
            at_stake="the fence counter could vanish and a fence would repeat",
        )

    async def _check_policy(self, client: Any) -> bool:
        """Judge the server's eviction policy (the refusals are recorded):
        ``True`` when it could not be read and ``assume`` vouches for it."""
        self._refusal = None
        if self._assume_refused():  # refused before the server is asked
            refusal = self._connection_refusal(None)
            if refusal is not None:
                self._refuse(refusal)
        policy = await read_policy(client.config_get(POLICY_SETTING))
        refusal = self._connection_refusal(policy)
        if refusal is not None:
            self._refuse(refusal)
        return policy is None

    async def _ready(self) -> Any:
        client = await self._redis()
        if not self._policy_checked:
            try:
                await self._check_policy(client)
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
        if self._client_factory is None:  # a client of its own needs the url
            require_url("lease", self.name, self._opts.url)
        register_endpoint(
            "lease",
            self.name,
            redis_endpoint(self._opts.url),
            "persistent",
            option=f"lease.{self.name}.url",
            declared=not self.implicit,
        )
        await self._ready()

    async def aclose(self) -> None:
        unregister_endpoint("lease", self.name, "persistent")
        client, self._client = self._client, None
        self._loop = None
        self._policy_checked = False
        if client is not None:
            with contextlib.suppress(Exception):  # closing is best effort
                await client.aclose(close_connection_pool=True)

    def _refusal_reason(self, refusal: InfraError, refused_before: bool) -> str:
        """The ``reason`` of a probe a refusal failed: a refusal already
        recorded, or ``assume`` without the kernel's leave, is
        ``eviction_check_refused``; otherwise the policy decides."""
        if refused_before or self._assume_refused():
            return "eviction_check_refused"
        return refusal_reason(refusal)

    def _failed_probe(
        self, exc: BaseException, what: str, *, refused_before: bool
    ) -> HealthResult:
        """A probe a backend failure failed. A socket the pool refused (its
        server evicts or cannot be verified) is judged as the policy check
        judges it, not mapped to the error type of the refusal."""
        refusal = refusal_of(exc)
        mapped = self._fail(exc, what)
        if refusal is None:
            return HealthResult(status="fail", details={"error": type(mapped).__name__})
        return HealthResult(
            status="fail",
            details={"reason": self._refusal_reason(refusal, refused_before)},
        )

    async def health(self) -> HealthResult:
        started = time.monotonic()
        refused_before = self._refusal is not None
        try:
            client = await self._redis()
            await client.ping()
        except InfraError as exc:  # a failed probe, by its type only
            return HealthResult(status="fail", details={"error": type(exc).__name__})
        except _backend_errors() as exc:
            return self._failed_probe(exc, "ping", refused_before=refused_before)
        try:
            unverified = await self._check_policy(client)
        except InfraError as exc:
            if self._refusal is None:  # the read failed; the server did not refuse
                return HealthResult(
                    status="fail", details={"error": type(exc).__name__}
                )
            # The policy was lost after a check passed: the next operation
            # judges it again (and raises) instead of trusting the old pass.
            self._policy_checked = False
            return HealthResult(
                status="fail",
                details={"reason": self._refusal_reason(exc, refused_before)},
            )
        except _backend_errors() as exc:
            return self._failed_probe(
                exc, "policy check", refused_before=refused_before
            )
        latency = round((time.monotonic() - started) * 1000, 1)
        if unverified:
            return HealthResult(
                status="degraded",
                details={"reason": "noeviction_unverified", "latency_ms": latency},
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
            or ttl_seconds < MIN_LEASE_TTL_SECONDS
        ):
            raise InvalidArgument(
                f"a lease TTL is a whole number of seconds >= {MIN_LEASE_TTL_SECONDS}",
                capability=self.capability,
                instance=self.name,
            )
        if not isinstance(owner, str) or len(owner) > MAX_LEASE_NAME_LENGTH:
            raise InvalidArgument(
                f"a lease owner is a label of up to {MAX_LEASE_NAME_LENGTH} characters",
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


__all__ = [
    "UNVERIFIED_NOEVICTION_RISK",
    "RedisLeaseOptions",
    "RedisLeaseProvider",
]
