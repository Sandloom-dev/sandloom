"""The Redis ``atomic_store`` driver (``redis``): a durable, never-evicting store.

Every operation is one round trip and atomic on the server: a single command
(``GET``, ``SET``, ``DEL``, ``SET NX``) or one Lua script via ``EVALSHA``
(``add_or_get``, ``compare_and_set``, ``compare_and_delete``,
``set_subject_state``). Redis treats an expired key as absent by itself, so no
script reads an expiry.

**It never evicts, and says so only after checking.** On ``aopen`` and in every
``health`` probe the driver reads ``maxmemory-policy`` and refuses a server
whose policy is not ``noeviction`` (an evicting server loses a revocation mark
before its TTL). The option ``never_evicts_check`` decides what a *refused read*
of that setting (managed Redis often refuses ``CONFIG GET``) means:

- ``verify`` (default): a refused read is ``PermissionDenied`` at ``aopen``;
- ``assume``: the operator vouches for the policy. Only a refused read is
  skipped: a policy that can be read and is not ``noeviction`` is refused all
  the same, and ``health`` keeps trying the read (``degraded`` while it stays
  refused). In production ``assume`` needs the accepted risk
  ``unverified_noeviction``; the driver reads no setting, so the kernel states
  that policy as the constructor argument ``assume_allowed`` when it builds
  the driver, and a driver built without it refuses ``assume``.

The check does not depend on ``aopen``: a worker or a CLI process never runs it,
and a registry outside production only logs a refused one. So every operation
judges the policy itself until one check has passed, and raises the refusal
meanwhile. A failed ``health`` probe (policy lost or unreadable, or the probe
itself failing with a connection error), an operation failing with
``Unavailable`` or ``Timeout`` and a client rebuilt for another loop open the
check again, so a ``noeviction`` lost at runtime is refused by the next
operation.

**Every socket is judged on itself.** A flag cannot judge a connection it did
not see: the retrying client and the pool's liveness probe reconnect a dropped
socket without raising, and a pool that grows opens new ones, after a failover
or a DNS repoint possibly to a reconfigured server. So the pool's
``redis_connect_func`` (:mod:`sandloom_redis._eviction_guard`) sends ``CONFIG
GET maxmemory-policy`` on each new socket, the first one included, before any
command, and the same refusal rules apply (an evicting policy is
``InvalidArgument``, a refused read ``PermissionDenied`` under ``verify``). The
price is one round trip per new socket. A client given by ``client_factory`` (a
test seam) has no pool, so no such hook.

**A lost reply is never answered by running the command again.** A connection
error after the server ran ``add``, ``add_or_get``, ``compare_and_set`` or
``compare_and_delete`` would answer ``False`` (or the caller's own value) to the
caller that won, so those four use a second client that never re-sends and raise
``Unavailable``. ``get``, ``set``, ``delete`` and ``set_subject_state`` are
idempotent and use the client that re-sends after a connection error
(``retry_attempts``).

The driver reads no kernel setting and keeps no process-wide client: each
instance owns two bounded clients built from its options (the cache driver's
bounds; each has its own pool of ``max_connections``). It maps every backend
failure to the ``InfraError`` hierarchy and carries only the error type in a
message, never the backend's text, which can name a host or a key.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from types import ModuleType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sandloom.driverkit import (
    AtomicStoreProvider,
    BackupHint,
    HealthResult,
    InfraError,
    InvalidArgument,
    Timeout,
    Unavailable,
    observe,
)
from sandloom.driverkit.atomic_store import (
    ATOMIC_STORE_PERFORMANCE_HINTS,
    DEFAULT_MAX_VALUE_BYTES,
    MAX_SUBJECT_STATE_CHARS,
    check_key,
    check_subject_state,
    check_ttl,
    check_value,
)
from sandloom.driverkit.shared_endpoints import redis_endpoint
from sandloom.driverkit.shared_endpoints import register as register_endpoint
from sandloom.driverkit.shared_endpoints import unregister as unregister_endpoint
from sandloom.driverkit.testing_isolation import isolated_url_options
from sandloom_redis._errors import map_redis_error
from sandloom_redis._eviction_guard import (
    NOEVICTION,
    POLICY_SETTING,
    UNVERIFIED_NOEVICTION_RISK,
    Judge,
    connect_func,
    eviction_refusal,
    read_policy,
    refusal_of,
    refusal_reason,
)
from sandloom_redis._url import checked_url, require_url
from sandloom_redis.backup import atomic_store_hint

#: Failed operations of the store (connection, timeout, server error), by
#: instance and operation: the page-level alert on the security store reads it.
atomic_store_operation_errors_total = observe.counter(
    "sandloom_atomic_store_operation_errors_total",
    "Atomic store operations that raised (connection, timeout, server error)",
    ("instance", "operation"),
)

#: What ``never_evicts_check`` is set to (see the module docstring).
NeverEvictsCheck = Literal["verify", "assume"]


class RedisAtomicStoreOptions(BaseModel):
    """Options of the ``redis`` atomic_store driver: the bounded client's, the
    eviction check and the bound on a value. ``extra="forbid"`` rejects an
    unknown key (``ur1`` for ``url``) at startup."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    url: str | None = None

    @field_validator("url")
    @classmethod
    def _url_parses(cls, value: str | None) -> str | None:
        return checked_url(value)

    socket_timeout_seconds: float = Field(default=0.5, gt=0)
    socket_connect_timeout_seconds: float = Field(default=0.5, gt=0)
    health_check_interval_seconds: int = Field(default=30, ge=0)
    max_connections: int = Field(default=64, ge=1)
    pool_timeout_seconds: float = Field(default=1.0, gt=0)
    retry_attempts: int = Field(default=1, ge=0, le=5)
    never_evicts_check: NeverEvictsCheck = "verify"
    max_value_bytes: int = Field(default=DEFAULT_MAX_VALUE_BYTES, ge=1)


