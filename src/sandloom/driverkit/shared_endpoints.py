"""Which instances share one Redis database: the startup warning's rendezvous.

The ``cache`` is allowed to evict (``maxmemory-policy allkeys-lru`` is the usual
setting). An ``atomic_store``, a ``lease``, a ``streams`` bus or a task queue's
broker on the same Redis database as ``cache.default`` can lose live keys when
the cache fills up: an idempotency record, a lease holder, a stream entry, a
queued task. No url is shared by inheritance (each instance states its own), so
a shared database is a choice the operator made, or a url copied into several
options. This module says so at startup, once per pair, as a WARNING and never a
refusal (a development setup shares one Redis on purpose).

Instances of different driver packages do not start in a fixed order and import
nothing of each other (a driver package imports only ``sandloom.driverkit``), so
the check is a rendezvous here: the driver of ``cache.default`` registers the
endpoint it serves (``role="cache_default"``), each driver of a persistent
instance registers the endpoint it opens on (``role="persistent"``), and the
registration that comes second finds the other and logs.

An endpoint is an opaque string both sides compute alike: :func:`redis_endpoint`
gives ``host:port/db`` of a ``redis://`` / ``rediss://`` url and never carries a
credential. A persistent instance the kernel synthesized (``declared=False``:
nobody wrote it in ``INFRA``, so nobody chose it) is logged at INFO, never as a
WARNING: a development default (an instance the composition selects without
an ``INFRA`` entry) shares by construction and a warning every start would hide
the one that matters. In production nothing counts as a development default: a
synthesized instance that shares the default cache's Redis is a WARNING there
too (``InfraProvider.implicit`` is never true in production).

An instance unregisters when it closes (:func:`unregister`); a test harness
starts clean with :func:`reset`.
"""

from __future__ import annotations

import logging
from typing import Literal
from urllib.parse import parse_qs, urlsplit

__all__ = ["Role", "redis_endpoint", "register", "reset", "unregister"]

logger = logging.getLogger(__name__)

#: ``cache_default``: the endpoint the ``cache.default`` instance serves.
#: ``persistent``: the endpoint of an instance that must not lose a key.
Role = Literal["cache_default", "persistent"]

_REDIS_SCHEMES = frozenset({"redis", "rediss"})
_DEFAULT_PORT = 6379
_ROLES: tuple[Role, ...] = ("cache_default", "persistent")

_Key = tuple[str, str]


class _Entry:
    __slots__ = ("declared", "endpoint", "option")

    def __init__(self, endpoint: str, option: str, *, declared: bool) -> None:
        self.endpoint = endpoint
        self.option = option
        self.declared = declared


_entries: dict[Role, dict[_Key, _Entry]] = {role: {} for role in _ROLES}
#: ``(persistent key, cache key, endpoint)`` of the pairs already logged.
_logged: set[tuple[_Key, _Key, str]] = set()


def redis_endpoint(url: str | None) -> str | None:
    """``host:port/db`` of a ``redis://`` / ``rediss://`` url (host lower-cased,
    port 6379 and db 0 when left out; ``?db=N`` read as redis-py does), or
    ``None`` for anything else: another scheme, no host, a malformed url, or a
    path that still holds an ``@`` (an unencoded password: no part of it is
    echoed). No user name, password or query option other than ``db`` is part
    of the result."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port or _DEFAULT_PORT
    except ValueError:
        return None
    if parts.scheme not in _REDIS_SCHEMES or not parts.hostname:
        return None
    # An ``@`` anywhere after the authority: an unencoded ``?`` / ``#`` /
    # ``/`` in the password moved the rest of it (and the real host) out of the
    # netloc, so what is left would echo user and password digits as host:port.
    if "@" in parts.path + parts.query + parts.fragment:
        return None
    raw_db = parts.path.lstrip("/") or (parse_qs(parts.query).get("db") or ["0"])[0]
    try:
        db = int(raw_db) if raw_db.isdecimal() else 0
    except ValueError:  # more digits than int() converts (a 4300+ digit path)
        return None
    return f"{parts.hostname.lower()}:{port}/{db}"


def _say(persistent: _Key, entry: _Entry, cache: _Key) -> None:
    pair = (persistent, cache, entry.endpoint)
    if pair in _logged:
        return
    _logged.add(pair)
    capability, instance = persistent
    logger.log(
        logging.WARNING if entry.declared else logging.INFO,
        "%s instance %r runs on the same Redis database as %s.%s (%s): a cache "
        "that evicts can drop the keys this instance must keep. Give it a "
        "Redis of its own (INFRA option %s) with maxmemory-policy noeviction.",
        capability,
        instance,
        cache[0],
        cache[1],
        entry.endpoint,
        entry.option,
    )


def register(
    capability: str,
    instance: str,
    endpoint: str | None,
    role: Role,
    *,
    option: str = "",
    declared: bool = True,
) -> None:
    """The instance ``capability.instance`` serves (``role="cache_default"``) or
    opens on (``role="persistent"``) ``endpoint``; ``None`` (a url that is not a
    Redis one) unregisters it. Whichever of the two roles registers second logs
    once per ``(persistent, cache, endpoint)``.

    ``option`` names the INFRA option the message tells the operator to change
    (``atomic_store.security.url``); ``declared`` is ``False`` for an instance
    the kernel synthesized. Registering again replaces the earlier entry.
    """
    if role not in _ROLES:
        raise ValueError(f"role must be one of {_ROLES}, not {role!r}")
    key = (capability, instance)
    if endpoint is None:
        unregister(capability, instance, role)
        return
    entry = _Entry(endpoint, option, declared=declared)
    _entries[role][key] = entry
    if role == "persistent":
        for cache_key, cache in _entries["cache_default"].items():
            if cache.endpoint == endpoint:
                _say(key, entry, cache_key)
    else:
        for persistent_key, persistent in _entries["persistent"].items():
            if persistent.endpoint == endpoint:
                _say(persistent_key, persistent, key)


def unregister(capability: str, instance: str, role: Role) -> None:
    """The instance closed: forget it and the pairs logged for it, so a later
    app in the same process neither warns about it nor stays silent about a
    new pair. Unknown instance: nothing happens.

    The key is ``(capability, instance, role)`` and carries no app: two apps in
    one process that register the same names share one entry, and the first to
    close forgets it for both. A process runs one app, so the kernel never does
    this; a harness that builds several apps in one process calls :func:`reset`
    between them."""
    key = (capability, instance)
    _entries[role].pop(key, None)
    position = 0 if role == "persistent" else 1
    for pair in [pair for pair in _logged if pair[position] == key]:
        _logged.discard(pair)


def reset() -> None:
    """Forget every registration and every logged pair (a test harness)."""
    for entries in _entries.values():
        entries.clear()
    _logged.clear()
