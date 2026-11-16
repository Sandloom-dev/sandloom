"""A persistent store on the cache's Redis: the startup warning.

The ``cache`` is allowed to evict (``maxmemory-policy allkeys-lru`` is the usual
setting). An ``atomic_store``, a ``lease``, a ``streams`` bus or a Celery broker
on the same Redis database as ``cache.default`` can lose live keys when the
cache fills up: an idempotency record, a lease holder, a stream entry, a queued
task. No url is shared by inheritance any more (each instance states its own),
so a shared database is a choice the operator made, or a copy of one url into
several options: this module says so at startup, once per pair, as a WARNING
and never a refusal (a development setup shares one Redis on purpose).

The instances do not start in a fixed order, so the check is a rendezvous: the
Redis ``cache.default`` states its url when it is built
(:func:`register_default_cache`), each persistent instance when it opens
(:func:`warn_if_shares_default_cache`), and whichever comes second finds the
other and warns.

The log line names both instances and the endpoint as ``host:port/db``: the
credentials of a url never reach it.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

_REDIS_SCHEMES = frozenset({"redis", "rediss"})
_DEFAULT_PORT = 6379

#: ``host:port/db`` of the ``cache.default`` instance built from the redis driver.
_default_cache: str | None = None
#: ``(capability, instance) -> endpoint`` of the persistent instances that opened.
_persistent: dict[tuple[str, str], str] = {}


def endpoint(url: str | None) -> str | None:
    """``host:port/db`` of a ``redis://`` / ``rediss://`` url (host lower-cased,
    port 6379 and db 0 when left out; ``?db=N`` read as redis-py does), or
    ``None`` for anything else (another scheme, no host, a malformed url). No
    user name, password or query option other than ``db`` is part of it."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port or _DEFAULT_PORT
    except ValueError:
        return None
    if parts.scheme not in _REDIS_SCHEMES or not parts.hostname:
        return None
    raw_db = parts.path.lstrip("/") or (parse_qs(parts.query).get("db") or ["0"])[0]
    db = int(raw_db) if raw_db.isdecimal() else 0
    return f"{parts.hostname.lower()}:{port}/{db}"


def _warn(capability: str, instance: str, where: str) -> None:
    logger.warning(
        "%s instance %r runs on the same Redis database as cache.default (%s): "
        "a cache that evicts can drop the keys this instance must keep. Give it "
        "a Redis of its own (INFRA option %s.%s.url, or .broker for a task "
        "queue) with maxmemory-policy noeviction.",
        capability,
        instance,
        where,
        capability,
        instance,
    )


def register_default_cache(url: str | None) -> None:
    """The ``cache.default`` instance is built on ``url`` (``None``: no url):
    warn for each persistent instance that already opened on its endpoint."""
    global _default_cache
    where = endpoint(url)
    previous, _default_cache = _default_cache, where
    if where is None or where == previous:
        return
    for (capability, instance), own in _persistent.items():
        if own == where:
            _warn(capability, instance, where)


def warn_if_shares_default_cache(
    capability: str, instance: str, url: str | None
) -> None:
    """A persistent instance opens on ``url``: warn once when that is the
    endpoint of the ``cache.default`` built from the redis driver, or when
    ``cache.default`` is built on it later (:func:`register_default_cache`).

    Nothing is logged when the default cache is not a Redis one, when either url
    is not a ``redis://`` url, or when the endpoints differ.
    """
    own = endpoint(url)
    if own is None:
        _persistent.pop((capability, instance), None)
        return
    _persistent[(capability, instance)] = own
    if own == _default_cache:
        _warn(capability, instance, own)
