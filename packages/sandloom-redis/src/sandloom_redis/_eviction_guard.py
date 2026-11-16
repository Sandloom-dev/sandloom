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
the driver's ``judge``. A refusal raises :class:`EvictionCheckRefused`, a
``ConnectionError`` subclass that carries the driver's own ``InfraError``:
redis-py then drops the socket (no command is sent on it), and the driver's
error mapping answers the carried refusal in place of ``Unavailable``
(:func:`refusal_of`). A transient failure of the read itself (a connection
error, ``LOADING``) is not a refusal: it propagates and maps like any other.
The price is one ``CONFIG GET`` round trip per new socket.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from functools import cache
from typing import Any

from sandloom.driverkit import InfraError, Timeout, Unavailable
from sandloom_redis._errors import redis_error_kind

POLICY_SETTING = "maxmemory-policy"

#: What a driver answers for the policy a connection reported (``None``: the
#: read was refused): the refusal to raise, or ``None`` when the server is fit.
Judge = Callable[[str | None], InfraError | None]


@cache
def _refused_class() -> type[BaseException]:
    """``EvictionCheckRefused``, built when first needed: redis-py is imported
    on first use, so the class cannot be defined at module level."""
    connection_error = importlib.import_module("redis.exceptions").ConnectionError

    class EvictionCheckRefused(connection_error):  # type: ignore[misc, valid-type]
        """A connection refused because its server's eviction policy is not
        fit; ``args[0]`` is the driver's ``InfraError`` for it (its text names
        the instance and the fix, never a host or a secret)."""

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


def connect_func(judge: Judge) -> Callable[[Any], Awaitable[None]]:
    """The ``redis_connect_func`` of a pool: the connection's own ``on_connect``,
    then the eviction check of its server, on this connection, every time."""
    response_error = importlib.import_module("redis.exceptions").ResponseError

    async def connect(connection: Any) -> None:
        await connection.on_connect()
        await connection.send_command("CONFIG GET", POLICY_SETTING)
        policy: str | None
        try:
            policy = _policy_of(await connection.read_response())
        except response_error as exc:
            if redis_error_kind(exc) in (Unavailable, Timeout):
                raise  # LOADING, BUSY ...: no verdict, an outage
            policy = None  # NOPERM, a disabled CONFIG: a refused read
        refusal = judge(policy)
        if refusal is not None:
            raise _refused_class()(refusal)

    return connect
