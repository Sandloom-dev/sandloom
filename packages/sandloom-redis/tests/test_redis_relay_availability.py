"""The outbox relay's verdict on the replies the real ``redis`` client parses.

The kernel's relay names no client library's error: it trusts the ``InfraError``
a driver mapped. Here the replies go through the client's own parser and then
through the package's mapping (``sandloom_redis._errors``), so a class the client
renames or a reply it maps to another error fails the verdict, on both sides:
an outage never burns an attempt, a poison reply still does.
"""

from __future__ import annotations

import pytest
from redis import exceptions as redis_exceptions
from redis._parsers.base import BaseParser

from sandloom.core.tasks import relay
from sandloom_redis._errors import map_redis_error

_OUTAGE_REPLIES = [
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
]


def _mapped(raw: BaseException) -> BaseException:
    """What a driver raises for ``raw``: the mapped error, chained as the Streams
    transport and the pub/sub broadcast chain it (``raise ... from raw``)."""
    mapped = map_redis_error(raw, capability="eventbus", instance=None, what="publish")
    mapped.__cause__ = raw
    return mapped


@pytest.mark.parametrize("reply", _OUTAGE_REPLIES)
def test_redis_availability_replies_do_not_burn_relay_attempts(reply: str) -> None:
    # A failover's READONLY / MASTERDOWN and cluster TRYAGAIN / CLUSTERDOWN
    # replies are plain ResponseErrors: a failover must not burn every event's
    # OUTBOX_RELAY_MAX_ATTEMPTS and dead-letter valid events.
    err = _mapped(BaseParser().parse_error(reply))
    assert relay.is_broker_unavailable(err)
    wrapped = RuntimeError("publish failed")
    wrapped.__cause__ = err
    assert relay.is_broker_unavailable(wrapped)


@pytest.mark.parametrize(
    "raw",
    [
        redis_exceptions.ConnectionError("Connection refused"),
        redis_exceptions.TimeoutError("Timeout reading from socket"),
        redis_exceptions.BusyLoadingError("loading"),
    ],
    ids=["connection", "timeout", "loading"],
)
def test_a_mapped_connection_fault_is_an_outage(raw: BaseException) -> None:
    assert relay.is_broker_unavailable(_mapped(raw))


def test_a_poison_reply_still_burns_attempts() -> None:
    err = _mapped(
        BaseParser().parse_error("ERR value is not an integer or out of range")
    )
    assert not relay.is_broker_unavailable(err)
    wrapped = RuntimeError("publish failed")
    wrapped.__cause__ = err
    assert not relay.is_broker_unavailable(wrapped)


def test_the_kernel_does_not_read_a_raw_client_error() -> None:
    """The kernel names no Redis class or reply word: an error nobody mapped is
    not an outage whatever it says (a driver that leaves one raw fails its own
    test, here and in the kit)."""
    assert not relay.is_broker_unavailable(
        BaseParser().parse_error("LOADING Redis is loading the dataset in memory")
    )
    assert not relay.is_broker_unavailable(
        BaseParser().parse_error("MOVED 3999 127.0.0.1:6381")
    )


def test_a_cluster_redirect_keeps_its_hint_in_the_outage_reason() -> None:
    err = _mapped(BaseParser().parse_error("MOVED 3999 127.0.0.1:6381"))
    reason = relay.outage_reason(err)
    assert reason is not None
    assert reason.startswith("Unavailable (cluster redirect")
    assert "127.0.0.1" not in reason
