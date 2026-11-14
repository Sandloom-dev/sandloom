"""Which Redis URLs a test run has, and whether this process can reach Redis.

The questions the harness asks before it touches Redis at all (rewrite a
worker's URLs, claim, allocate, reset). Standard library only, and the answer
for a process without the redis client is "none" without importing anything
Redis-shaped: an application on memory drivers runs its tests with no client
installed. The layout of the isolation itself is
:mod:`sandloom.core.testing.redis_isolation`, imported only when this module
finds a Redis to isolate.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Mapping
from typing import Any

#: The URL schemes of a Redis server.
REDIS_SCHEMES = ("redis://", "rediss://")
#: The flat settings naming a Redis (the default cache, the task broker).
FLAT_VARS = ("CACHE_URL", "BROKER_URL")
#: The setting declaring named infrastructure instances (JSON).
INFRA_VAR = "INFRA"


def is_redis_url(url: str) -> bool:
    return url.startswith(REDIS_SCHEMES)


def redis_client_installed() -> bool:
    """Whether the redis client can be imported (it is an optional extra).

    Without it this process can reach no Redis (the Redis drivers and kombu's
    transport import it), so the harness has no Redis resource to claim,
    allocate or reset. Looks for the client; does not import it.
    """
    try:
        return importlib.util.find_spec("redis") is not None
    except (ImportError, ValueError):
        return False


def redis_isolation() -> Any:
    """The Redis half of the harness (:mod:`sandloom.core.testing.redis_isolation`),
    imported on first use: only a process that can reach a Redis calls this."""
    return importlib.import_module("sandloom.core.testing.redis_isolation")


def parse_infra(raw: str | None) -> dict[str, dict[str, dict[str, Any]]]:
    """The ``INFRA`` setting as ``{capability: {instance: spec}}``; ``{}`` for
    a missing or malformed one (settings refuse it when they load)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(cap): {
            str(name): spec
            for name, spec in instances.items()
            if isinstance(spec, dict)
        }
        for cap, instances in parsed.items()
        if isinstance(instances, dict)
    }


def infra_redis_urls(raw: str | None) -> list[str]:
    """The Redis URLs of the named instances ``INFRA`` declares with a ``url``."""
    return [
        spec["url"]
        for instances in parse_infra(raw).values()
        for spec in instances.values()
        if isinstance(spec.get("url"), str) and is_redis_url(spec["url"])
    ]


def redis_urls(values: Mapping[str, str | None]) -> list[str]:
    """Every Redis URL of the run in ``values``, each once; none without the
    client."""
    if not redis_client_installed():
        return []
    urls = [url for var in FLAT_VARS if is_redis_url(url := values.get(var) or "")]
    urls += infra_redis_urls(values.get(INFRA_VAR))
    return list(dict.fromkeys(urls))


__all__ = [
    "FLAT_VARS",
    "INFRA_VAR",
    "REDIS_SCHEMES",
    "infra_redis_urls",
    "is_redis_url",
    "parse_infra",
    "redis_client_installed",
    "redis_isolation",
    "redis_urls",
]
