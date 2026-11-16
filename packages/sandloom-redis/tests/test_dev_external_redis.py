"""``sandloom dev`` on a real Redis server through the real ``redis`` client.

The kernel's own tests (``test_dev_external.py``) run the same logic on a
stand-in client, because the kernel installs no Redis client. What only a real
server and client answer is here: the ``databases`` setting and ``DBSIZE`` past
the last index (a ``ResponseError``).
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from _redis_required import redis_unavailable
from sandloom_redis.testing.ownership import claim_redis

from sandloom.core.cli.dev import services


@pytest.fixture
def server() -> services.RedisServer:
    cache = os.environ.get("SANDLOOM_TEST_CACHE_URL", "")
    if not cache:
        redis_unavailable("SANDLOOM_TEST_CACHE_URL not set")
    netloc = urlsplit(cache).netloc
    server = services.RedisServer.from_url(urlunsplit(("redis", netloc, "", "", "")))
    if services.redis_ready(server.host, server.port, server.password) is not None:
        redis_unavailable("the test Redis does not answer")
    return server


def _own_empty(server: services.RedisServer, index: int) -> None:
    """Claim ``index`` of the shared test Redis for this run (a database another
    run marked refuses here, so its data is never flushed), then empty it: the
    test asserts which indexes hold no key, and the claim marker is a key."""
    auth = f":{quote(server.password, safe='')}@" if server.password else ""
    claim_redis(f"redis://{auth}{server.host}:{server.port}/{index}")
    client = services._redis(server.host, server.port, index, server.password)
    try:
        client.flushdb()
    finally:
        client.close()


def test_only_empty_redis_indexes_are_taken_on_a_real_server(
    server: services.RedisServer,
) -> None:
    probe = services._redis(server.host, server.port, 0, server.password)
    try:
        last = int(probe.config_get("databases")["databases"]) - 1
    finally:
        probe.close()
    first, second, third = last - 2, last - 1, last
    for index in (first, second, third):
        _own_empty(server, index)
    busy = services._redis(server.host, server.port, first, server.password)
    try:
        busy.set("theirs", "1")
        assert services.empty_indexes(server, [first, second, third]) == [
            second,
            third,
        ]
        # Past the server's `databases` setting there is nothing to take.
        assert services.empty_indexes(server, [second, 10**6, third]) == [second]
    finally:
        busy.delete("theirs")
        busy.close()