# redis-py is imported on first use: the driver module imports without it, and
# the registry refuses the driver before it is built when it is missing
# (SANDLOOM-B037). One accessor per module keeps each import name a literal.
@cache
def _exceptions() -> ModuleType:
    return importlib.import_module("redis.exceptions")


@cache
def _asyncio_module() -> ModuleType:
    return importlib.import_module("redis.asyncio")


@cache
def _retry_module() -> ModuleType:
    return importlib.import_module("redis.asyncio.retry")


@cache
def _backoff_module() -> ModuleType:
    return importlib.import_module("redis.backoff")


@cache
def _backend_errors() -> tuple[type[BaseException], ...]:
    """What a Redis backend raises: redis-py's errors, socket errors, a timeout
    and an undecodable stored value. Nothing else is caught: any other
    exception is a driver bug and propagates (spec 2.1)."""
    exceptions = _exceptions()
    return (exceptions.RedisError, OSError, TimeoutError, UnicodeDecodeError)


@cache
def _probe_errors() -> tuple[type[BaseException], ...]:
    """What a failed probe is: a backend failure or a refused configuration."""
    return (InfraError, *_backend_errors())


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:  # called outside a loop (sync caller)
        return None


def _build_client(
    options: RedisAtomicStoreOptions,
    *,
    retrying: bool,
    judge: Judge | None = None,
) -> Any:
    """A bounded client: timeouts, keepalive, a periodic health check and a
    capped BLOCKING pool, so an unreachable or hung Redis makes an operation
    raise within roughly ``(1 + retry_attempts) x socket_timeout_seconds``
    instead of hanging a request.

    ``retrying`` clients re-send a command after a connection error (a stale
    pooled socket after a failover), never after a timeout. A command whose
    reply was lost may have run, so only idempotent commands use that client;
    the others use a client that never re-sends (:meth:`RedisAtomicStoreProvider
    ._operation_client`)."""
    redis = _asyncio_module()
    retry_module = _retry_module()
    backoff = _backoff_module()
    exceptions = _exceptions()
    attempts = options.retry_attempts if retrying else 0
    pool: Any = redis.BlockingConnectionPool.from_url(
        options.url,
        max_connections=options.max_connections,
        timeout=options.pool_timeout_seconds,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=options.socket_timeout_seconds,
        socket_connect_timeout=options.socket_connect_timeout_seconds,
        socket_keepalive=True,
        health_check_interval=options.health_check_interval_seconds,
        retry=retry_module.Retry(
            backoff.FullJitterBackoff(cap=0.1, base=0.01),
            attempts,
            supported_errors=(exceptions.ConnectionError,),
        ),
        retry_on_error=[exceptions.ConnectionError] if retrying else [],
        # Every socket of the pool, the first included, is judged on itself
        # before it carries a command (_eviction_guard).
        redis_connect_func=None if judge is None else connect_func(judge),
    )
    client: Any = redis.Redis.from_pool(pool)
    return client


