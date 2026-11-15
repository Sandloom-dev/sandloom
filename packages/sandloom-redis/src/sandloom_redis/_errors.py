"""The one Redis error -> ``InfraError`` classifier of the package (spec 2.1).

Which Redis errors mean what is Redis knowledge, so it lives here and not in
``driverkit``. The cache, atomic store, lease, broadcast and Streams drivers
all map through :func:`map_redis_error`; a parity test pins the table.

=============================================  ====================
what Redis (or the socket) raised               the ``InfraError``
=============================================  ====================
a connection error, ``OSError``, a bad reply     ``Unavailable``
``LOADING`` ``READONLY`` ``MASTERDOWN``
``TRYAGAIN`` ``CLUSTERDOWN`` ``BUSY`` ``OOM``    ``Unavailable``
a timeout (the client's or the socket's)         ``Timeout``
``AuthenticationError``, ``NoPermissionError``,
``NOAUTH`` / ``WRONGPASS`` / ``NOPERM``          ``PermissionDenied``
``DataError`` (a value the client cannot send)   ``InvalidArgument``
any OTHER ``ResponseError`` (an unknown
``ERR ...``: a refused value, a script error)    ``InvalidArgument``
=============================================  ====================

The last row is the one that matters: the outbox relay trusts the mapped class,
so an error mapped to the retryable ``Unavailable`` is an outage that never
spends an attempt of the row and never dead-letters it. A reply the server
sends for THIS request (it will answer the same again) is not an outage: it is
not retryable, so the relay counts an attempt and dead-letters a poison row.

The message carries the error's type name only, never its text: a Redis error
names the host, the port or a key.

Chaining (``raise ... from``) stays with each call site, which knows who reads
the chain. The relay walks ``__cause__`` through the Streams transport and the
pub/sub broadcast, so those raise ``from exc``; the cache and the atomic store
answer a request unit that branches on the class only, so they raise
``from None`` and the raw error (its text names the host) never reaches a log.
"""

from __future__ import annotations

import importlib
from functools import cache
from types import ModuleType

from sandloom.driverkit import (
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Timeout,
    Unavailable,
)

#: Replies of a server that is loading, failing over, overloaded or full: the
#: same command may succeed later. Matched on the reply's first word, for the
#: errors the client does not turn into its own class.
TRANSIENT_REPLIES = frozenset(
    {"LOADING", "READONLY", "MASTERDOWN", "TRYAGAIN", "CLUSTERDOWN", "BUSY", "OOM"}
)

#: Replies that refuse the credentials or the command for this user.
REFUSED_REPLIES = frozenset({"NOAUTH", "WRONGPASS", "NOPERM"})


def _redis_exceptions() -> ModuleType:
    """``redis.exceptions``, imported when an error is classified."""
    return importlib.import_module("redis.exceptions")


@cache
def _transient_types() -> tuple[type[BaseException], ...]:
    """The client's own classes for a transient reply (``MasterDownError`` is a
    ``ClusterDownError``)."""
    exceptions = _redis_exceptions()
    return (
        exceptions.ReadOnlyError,
        exceptions.OutOfMemoryError,
        exceptions.ClusterDownError,
        exceptions.TryAgainError,
    )


def _reply_word(exc: BaseException) -> str:
    """The first word of the reply text (``"BUSY"`` of ``"BUSY script ..."``)."""
    return str(exc).split(" ", 1)[0]


def redis_error_kind(exc: BaseException) -> type[InfraError]:
    """The ``InfraError`` class of a Redis or socket failure (the table above).

    An allow-list for the retryable answer: only a connection fault, a timeout
    and the transient replies are outages; any other ``ResponseError`` is the
    server refusing this request and is not retryable.
    """
    exceptions = _redis_exceptions()
    if isinstance(
        exc,
        exceptions.AuthenticationError
        | exceptions.AuthorizationError
        | exceptions.NoPermissionError,
    ):
        return PermissionDenied
    if isinstance(exc, exceptions.TimeoutError | TimeoutError):
        return Timeout
    if isinstance(exc, exceptions.ResponseError):
        word = _reply_word(exc)
        if word in REFUSED_REPLIES:
            return PermissionDenied
        if word in TRANSIENT_REPLIES or isinstance(exc, _transient_types()):
            return Unavailable
        return InvalidArgument
    if isinstance(exc, exceptions.DataError):
        return InvalidArgument
    # ConnectionError (also BusyLoadingError), a socket OSError, a bad reply,
    # a cluster or pub/sub fault: the same call may succeed later.
    return Unavailable


def map_redis_error(
    exc: BaseException, *, capability: str, instance: str | None, what: str
) -> InfraError:
    """``exc`` as an ``InfraError`` named ``"<what>: <type of exc>"``.

    ``what`` says what was being done (``"cache 'default' get"``). The caller
    raises the result (``from exc`` or ``from None``, see the module docstring).
    """
    kind = redis_error_kind(exc)
    return kind(
        f"{what}: {type(exc).__name__}", capability=capability, instance=instance
    )
