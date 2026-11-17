"""What a Celery broker on Redis adds to ``sandloom backup plan``
(``InfraProvider.backup_hint``).

The plan keeps one item per server, so a broker that runs on the Redis server of
the cache or the event bus joins that item. This package imports no other driver
package, so the persistence step is stated here as ``sandloom-redis`` states it
for its own drivers; ``tests/test_celery_backup_hint.py`` keeps the two equal.
A broker that is not Redis (``amqp://``, ``memory://``) adds nothing: the plan
has no item for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sandloom.driverkit import BackupHint

__all__ = ["broker_hint"]

_REDIS_SCHEMES = ("redis://", "rediss://")
_PERSISTENCE = (
    "appendonly yes + appendfsync everysec (AOF) AND periodic RDB "
    "snapshots (save / BGSAVE); copy dump.rdb + the appendonly dir off-host"
)
_QUEUE_RESTORE = (
    "queued Celery tasks newer than the snapshot are lost and older ones "
    "may run again: task effects need DB-atomic idempotency; re-enqueue "
    "what is missing (docs/operate/operator-reference.md: "
    "uncertain task enqueues)"
)


def broker_hint(options: Mapping[str, Any]) -> BackupHint | None:
    """The Redis server a ``broker`` url names, or ``None`` for another broker."""
    broker = options.get("broker")
    if not isinstance(broker, str) or not broker.startswith(_REDIS_SCHEMES):
        return None
    return BackupHint(
        url=broker,
        kind="redis",
        detail="broker",
        backup=(_PERSISTENCE,),
        restore=(_QUEUE_RESTORE,),
    )