async def _close_quietly(client: Any) -> None:
    """Best-effort close of a client whose loop may be gone (never raises)."""
    closer = getattr(client, "aclose", None) or client.close
    try:
        await asyncio.wait_for(closer(), timeout=1.0)
    except (RuntimeError, OSError, TimeoutError):  # its loop is closed: drop it
        return


def _script_sha(script: str) -> str:
    return hashlib.sha1(  # Redis EVALSHA key, not security
        script.encode("utf-8"), usedforsecurity=False
    ).hexdigest()


async def _run_script(
    client: Any, script: str, sha: str, keys: Sequence[str], args: Sequence[Any]
) -> Any:
    """``EVALSHA`` the script; on ``NOSCRIPT`` load it once and retry."""
    evalsha = client.evalsha
    try:
        return await evalsha(sha, len(keys), *keys, *args)
    except _exceptions().NoScriptError:
        await client.script_load(script)
        return await evalsha(sha, len(keys), *keys, *args)


def _failure_details(exc: BaseException) -> dict[str, object]:
    """The ``details`` of a failed probe: the error type name, never the
    exception text (a Redis error names the host)."""
    return {"error": type(exc).__name__}


# ``add_or_get``: create when absent, else answer the value that blocked. The
# TTL (ARGV[2], ``0``: none) is applied only when the key is created.
_ADD_OR_GET_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur then return {0, cur} end
if tonumber(ARGV[2]) > 0 then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
else
    redis.call('SET', KEYS[1], ARGV[1])
