"""The one Redis error -> ``InfraError`` classifier of the package (spec 2.1).

Which Redis errors mean what is Redis knowledge, so it lives here and not in
``driverkit``. The cache, atomic store, lease, broadcast and Streams drivers
all map through :func:`map_redis_error`; a parity test pins the table.

=============================================  ====================
what Redis (or the socket) raised               the ``InfraError``
=============================================  ====================
a connection error, ``OSError``, a bad reply     ``Unavailable``
``LOADING`` ``READONLY`` ``MASTERDOWN``
``TRYAGAIN`` ``CLUSTERDOWN`` ``BUSY`` ``OOM``
``MISCONF`` ``NOREPLICAS`` ``MOVED`` ``ASK``
``UNBLOCKED``                                    ``Unavailable``
a timeout (the client's or the socket's)         ``Timeout``
``AuthenticationError``, ``NoPermissionError``,
``NOAUTH`` / ``WRONGPASS`` / ``NOPERM``          ``PermissionDenied``
``DataError`` (a value the client cannot send)   ``InvalidArgument``
any OTHER ``ResponseError`` (an unknown
``ERR ...``: a refused value, a script error)    ``InvalidArgument``
anything that is not a Redis failure (a          re-raised as itself,
``TypeError`` of a bug)                          never mapped
=============================================  ====================

The last row is the one that matters: the outbox relay trusts the mapped class,
so an error mapped to the retryable ``Unavailable`` is an outage that never
spends an attempt of the row and never dead-letters it. A reply the server
sends for THIS request (it will answer the same again) is not an outage: it is
not retryable, so the relay counts an attempt and dead-letters a poison row.

The message carries the error's type name only, never its text: a Redis error
names the host, the port or a key. A ``MOVED`` / ``ASK`` reply also sets the
error's ``hint`` (:data:`CLUSTER_REDIRECT_HINT`, a fixed sentence) so the relay's
outage warning says why; the hint labels a line and decides nothing.

Chaining (``raise ... from``) stays with each call site, which knows who reads
the chain. The relay walks ``__cause__`` through the Streams transport and the
pub/sub broadcast, so those raise ``from exc``; the cache and the atomic store
answer a request unit that branches on the class only, so they raise
``from None`` and the raw error (its text names the host) never reaches a log.
"""

from __future__ import annotations

import importlib
import re
from functools import cache
from types import ModuleType

from sandloom.driverkit import (
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Timeout,
    Unavailable,
)

#: Replies of a server that is loading, failing over, overloaded, full, unable
#: to persist (``MISCONF``: the last background save failed and the server stops
#: writes; ``NOREPLICAS``: ``min-replicas-to-write`` is unmet) or that is not the
#: node that owns the key (``MOVED`` / ``ASK``: a single-node URL pointed at a
#: cluster node; ``UNBLOCKED``: a blocked read cut by a failover). The same
#: command may succeed later, or against the right broker: none of it is the
#: request's fault. Matched on the reply's first word, for the errors the client
#: does not turn into its own class.
TRANSIENT_REPLIES = frozenset(
    {
        "LOADING",
        "READONLY",
        "MASTERDOWN",
        "TRYAGAIN",
        "CLUSTERDOWN",
        "BUSY",
        "OOM",
        "MISCONF",
        "NOREPLICAS",
        "MOVED",
        "ASK",
        "UNBLOCKED",
    }
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
        exceptions.MovedError,  # the cluster client's classes: the text lost the word
        exceptions.AskError,
    )


@cache
def _backend_types() -> tuple[type[BaseException], ...]:
    """What a Redis backend raises: the client's own errors, a socket's
    ``OSError``, a timeout and an undecodable stored value (the cache and the
    atomic store name it a backend failure)."""
    return (
        _redis_exceptions().RedisError,
        OSError,
        TimeoutError,
        UnicodeDecodeError,
    )


#: What redis-py puts before the reply of a command rejected inside a pipeline
#: (``Command # 1 (XADD ...) of pipeline caused error: ('MASTERDOWN ...',)``).
_PIPELINE_NOTE_START = "Command # "
_PIPELINE_NOTE_END = ") of pipeline caused error: "
_WORD = re.compile(r"\w+")


def _reply_word(exc: BaseException) -> str:
    """The first word of the reply text (``"BUSY"`` of ``"BUSY script ..."``).

    A command rejected at queue time inside ``MULTI`` carries the client's
    annotation before the reply: the word is the reply's, taken after the LAST
    ``... of pipeline caused error: `` (the command, whose arguments can hold
    any payload, comes before it; the reply comes after and is the server's).
    """
    text = str(exc)
    if text.startswith(_PIPELINE_NOTE_START) and _PIPELINE_NOTE_END in text:
        reply = text.rpartition(_PIPELINE_NOTE_END)[2].lstrip("('\"")
        found = _WORD.match(reply)
        return found.group() if found else ""
    return text.split(" ", 1)[0]


#: The log hint of a reply from a cluster node that does not own the key (a plain
#: client pointed at a Redis Cluster gets ``MOVED`` / ``ASK`` for most keys and
#: retries for ever). A fixed sentence: the ``host:port`` of the reply stays out.
CLUSTER_REDIRECT_HINT = "cluster redirect: is the client cluster-aware?"
_CLUSTER_REDIRECT_WORDS = frozenset({"MOVED", "ASK"})


def _is_cluster_redirect(exc: BaseException) -> bool:
    """Whether ``exc`` is a ``MOVED`` / ``ASK`` reply, by its text or, for the
    cluster client's classes (their text lost the word), by its class."""
    exceptions = _redis_exceptions()
    if not isinstance(exc, exceptions.ResponseError):
        return False
    return _reply_word(exc) in _CLUSTER_REDIRECT_WORDS or isinstance(
        exc, exceptions.MovedError | exceptions.AskError
    )


def redis_error_kind(exc: BaseException) -> type[InfraError]:
    """The ``InfraError`` class of a Redis or socket failure (the table above).

    An allow-list for the retryable answer: only a connection fault, a timeout
    and the transient replies are outages; any other ``ResponseError`` is the
    server refusing this request and is not retryable. An exception that is not
    a backend failure at all (``RedisError``, ``OSError``, ``TimeoutError``,
    ``UnicodeDecodeError``) is re-raised as itself, unmapped.
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
    if isinstance(exc, _backend_types()):
        # ConnectionError (also BusyLoadingError), a socket OSError, a bad reply,
        # a cluster or pub/sub fault: the same call may succeed later.
        return Unavailable
    # Not a Redis failure (a TypeError of a bug): never an outage, which the
    # relay retries for ever. The caller's own ``raise`` of the result never runs.
    raise exc


def map_redis_error(
    exc: BaseException, *, capability: str, instance: str | None, what: str
) -> InfraError:
    """``exc`` as an ``InfraError`` named ``"<what>: <type of exc>"``.

    ``what`` says what was being done (``"cache 'default' get"``). The caller
    raises the result (``from exc`` or ``from None``, see the module docstring).
    A non-backend ``exc`` is re-raised as itself (see :func:`redis_error_kind`).
    """
    kind = redis_error_kind(exc)
    return kind(
        f"{what}: {type(exc).__name__}",
        capability=capability,
        instance=instance,
        hint=CLUSTER_REDIRECT_HINT if _is_cluster_redirect(exc) else None,
    )
