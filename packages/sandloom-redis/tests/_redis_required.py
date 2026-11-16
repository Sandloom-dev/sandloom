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
from sandloom.sdk.testing import resolved_cache_url

REQUIRED_ENV = "SANDLOOM_REDIS_REQUIRED"


def redis_unavailable(reason: str) -> NoReturn:
    """Fail when the lane requires Redis, else skip, with ``reason`` (the
    driverkit guard, bound to this package's variable)."""
    required_or_skip(REQUIRED_ENV, reason)


def cache_url_or_unavailable() -> str:
    """The test run's ``default`` cache url (``resolved_cache_url``); when none
    is configured, :func:`redis_unavailable` (a failure in the lane that
    requires Redis, a skip elsewhere)."""
    try:
        return resolved_cache_url()
    except RuntimeError:
        redis_unavailable(
            "no test cache url: INFRA's cache.default.url or SANDLOOM_TEST_CACHE_URL"
        )


async def require_redis(url: str) -> None:
    """Return when the Redis at ``url`` answers ``PING``; else
    :func:`redis_unavailable`. For a test that builds its own provider on the
    test Redis and has no fixture probing it. The reason never names the URL
    (it can carry credentials); an unset URL is unavailable too."""
    if not url:
        redis_unavailable("test Redis URL not set")
    client = Redis.from_url(url)
    try:
        await asyncio.wait_for(client.ping(), 5)
    except (OSError, RedisError, TimeoutError):
        redis_unavailable("test Redis not reachable")
    finally:
        await client.aclose()
