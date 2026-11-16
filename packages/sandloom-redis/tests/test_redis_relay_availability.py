"""The outbox relay's verdict on the replies the real ``redis`` client parses.

The relay matches a client's error by class name and reply prefix and imports
no client; the kernel's tests (``test_batch_publish.py``) run it on a stand-in.
Here the replies go through the client's own parser, so a class the client
renames or a reply it maps to another error fails the verdict.
"""

from __future__ import annotations

import pytest
from redis._parsers.base import BaseParser

from sandloom.core.tasks import relay


@pytest.mark.parametrize(
    "reply",
    [
        "LOADING Redis is loading the dataset in memory",
        "READONLY You can't write against a read only replica.",
        "MASTERDOWN Link with MASTER is down and replica-serve-stale-data is no",
        "TRYAGAIN Multiple keys request during rehashing of slot",
        "CLUSTERDOWN The cluster is down",
        "MISCONF Redis is configured to save RDB snapshots, but it's currently "
        "unable to persist to disk.",
        "NOREPLICAS Not enough good replicas to write.",
        "MOVED 3999 127.0.0.1:6381",
        "ASK 3999 127.0.0.1:6381",
        "UNBLOCKED force unblock from blocking operation, instance state changed "
        "(master -> replica?)",
    ],
)
def test_redis_availability_replies_do_not_burn_relay_attempts(reply: str) -> None:
    # A failover's READONLY / MASTERDOWN and cluster TRYAGAIN / CLUSTERDOWN
    # replies are plain ResponseErrors: a failover must not burn every event's
    # OUTBOX_RELAY_MAX_ATTEMPTS and dead-letter valid events.
    err = BaseParser().parse_error(reply)
    assert relay.is_broker_unavailable(err)
    wrapped = RuntimeError("publish failed")
    wrapped.__cause__ = err
    assert relay.is_broker_unavailable(wrapped)


def test_a_poison_reply_still_burns_attempts() -> None:
    assert not relay.is_broker_unavailable(
        BaseParser().parse_error("ERR value is not an integer or out of range")
    )