end
return {1}
"""

# The subject-state record: values ``v1:after:<decimal>`` / ``v1:blocked:<decimal>``;
# every ``blocked`` ranks above every ``after``, within a kind the decimal is
# compared exactly as digit strings, byte by byte (Lua numbers are doubles:
# 16+ significant digits would round, and ``<`` on strings is locale-collated).
# A stored value outside the domain is corrupt and replaced; one longer than
# the domain's bound is corrupt without being matched (the pattern backtracks
# quadratically on a long run of digits: 40k characters held a server for 7 s).
# The TTL applies only when ARGV[1] is written (``0``: none, which also clears
# an older TTL).
_SUBJECT_STATE_LUA = """
local function parse(s)
    if not s then return nil end
    if #s > __MAX_CHARS__ then return nil end
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
""".replace("__MAX_CHARS__", str(MAX_SUBJECT_STATE_CHARS))

# Compare-and-set / compare-and-delete: the value is replaced or removed only
# while it still equals the caller's expected value (an owner token in it). An
# absent (or expired) key answers 0. ``ARGV[3]`` is the new TTL, ``0``: none.
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
_CAD_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_SHAS = {
    script: _script_sha(script)
    for script in (_ADD_OR_GET_LUA, _SUBJECT_STATE_LUA, _CAS_LUA, _CAD_LUA)
}


class RedisAtomicStoreProvider(AtomicStoreProvider):
    """One Redis ``atomic_store`` instance."""

    contract_version = 1
    supported_features = frozenset({"shared", "never_evicts"})
    performance_hints = ATOMIC_STORE_PERFORMANCE_HINTS
    options_schema = RedisAtomicStoreOptions
    url_option = "url"
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"
    # Beta, the floor for a driver of this repository (spec 2.5): the lane
    # `redis-drivers` job of ci.yml runs AtomicStoreConformance against a real
    # Redis. An undeclared tier would be experimental.
    tier = "beta"

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """Another database index of the configured URL for each xdist worker
        (the security store lives on the cache's database in a suite: the worker's
        marks and blocks stay apart from every other worker's)."""
        return isolated_url_options(options, worker_id)

    @classmethod
    def backup_hint(cls, options: Mapping[str, Any]) -> BackupHint | None:
        """The server this store runs on, for ``sandloom backup plan``."""
        return atomic_store_hint(options)

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: Mapping[str, Any],
        assume_allowed: Callable[[], bool] | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """``assume_allowed`` is the kernel's policy for ``never_evicts_check=
        assume`` (production without the accepted risk: ``False``); ``None``
        refuses ``assume``. ``client_factory`` replaces the client built from
        the options (a test seam)."""
        super().__init__(name=name, driver=driver, options=options)
        self._options = RedisAtomicStoreOptions.model_validate(self.options)
        self._assume_allowed = assume_allowed
        self._client_factory = client_factory
        self._client: Any | None = None
        self._once_client: Any | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        # True once a check of the server's eviction policy passed. Until then
        # every operation judges the policy itself (a worker or a CLI process
        # never runs ``aopen``; a registry outside production only logs a
        # refused one), so an evicting store is never served. A health probe
        # that finds the policy lost, and a failed operation, clear it again.
        # A new socket needs no flag: the pool judges each one on itself.
        self._policy_checked = False

    def _connection_refusal(self, policy: str | None) -> InfraError | None:
        """The refusal for the policy one connection reported (``None``: the
        read was refused), or ``None`` when the server is fit. The pool runs it
        on every new socket; ``_check_policy`` and ``health`` share its rules
        (:func:`~sandloom_redis._eviction_guard.eviction_refusal`)."""
        return eviction_refusal(
            capability=self.capability,
            instance=self.name,
            policy=policy,
            never_evicts_check=self._options.never_evicts_check,
            assume_refused=self._assume_refused(),
            at_stake="a revocation mark or an idempotency reservation could "
            "vanish before its TTL",
        )

    # ---------------------------------------------------------------- client
    async def _client_obj(self) -> Any:
        """The client that re-sends a command after a connection error: for
        the lifecycle and the idempotent operations."""
        return await self._client_of(retrying=True)

    async def _operation_client(self, *, idempotent: bool) -> Any:
        """The client of an operation, after the eviction check passed. A
        command whose reply was lost may have run: ``add``, ``add_or_get``,
        ``compare_and_set`` and ``compare_and_delete`` answer a different value
        when run twice, so they use the client that never re-sends."""
        if not self._policy_checked:
            await self._check_policy("policy_check")
        return await self._client_of(retrying=idempotent)

    async def _client_of(self, *, retrying: bool) -> Any:
        loop = _running_loop()
        if (
            self._client_loop is not None
            and loop is not None
            and self._client_loop is not loop
            and (self._client is not None or self._once_client is not None)
        ):
            # A redis.asyncio client is bound to the loop it first ran on.
            stale = (self._client, self._once_client)
            self._client = self._once_client = None
            # The new pools judge each of their sockets; the flag opens too, so
            # the next operation reads the policy again (as the lease does).
            self._policy_checked = False
            for old in dict.fromkeys(c for c in stale if c is not None):
                await _close_quietly(old)
        if self._client_factory is not None:
            if self._client is None:
                self._client = self._client_factory()
                self._client_loop = loop
            return self._client
        if not self._options.url:
            raise InvalidArgument(
                f"atomic_store instance {self.name!r} has no 'url' option",
                capability=self.capability,
                instance=self.name,
            )
        if retrying:
            if self._client is None:
                self._client = _build_client(
                    self._options, retrying=True, judge=self._connection_refusal
                )
                self._client_loop = loop
            return self._client
        if self._once_client is None:
            self._once_client = _build_client(
                self._options, retrying=False, judge=self._connection_refusal
            )
            self._client_loop = loop
        return self._once_client

    def _failure(self, operation: str, exc: BaseException) -> InfraError:
        """The ``InfraError`` for a backend failure: its type, never its text."""
        atomic_store_operation_errors_total.labels(self.name, operation).inc()
        refusal = refusal_of(exc)
        if refusal is not None:
            # A socket's server evicts (or cannot be verified): the pool refused
            # it before any command. The operations judge the policy again.
            self._policy_checked = False
            return refusal
        # ``from None`` at the call sites: callers branch on the class only.
        mapped = map_redis_error(
            exc,
            capability=self.capability,
            instance=self.name,
            what=f"atomic_store {self.name!r} {operation}",
        )
        if isinstance(mapped, Unavailable | Timeout):
            # The server may come back reconfigured: judge its policy again.
            self._policy_checked = False
        return mapped

    # ------------------------------------------------------------ operations
    async def get(self, key: str) -> str | None:
        check_key(key, instance=self.name)
        try:
            client = await self._operation_client(idempotent=True)
            value = await client.get(key)
        except InfraError:
            raise
        except _backend_errors() as exc:
            raise self._failure("get", exc) from None
        return value if value is None else str(value)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        check_key(key, instance=self.name)
        check_value(value, limit=self._options.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        try:
            client = await self._operation_client(idempotent=True)
            await client.set(key, value, ex=ttl or None)
        except InfraError:
            raise
        except _backend_errors() as exc:
            raise self._failure("set", exc) from None

    async def delete(self, key: str) -> None:
        check_key(key, instance=self.name)
        try:
            client = await self._operation_client(idempotent=True)
            await client.delete(key)
        except InfraError:
            raise
        except _backend_errors() as exc:
            raise self._failure("delete", exc) from None

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        check_key(key, instance=self.name)
        check_value(value, limit=self._options.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        try:
            client = await self._operation_client(idempotent=False)
            return bool(await client.set(key, value, nx=True, ex=ttl or None))
        except InfraError:
            raise
        except _backend_errors() as exc:
            raise self._failure("add", exc) from None

    async def _script(
        self,
        operation: str,
        script: str,
        key: str,
        args: list[Any],
        *,
        idempotent: bool = False,
    ) -> Any:
        try:
            client = await self._operation_client(idempotent=idempotent)
            return await _run_script(client, script, _SHAS[script], [key], args)
        except InfraError:
            raise
        except _backend_errors() as exc:
            raise self._failure(operation, exc) from None

    async def add_or_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]:
        check_key(key, instance=self.name)
        check_value(value, limit=self._options.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        reply = await self._script("add_or_get", _ADD_OR_GET_LUA, key, [value, ttl])
        if int(reply[0]) == 1:
            return True, None
        return False, _text(reply[1])

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        check_key(key, instance=self.name)
        check_value(expected, limit=self._options.max_value_bytes, instance=self.name)
        check_value(value, limit=self._options.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        swapped = await self._script(
            "compare_and_set", _CAS_LUA, key, [expected, value, ttl]
        )
        return bool(int(swapped))

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        check_key(key, instance=self.name)
        check_value(expected, limit=self._options.max_value_bytes, instance=self.name)
        deleted = await self._script("compare_and_delete", _CAD_LUA, key, [expected])
        return bool(int(deleted))

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        check_key(key, instance=self.name)
        check_subject_state(value, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        held = await self._script(
            "set_subject_state",
            _SUBJECT_STATE_LUA,
            key,
            [value, ttl],
            idempotent=True,  # the same value written twice leaves one state
        )
        return _text(held)

    # ------------------------------------------------------------- lifecycle
    def _assume_refused(self) -> bool:
        """``assume`` without the kernel's leave (production, no accepted risk)."""
        return self._options.never_evicts_check == "assume" and not (
            self._assume_allowed is not None and self._assume_allowed()
        )

    async def _check_policy(self, operation: str) -> None:
        """Judge the server's eviction policy; a refusal raises, and the check
        stays open until one passes (``aopen`` and the first operation of a
        process that never ran it)."""
        self._policy_checked = False
        if self._assume_refused():  # refused before the server is asked
            refusal = self._connection_refusal(None)
            if refusal is not None:
                raise refusal
        try:
            client = await self._client_obj()
            policy = await read_policy(client.config_get(POLICY_SETTING))
            if policy is None:
                await client.ping()  # an unreachable server is Unavailable
        except InfraError:
            raise
        except _backend_errors() as exc:
            raise self._failure(operation, exc) from None
        refusal = self._connection_refusal(policy)
        if refusal is not None:
            raise refusal
        self._policy_checked = True

    async def aopen(self) -> None:
        if self._client_factory is None:  # a client of its own needs the url
            require_url("atomic_store", self.name, self._options.url)
        register_endpoint(
            "atomic_store",
            self.name,
            redis_endpoint(self._options.url),
            "persistent",
            option=f"atomic_store.{self.name}.url",
            declared=not self.implicit,
        )
        await self._check_policy("aopen")

    async def health(self) -> HealthResult:
        if self._assume_refused():
            self._policy_checked = False
            return HealthResult(
                status="fail", details={"reason": "eviction_check_refused"}
            )
        try:
            client = await self._client_obj()
            if not await client.ping():
                return HealthResult(status="fail", details={"error": "no-pong"})
            policy = await read_policy(client.config_get(POLICY_SETTING))
        except _probe_errors() as exc:
            # The server may come back reconfigured: the operations judge its
            # policy again. The type only: a redis error text names the host
            # (spec 2.4).
            self._policy_checked = False
            refusal = refusal_of(exc)
            if refusal is not None:  # the pool refused a socket of the server
                return HealthResult(
                    status="fail", details={"reason": refusal_reason(refusal)}
                )
            return HealthResult(status="fail", details=_failure_details(exc))
        if policy is None:
            if self._options.never_evicts_check == "verify":
                self._policy_checked = False  # the operations judge it again
                return HealthResult(
                    status="fail", details={"reason": "noeviction_unverifiable"}
                )
            return HealthResult(
                status="degraded", details={"reason": "noeviction_unverified"}
            )
        if policy != NOEVICTION:
            self._policy_checked = False  # the operations judge it again
            return HealthResult(status="fail", details={"reason": "evicting_policy"})
        return HealthResult(status="ok")

    async def isolation_identity(self) -> str | None:
        """``redis:<run_id>`` of the reached server (``INFO server``), the same
        through DNS aliases: two instances with one identity share eviction."""
        try:
            client = await self._client_obj()
            info = await client.info(section="server")
        except InfraError:
            raise
        except _backend_errors() as exc:
            raise self._failure("isolation_identity", exc) from None
        run_id = info.get("run_id") or info.get(b"run_id")
        if isinstance(run_id, bytes):
            run_id = run_id.decode("ascii", "replace")
        if not isinstance(run_id, str) or not run_id:
            raise Unavailable(
                f"atomic_store {self.name!r}: INFO server returned no run_id",
                capability=self.capability,
                instance=self.name,
            )
        return f"redis:{run_id}"

    async def aclose(self) -> None:
        unregister_endpoint("atomic_store", self.name, "persistent")
        self._policy_checked = False
        clients = dict.fromkeys(
            c for c in (self._client, self._once_client) if c is not None
        )
        self._client = self._once_client = None
        for client in clients:
            try:
                await client.aclose()
            except AttributeError:  # pragma: no cover - older redis-py
                await client.close()


def _text(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


__all__ = [
    "UNVERIFIED_NOEVICTION_RISK",
    "NeverEvictsCheck",
    "RedisAtomicStoreOptions",
    "RedisAtomicStoreProvider",
]
