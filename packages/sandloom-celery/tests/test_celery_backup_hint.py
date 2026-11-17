"""The Celery driver's ``backup_hint``: a broker on Redis is a server to back up.

``sandloom-celery`` imports no other driver package, so it states the Redis
persistence step itself; the parity test keeps its text equal to the one
``sandloom-redis`` states for its own drivers (both packages are installed in
this lane's environment).
"""

from __future__ import annotations

import pytest

from sandloom.driverkit import BackupHint
from sandloom_celery.backup import _PERSISTENCE, broker_hint
from sandloom_redis import backup as redis_backup
from sandloom_celery.taskqueue import CeleryTaskQueueProvider


@pytest.mark.parametrize("scheme", ["redis", "rediss"])
def test_a_redis_broker_is_a_server_the_plan_lists(scheme: str) -> None:
    url = f"{scheme}://:pw@brokerhost:6379/4"
    hint = CeleryTaskQueueProvider.backup_hint({"broker": url})
    assert isinstance(hint, BackupHint)
    assert hint.url == url
    assert hint.kind == "redis"
    assert hint.detail == "broker"
    assert hint.backup == (_PERSISTENCE,)
    assert "uncertain task enqueues" in hint.restore[0]
    assert hint.tolerable_loss is None  # queued tasks are not a cache


@pytest.mark.parametrize(
    "options",
    [
        {"broker": "amqp://guest@mq:5672//"},
        {"broker": "memory://"},
        {"broker": ""},
        {"broker": None},
        {},
    ],
)
def test_another_broker_or_none_adds_nothing(options: dict[str, object]) -> None:
    assert CeleryTaskQueueProvider.backup_hint(options) is None
    assert broker_hint(options) is None


def test_the_persistence_step_equals_the_one_the_redis_drivers_state() -> None:
    # Both packages are installed in this lane (the beat-lease tests import the
    # Redis lease driver the same way): a missing one fails, never skips.
    assert _PERSISTENCE == redis_backup.AOF_STEP


def test_the_kind_equals_the_one_the_redis_drivers_state() -> None:
    """One server of one kind: a broker and a cache on it share a plan item."""
    hint = broker_hint({"broker": "redis://h:6379/0"})
    assert hint is not None
    assert hint.kind == redis_backup.KIND
