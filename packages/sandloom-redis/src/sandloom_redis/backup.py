"""What the Redis drivers add to ``sandloom backup plan``.

The hook is ``InfraProvider.backup_hint``.

The kernel's plan keeps one item per server: the cache, the atomic store and the
Streams bus whose urls name one Redis server share it, and it prints each step
once. This module is the Redis knowledge of that item: which persistence a
server needs (AOF and RDB), that a store the kernel relies on must never evict,
and what a restore of each kind of data costs.

``sandloom-celery`` states the same persistence step for a broker on Redis
(it imports no other driver package: a parity test keeps the two texts equal).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sandloom.driverkit import BackupHint

__all__ = [
    "AOF_STEP",
    "KIND",
    "NOEVICTION_STEP",
    "atomic_store_hint",
    "cache_hint",
    "streams_hint",
]

#: The plan item kind of a Redis server.
KIND = "redis"

#: Persistence every Redis server of the plan needs.
AOF_STEP = (
    "appendonly yes + appendfsync everysec (AOF) AND periodic RDB "
    "snapshots (save / BGSAVE); copy dump.rdb + the appendonly dir off-host"
)
#: What an atomic store (revocation marks, user blocks, idempotency records)
#: needs of its server: the drivers refuse to start on one that evicts.
NOEVICTION_STEP = (
    "maxmemory-policy noeviction (an evicted block mark = an unblocked "
    "user); a SEPARATE instance from the best-effort cache in prod"
)
_CACHE_TOLERABLE = (
    "losing it is tolerable: rate-limit windows reset, cache-backed "
    "idempotency replays are forgotten (DB-atomic dedupe is unaffected)"
)
_STREAMS_RESTORE = (
    "events delivered to the stream after the snapshot are lost from it "
    "(the outbox already marked them delivered): rebuild projections "
    "(sandloom projection rebuild) and replay affected consumers by hand",
    "consumer-group offsets come back older: consumers re-read and dedupe",
)


def _url(options: Mapping[str, Any]) -> str | None:
    """The instance's ``url`` option, or ``None`` when it states none."""
    url = options.get("url")
    return url if isinstance(url, str) and url else None


def cache_hint(options: Mapping[str, Any]) -> BackupHint | None:
    """The ``cache`` driver: tolerable to lose, persistence advised."""
    url = _url(options)
    if url is None:
        return None
    return BackupHint(
        url=url,
        kind=KIND,
        backup=(AOF_STEP,),
        tolerable_loss=_CACHE_TOLERABLE,
    )


def atomic_store_hint(options: Mapping[str, Any]) -> BackupHint | None:
    """The ``atomic_store`` driver: a server that must never evict."""
    url = _url(options)
    if url is None:
        return None
    return BackupHint(url=url, kind=KIND, backup=(NOEVICTION_STEP, AOF_STEP))


def streams_hint(options: Mapping[str, Any]) -> BackupHint:
    """The ``streams`` event bus: the stream is data the outbox no longer holds.

    A bus with no ``url`` of its own runs on the default cache client, so its
    data lives where ``cache.default`` points (``url=None``)."""
    return BackupHint(
        url=_url(options),
        kind=KIND,
        criticality="required",
        backup=(AOF_STEP,),
        restore=_STREAMS_RESTORE,
    )
