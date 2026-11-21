"""Which Redis URLs a test run has, and whether this process can reach Redis.

The questions the harness family asks before it touches Redis at all (rewrite a
worker's URLs, claim, allocate, reset). Standard library only: a process
without the redis client answers "none" without importing anything Redis-shaped
(the package is installed beside an application that runs its tests on memory
drivers too). The layout of the isolation itself is
:mod:`sandloom_redis.testing.isolation`, imported only when this module finds a
Redis to isolate.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from urllib.parse import parse_qs

from sandloom.driverkit.testing_family import (
    INFRA_VAR,
    INSTANCE_URL_OPTIONS,
    parse_infra,
)
from sandloom.driverkit.url_split import redact, split_url

#: The URL schemes of a Redis server.
REDIS_SCHEMES = ("redis://", "rediss://")


def is_redis_url(url: str) -> bool:
    return url.startswith(REDIS_SCHEMES)


def refuse_db_query(url: str) -> None:
    """Refuse a Redis ``url`` carrying a non-empty ``db`` query option.

    redis-py reads ``?db=N`` over the URL path, while the claim, the allocation,
    the worker mover and the flush of this harness key on the path index: a URL
    naming both would be isolated on one index and used on another, and the
    workers would share data (or the flush would empty the wrong database).
    The index of a test URL goes in the path (``redis://host:6379/3``).
    """
    if "db" in parse_qs(split_url(url).query):
        raise ValueError(
            f"a Redis test URL names its database index in the path, not in a "
            f"'db' query option: {redact(url)} (use redis://host:port/<index>); "
            "redis-py reads ?db=N over the path, so the per-worker isolation "
            "would leave every worker on the same database"
        )


def redis_client_installed() -> bool:
    """Whether the redis client can be imported.

    Without it this process can reach no Redis (the Redis drivers and kombu's
    transport import it), so the harness has no Redis resource to claim,
    allocate or reset. Looks for the client; does not import it.
    """
    try:
        return importlib.util.find_spec("redis") is not None
    except (ImportError, ValueError):
        return False


def instance_redis_options(spec: Mapping[str, object]) -> list[str]:
    """The URL options of one ``INFRA`` instance ``spec`` that hold a Redis URL."""
    return [
        option
        for option in INSTANCE_URL_OPTIONS
        if isinstance(value := spec.get(option), str) and is_redis_url(value)
    ]


def infra_redis_urls(raw: str | None) -> list[str]:
    """The Redis URLs of the instances ``INFRA`` declares (``url``, and a task
    queue's ``broker`` / ``backend``)."""
    found = [
        str(spec[option])
        for instances in parse_infra(raw).values()
        for spec in instances.values()
        for option in instance_redis_options(spec)
    ]
    for url in found:
        refuse_db_query(url)
    return found


def redis_urls(values: Mapping[str, str | None]) -> list[str]:
    """Every Redis URL of the run in ``values``, each once; none without the
    client."""
    if not redis_client_installed():
        return []
    return list(dict.fromkeys(infra_redis_urls(values.get(INFRA_VAR))))


__all__ = [
    "REDIS_SCHEMES",
    "infra_redis_urls",
    "instance_redis_options",
    "is_redis_url",
    "redis_client_installed",
    "refuse_db_query",
    "redis_urls",
]
