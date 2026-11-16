"""The eviction check of a pooled connection, run before its first command.

The atomic store and the lease must never talk to a server that evicts keys (a
revocation mark or a fence counter would vanish before its TTL). A pool opens a
socket whenever it grows and reconnects a dropped one without raising (the
retrying client and the pool's own liveness probe both do), and after a failover
or a DNS repoint any of those sockets can reach a server that came back
reconfigured. A flag that says "the policy was checked once" cannot judge that
socket: the command that caused the reconnect would already be on its way.

So the judgement sits where the socket is born: :func:`connect_func` is the
``redis_connect_func`` of the pool. After the connection's own ``on_connect``
(auth, ``SELECT``, client setup) it sends ``CONFIG GET maxmemory-policy`` on
THAT connection, on every connect (the first included), and hands the answer to
the driver's ``judge`` (:func:`eviction_refusal` is the one verdict,
:func:`read_policy` the one reader of the reply). A refusal raises
:class:`EvictionCheckRefused`, a ``RedisError`` (no ``ConnectionError``, which
the client's ``Retry`` would dial again) that carries the driver's own
``InfraError``: redis-py then drops the socket (no command is sent on it), and
the driver's error mapping answers the carried refusal in place of
``Unavailable`` (:func:`refusal_of`). A transient failure of the read itself (a
connection error, ``LOADING``) is not a refusal: it propagates and maps like any
other. The price is one ``CONFIG GET`` round trip per new socket.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from functools import cache
from typing import Any

from sandloom.driverkit import (
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Timeout,
    Unavailable,
)
from sandloom_redis._errors import redis_error_kind

POLICY_SETTING = "maxmemory-policy"
NOEVICTION = "noeviction"
#: The accepted risk that lets ``never_evicts_check=assume`` run in production.
UNVERIFIED_NOEVICTION_RISK = "unverified_noeviction"

#: What a driver answers for the policy a connection reported (``None``: the
#: read was refused): the refusal to raise, or ``None`` when the server is fit.
Judge = Callable[[str | None], InfraError | None]


@cache
def _refused_class() -> type[BaseException]:
    """``EvictionCheckRefused``, built when first needed: redis-py is imported
    on first use, so the class cannot be defined at module level."""
    redis_error = importlib.import_module("redis.exceptions").RedisError

    class EvictionCheckRefused(redis_error):  # type: ignore[misc, valid-type]
        """A connection refused because its server's eviction policy is not
        fit; ``args[0]`` is the driver's ``InfraError`` for it (its text names
        the instance and the fix, never a host or a secret). A ``RedisError``
        and no ``ConnectionError``: the client's ``Retry`` repeats only a
        ``ConnectionError``, and a refusal is a verdict, not an outage, so it
        is never dialled again within the same call."""

    return EvictionCheckRefused


def refusal_of(exc: BaseException) -> InfraError | None:
    """The refusal a connection's eviction check raised, or ``None`` for any
    other error (which the caller maps as before)."""
    if isinstance(exc, _refused_class()) and exc.args:
        refusal = exc.args[0]
        if isinstance(refusal, InfraError):
            return refusal
    return None


def _policy_of(reply: object) -> str | None:
    """``maxmemory-policy`` out of a raw ``CONFIG GET`` reply: a flat
    ``[name, value]`` list (RESP2) or a mapping (RESP3), text or bytes."""
    if isinstance(reply, dict):
        pairs = reply
    elif isinstance(reply, list | tuple):
        pairs = dict(zip(reply[::2], reply[1::2], strict=False))
    else:
        return None
    value = pairs.get(POLICY_SETTING, pairs.get(POLICY_SETTING.encode()))
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return value if isinstance(value, str) else None


async def read_policy(reply: Awaitable[object]) -> str | None:
    """The server's ``maxmemory-policy`` out of a ``CONFIG GET`` reply, or
    ``None`` when the server refused the read (``NOPERM``, a disabled or renamed
    ``CONFIG``). A connection failure or a transient reply (``LOADING``,
    ``BUSY`` ...) is no refusal and propagates: recording it would be a standing
    ``PermissionDenied``. The one reader of every driver and of the pool."""
    response_error = importlib.import_module("redis.exceptions").ResponseError
    try:
        return _policy_of(await reply)
    except response_error as exc:
        if redis_error_kind(exc) in (Unavailable, Timeout):
            raise
        return None


def eviction_refusal(
    *,
    capability: str,
    instance: str,
    policy: str | None,
    never_evicts_check: str,
    assume_refused: bool,
    at_stake: str,
) -> InfraError | None:
    """The one verdict on a server's eviction policy, for every driver.

    ``policy`` is what the server reported (``None``: it refused the read);
    ``assume_refused`` is ``assume`` without the kernel's leave (production
    without the accepted risk); ``at_stake`` names what an eviction would break.
    Returns the refusal, or ``None`` when the server is fit. The texts name the
    instance and the fix, never a host or a secret."""
    where = f"{capability} instance {instance!r}"
    if assume_refused:
        return PermissionDenied(
            f"{where}: never_evicts_check='assume' needs the accepted risk "
            f"{UNVERIFIED_NOEVICTION_RISK!r} in production (fix: set "
            f"never_evicts_check='verify' and grant CONFIG GET {POLICY_SETTING}, "
            "or accept the risk with "
            f'SANDLOOM_ACCEPTED_RISKS=["{UNVERIFIED_NOEVICTION_RISK}"])',
            capability=capability,
            instance=instance,
        )
    if policy is not None and policy != NOEVICTION:
        return InvalidArgument(
            f"{where}: the Redis server evicts keys ({POLICY_SETTING} is "
            f"{policy!r}), so {at_stake} "
            f"(fix: set {POLICY_SETTING} to {NOEVICTION!r})",
            capability=capability,
            instance=instance,
        )
    if policy is None and never_evicts_check == "verify":
        return PermissionDenied(
            f"{where}: the Redis server refused CONFIG GET {POLICY_SETTING}, so "
            "the driver cannot verify that it never evicts (fix: grant CONFIG GET "
            f"{POLICY_SETTING} to this user, or vouch for the policy with the "
            "option never_evicts_check='assume')",
            capability=capability,
            instance=instance,
        )
    return None


def refusal_reason(refusal: InfraError) -> str:
    """The ``details["reason"]`` of a health probe a refusal failed."""
    if isinstance(refusal, InvalidArgument):
        return "evicting_policy"
    return "noeviction_unverifiable"


def connect_func(judge: Judge) -> Callable[[Any], Awaitable[None]]:
    """The ``redis_connect_func`` of a pool: the connection's own ``on_connect``,
    then the eviction check of its server, on this connection, every time."""

    async def connect(connection: Any) -> None:
        await connection.on_connect()
        await connection.send_command("CONFIG GET", POLICY_SETTING)
        refusal = judge(await read_policy(connection.read_response()))
        if refusal is not None:
            raise _refused_class()(refusal)

    return connect
