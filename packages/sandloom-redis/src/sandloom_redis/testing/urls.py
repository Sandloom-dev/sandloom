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
import re
from collections.abc import Mapping
from urllib.parse import SplitResult, parse_qs

from sandloom.driverkit.testing_family import (
    INFRA_VAR,
    INSTANCE_URL_OPTIONS,
    parse_infra,
)
from sandloom.driverkit.url_split import split_url

#: The URL schemes of a Redis server.
REDIS_SCHEMES = ("redis://", "rediss://")


def is_redis_url(url: str) -> bool:
    return url.startswith(REDIS_SCHEMES)


#: The database path of a test URL: ``/3``, ``/03`` or none (database 0).
_PLAIN_INDEX_PATH = re.compile(r"/?[0-9]{0,9}")


def refuse_unplain_database(url: str) -> None:
    """Refuse a Redis ``url`` that names its database in a way the harness does
    not read as redis-py does.

    redis-py reads a non-empty ``?db=N`` over the URL path, and takes the path
    with every ``/`` dropped and percent-escapes decoded (``/3/``, ``/%33`` and
    ``/1/5`` are databases 3, 3 and 15). The claim, the allocation, the worker
    mover and the flush of this harness key on the plain path index: a URL
    naming its database another way would be isolated on one index and used on
    another, and the workers would share data (or the flush would empty the
    wrong database). The index of a test URL is the whole path, ``/<digits>``
    (``redis://host:6379/3``). The message names no path, query or password.
    """
    parts = split_url(url)
    plain = _PLAIN_INDEX_PATH.fullmatch(parts.path) is not None
    if plain and "db" not in parse_qs(parts.query):
        return
    reason = (
        "a 'db' query option"
        if plain
        else "a database path that is not a plain decimal index"
    )
    raise ValueError(
        f"a Redis test URL names its database index as /<digits> in the path "
        f"(redis://host:port/3), but {_where(parts)} has {reason}: redis-py "
        "reads it another way than the per-worker isolation does, so the "
        "workers would share one database"
    )


def _where(parts: SplitResult) -> str:
    """``scheme://host:port`` of a URL, for a message (never a credential, path
    or query). An ``@`` after the authority means an unencoded ``/``, ``?`` or
    ``#`` in the password ended the netloc early: what is left as host and port
    is a piece of the user name or password, so only ``scheme://`` is named (the
    guard of :func:`sandloom.driverkit.shared_endpoints.redis_endpoint`)."""
    if "@" in parts.path + parts.query + parts.fragment:
        return f"{parts.scheme}://"
    host = parts.hostname or "<no host>"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{host}{port}"


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
        refuse_unplain_database(url)
    return found


def server_of(url: str) -> str:
    """``scheme://host:port`` of ``url``, for a message (no credential, path or
    query)."""
    return _where(split_url(url))


def instance_using(raw: str | None, url: str) -> tuple[str, str] | None:
    """``(capability, instance)`` of the first ``INFRA`` instance whose Redis URL
    option is ``url`` (``None``: no instance names it)."""
    for capability, instances in parse_infra(raw).items():
        for name, spec in instances.items():
            if any(spec[option] == url for option in instance_redis_options(spec)):
                return capability, name
    return None


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
    "instance_using",
    "is_redis_url",
    "redis_client_installed",
    "redis_urls",
    "refuse_unplain_database",
    "server_of",
]
