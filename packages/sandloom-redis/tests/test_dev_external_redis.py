"""``sandloom dev`` on a real Redis server through the real ``redis`` client.

The kernel's own tests (``test_dev_external.py`` in ``sandloom.core.tests``) run
the engine on an in-memory stand-in of the stack service, because the kernel
installs no Redis client. What only a real server and client answer is here:
the ``databases`` setting and ``DBSIZE`` past the last index (a
``ResponseError``), the clients a ``CLIENT LIST`` counts, and ``FLUSHDB``.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
import redis
from _redis_required import redis_unavailable
from sandloom_redis.stack import STACK_SERVICE, RedisServer
from sandloom_redis.testing.ownership import claim_redis


@pytest.fixture
def server() -> RedisServer:
    cache = os.environ.get("SANDLOOM_TEST_CACHE_URL", "")
    if not cache:
        redis_unavailable("SANDLOOM_TEST_CACHE_URL not set")
    netloc = urlsplit(cache).netloc
    server = RedisServer.from_url(urlunsplit(("redis", netloc, "", "", "")))
    if STACK_SERVICE.problem(server) is not None:
        redis_unavailable("the test Redis does not answer")
    return server


def _client(server: RedisServer, index: int) -> redis.Redis[bytes]:
    return redis.Redis(
        host=server.host, port=server.port, db=index, password=server.password or None
    )


def _own_empty(server: RedisServer, index: int) -> None:
    """Claim ``index`` of the shared test Redis for this run (a database another
    run marked refuses here, so its data is never flushed), then empty it: the
    test asserts which indexes hold no key, and the claim marker is a key."""
    auth = f":{quote(server.password, safe='')}@" if server.password else ""
    claim_redis(f"redis://{auth}{server.host}:{server.port}/{index}")
    client = _client(server, index)
    try:
        client.flushdb()
    finally:
        client.close()


def _last_index(server: RedisServer) -> int:
    probe = _client(server, 0)
    try:
        return int(probe.config_get("databases")["databases"]) - 1
    finally:
        probe.close()


def test_only_empty_redis_indexes_are_taken_on_a_real_server(
    server: RedisServer,
) -> None:
    last = _last_index(server)
    first, second, third = last - 2, last - 1, last
    for index in (first, second, third):
        _own_empty(server, index)
    busy = _client(server, first)
    try:
        busy.set("theirs", "1")
        assert STACK_SERVICE.empty_indexes(server, [first, second, third]) == [
            second,
            third,
        ]
        # Past the server's `databases` setting there is nothing to take.
        assert STACK_SERVICE.empty_indexes(server, [second, 10**6, third]) == [second]
    finally:
        busy.delete("theirs")
        busy.close()


def test_flush_empties_only_the_indexes_it_is_given(server: RedisServer) -> None:
    last = _last_index(server)
    mine, other = last - 4, last - 3
    for index in (mine, other):
        _own_empty(server, index)
    keep = _client(server, other)
    drop = _client(server, mine)
    try:
        keep.set("kept", "1")
        drop.set("gone", "1")
        STACK_SERVICE.flush(server, [mine])
        assert drop.dbsize() == 0
        assert keep.get("kept") == b"1"
    finally:
        keep.delete("kept")
        keep.close()
        drop.close()


def test_clients_counts_the_other_connections_of_the_given_indexes(
    server: RedisServer,
) -> None:
    index = _last_index(server) - 5
    _own_empty(server, index)
    mine = _client(server, index)
    try:
        mine.ping()  # a connection on `index`, held open
        assert STACK_SERVICE.clients(server, [index]) >= 1
        assert STACK_SERVICE.clients(server, [10**6]) == 0
    finally:
        mine.close()


def test_a_server_that_does_not_answer_is_one_line() -> None:
    down = STACK_SERVICE.server("127.0.0.1", 9)  # the discard port: nothing listens
    assert STACK_SERVICE.problem(down) == "connection refused"
