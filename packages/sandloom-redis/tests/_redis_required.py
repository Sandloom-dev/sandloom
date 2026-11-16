"""A test Redis that is missing: a skip locally, a failure in the ``redis-drivers`` lane.

The ``redis-drivers`` CI job sets ``SANDLOOM_REDIS_REQUIRED=1`` because it is the
only job that runs these tests: a Redis that is down or a URL that is not set
must turn the job red, not green with the tests skipped. Without the variable
(a laptop with no Redis) the tests skip, as before. Every package fixture that
reaches for the test Redis calls :func:`redis_unavailable`, never ``pytest.skip``
(``test_redis_required_guard.py`` holds that).
"""

from __future__ import annotations

import asyncio
from typing import NoReturn

from redis.asyncio import Redis
from redis.exceptions import RedisError

from sandloom.driverkit.testing import required_or_skip

REQUIRED_ENV = "SANDLOOM_REDIS_REQUIRED"


def redis_unavailable(reason: str) -> NoReturn:
    """Fail when the lane requires Redis, else skip, with ``reason`` (the
    driverkit guard, bound to this package's variable)."""
    required_or_skip(REQUIRED_ENV, reason)


async def require_redis(url: str) -> None:
    """Return when the Redis at ``url`` answers ``PING``; else
    :func:`redis_unavailable`. For a test that builds its own provider on the
    test Redis and has no fixture probing it."""
    client = Redis.from_url(url)
    try:
        await asyncio.wait_for(client.ping(), 5)
    except (OSError, RedisError, TimeoutError):
        redis_unavailable(f"test Redis not reachable at {url}")
    finally:
        await client.aclose()
