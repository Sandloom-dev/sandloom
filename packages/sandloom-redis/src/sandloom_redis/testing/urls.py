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

from sandloom.driverkit.testing_family import (
    INFRA_VAR,
    INSTANCE_URL_OPTIONS,
    parse_infra,
)

#: The URL schemes of a Redis server.
REDIS_SCHEMES = ("redis://", "rediss://")


def is_redis_url(url: str) -> bool:
    return url.startswith(REDIS_SCHEMES)


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
    return [
        str(spec[option])
        for instances in parse_infra(raw).values()
        for spec in instances.values()
        for option in instance_redis_options(spec)
    ]


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
    "redis_urls",
]
