"""Ownership of a test Redis database index: never reset another run's.

Every Redis index a test run resets carries an OWNER marker, the key
``sandloom:test:owner`` (re-written after every harness ``FLUSHDB``), and when a
run last used it, the key ``sandloom:test:last_used`` (epoch seconds), which
``tools/checks/test_resources.py`` reads to prune the indexes no run has used
for days. An index without a marker is CLAIMED by the first run that uses it; a
marker of a DIFFERENT owner is refused before anything is reset
(``TestResourceOwnershipError``), unless ``SANDLOOM_TEST_TAKE_OWNERSHIP=1``. The
rules, the owner id and the refusal are the kernel's (``check_holder``,
``current_owner`` of :mod:`sandloom.driverkit.testing_family`): the same as for
a PostgreSQL database.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlunsplit

from sandloom.driverkit.testing_family import (
    TestResourceOwnershipError,
    already_claimed,
    check_holder,
    current_owner,
    record_claim,
)
from sandloom.driverkit.url_split import split_url
from sandloom_redis.testing.urls import is_redis_url, redis_client_installed

#: Redis key carrying the owner of a database index.
REDIS_OWNER_KEY = "sandloom:test:owner"
#: Redis key carrying when a run last claimed / flushed the index (epoch s).
REDIS_LAST_USED_KEY = "sandloom:test:last_used"

#: The text of the reply to ``SELECT n`` with ``n`` past the server's ``databases``.
OUT_OF_RANGE = "DB index is out of range"
_KIND = "Redis database"
_HINT = (
    "export SANDLOOM_TEST_CACHE_URL / SANDLOOM_TEST_BROKER_URL=redis://…/<your index>"
)


def decoded(value: Any) -> str | None:
    """A reply of the client (bytes, or text with ``decode_responses``) as text;
    ``None`` for a missing key."""
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _redact(url: str) -> str:
    try:
        parts = split_url(url)
    except ValueError:
        return "<unparseable url>"  # it may hold the password: not echoed
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def claim_redis(
    url: str, *, owner: str | None = None, environ: Mapping[str, str] | None = None
) -> None:
    """Verify (or claim) that this run owns the Redis database ``url``."""
    if not is_redis_url(url) or not redis_client_installed():
        return  # no Redis URL, or no client: this process reaches no Redis
    split_url(url)  # unreadable: a fixed message, never redis-py's (it quotes the url)
    owner = owner or current_owner(environ)
    if already_claimed("redis", url, owner):
        return

    import redis

    client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    try:
        try:
            holder = decoded(client.get(REDIS_OWNER_KEY))
        except redis.exceptions.ConnectionError:
            return  # server down: the tests that need it skip / fail
        except redis.exceptions.ResponseError as exc:
            if OUT_OF_RANGE not in str(exc):
                raise
            raise TestResourceOwnershipError(
                f"the test Redis {_redact(url)} has fewer database indexes than "
                "this run needs (a stock redis:7 has 16, and the default "
                "xdist layout puts worker N at index + 16 * (N + 1)): start it "
                "with `--databases 256` (`docker run redis:7 --databases 256`), "
                "or point the run at a Redis that has them "
                "(docs/reference/testing.md, 'Running the tests'). "
                f"The server said: {exc}"
            ) from None
        check_holder(_KIND, _redact(url), holder, owner, _HINT, environ)
        if holder != owner:
            client.set(REDIS_OWNER_KEY, owner)
        client.set(REDIS_LAST_USED_KEY, int(time.time()))
    finally:
        client.close()
    record_claim("redis", url, owner)


async def flush_owned_redis(client: Any, *, owner: str | None = None) -> None:
    """``FLUSHDB`` a test Redis database this run owns, keeping its marker.

    ``client`` is an asyncio Redis client (bytes or decoded responses).
    Raises ``TestResourceOwnershipError`` when another run marked it.
    """
    owner = owner or current_owner()
    holder = decoded(await client.get(REDIS_OWNER_KEY))
    kwargs = client.connection_pool.connection_kwargs
    where = f"redis://{kwargs.get('host', '?')}:{kwargs.get('port', '?')}/" + str(
        kwargs.get("db", 0)
    )
    check_holder(_KIND, where, holder, owner, _HINT)
    await client.flushdb()
    await client.set(REDIS_OWNER_KEY, owner)
    await client.set(REDIS_LAST_USED_KEY, int(time.time()))


__all__ = [
    "REDIS_LAST_USED_KEY",
    "REDIS_OWNER_KEY",
    "claim_redis",
    "decoded",
    "flush_owned_redis",
]
