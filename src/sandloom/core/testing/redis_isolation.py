"""Redis parallel-test isolation: one set of Redis database indexes per xdist worker.

This is the Redis half of the worker isolation of :mod:`sandloom.core.testing.workers`,
kept in one module so it can move into a ``sandloom-redis`` pytest plugin with
the Redis drivers. A driver states what one worker's instance uses through
``InfraProvider.test_isolation(options, worker_id)`` (the Redis drivers move the
database index of their URL options); the harness rewrites ``CACHE_URL`` /
``BROKER_URL`` the same way, before any setting is read.

The harness cannot ask the driver class: importing a driver imports
``sandloom.core.config.settings``, which snapshots the environment, and the
worker's URLs must be in the environment first. So this module and the drivers'
hook compute the same answer for the default layout
(``test_harness_isolation.py`` holds them equal).

Two layouts, chosen by ``SANDLOOM_TEST_REDIS_DB_STRIDE``:

* a number (default 16): the index moves by ``stride * (N + 1)`` for worker
  ``gw<N>``;
* ``auto``: the xdist controller claims free indexes for every worker before
  they start and hands the workers the mapping in
  ``SANDLOOM_TEST_REDIS_WORKER_DBS`` (``<base>=<i0>,<i1>,...;...``); use it on a
  Redis shared with other runs, whose markers break any fixed layout.

Imports nothing but the standard library: no redis client, no framework module.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

#: The URL schemes this module isolates.
REDIS_SCHEMES = ("redis://", "rediss://")
REDIS_STRIDE_ENV = "SANDLOOM_TEST_REDIS_DB_STRIDE"
DEFAULT_REDIS_STRIDE = 16
#: ``SANDLOOM_TEST_REDIS_DB_STRIDE`` value selecting allocated indexes.
REDIS_STRIDE_AUTO = "auto"
#: The controller's allocation for ``auto``: ``<base>=<i0>,<i1>,...;...``.
REDIS_MAP_ENV = "SANDLOOM_TEST_REDIS_WORKER_DBS"


def is_redis_url(url: str) -> bool:
    return url.startswith(REDIS_SCHEMES)


def redis_client_installed() -> bool:
    """Whether the redis client can be imported (it is an optional extra).

    Without it this process can reach no Redis (the Redis drivers and kombu's
    transport import it), so the harness has no Redis resource to claim,
    allocate or reset: an application running on memory drivers needs no
    client to run its tests. Looks for the client; does not import it.
    """
    try:
        return importlib.util.find_spec("redis") is not None
    except (ImportError, ValueError):
        return False


def redis_stride(environ: Mapping[str, str]) -> int:
    raw = (environ.get(REDIS_STRIDE_ENV) or "").strip()
    try:
        stride = int(raw) if raw else DEFAULT_REDIS_STRIDE
    except ValueError:
        return DEFAULT_REDIS_STRIDE
    return stride if stride > 0 else DEFAULT_REDIS_STRIDE


def redis_auto(environ: Mapping[str, str] | None = None) -> bool:
    """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``: workers get allocated indexes."""
    env = os.environ if environ is None else environ
    return (env.get(REDIS_STRIDE_ENV) or "").strip().lower() == REDIS_STRIDE_AUTO


def format_redis_map(mapping: Mapping[int, list[int]]) -> str:
    return ";".join(
        f"{base}=" + ",".join(str(i) for i in indexes)
        for base, indexes in sorted(mapping.items())
    )


def parse_redis_map(raw: str) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for part in filter(None, (p.strip() for p in raw.split(";"))):
        base, _, indexes = part.partition("=")
        out[int(base)] = [int(i) for i in indexes.split(",") if i.strip()]
    return out


def mapped_redis_url(url: str, number: int, mapping: Mapping[int, list[int]]) -> str:
    """``url`` moved to worker ``number``'s allocated index (``auto``)."""
    parts = urlsplit(url)
    raw = parts.path.lstrip("/")
    try:
        index = int(raw) if raw else 0
    except ValueError:
        return url  # not a plain index (unix socket path, ...): leave it
    indexes = mapping.get(index)
    if not indexes:
        raise RuntimeError(
            f"{REDIS_STRIDE_ENV}=auto: no Redis index allocated for base index "
            f"{index}, worker gw{number} "
            f"({REDIS_MAP_ENV}={format_redis_map(mapping)!r})"
        )
    if number >= len(indexes):
        # A replacement for crashed workers beyond the allocated spares:
        # deterministic (never a conftest import failure cascade), but it
        # shares an index with another worker: say so on stderr.
        slot = number % len(indexes)
        print(
            f"sandloom testing: worker gw{number} has no Redis index of its own for "
            f"base {index}; sharing gw{slot}'s index {indexes[slot]} (a crashed "
            "worker was replaced more often than the spares allow)",
            file=sys.stderr,
        )
        return urlunsplit(parts._replace(path=f"/{indexes[slot]}"))
    return urlunsplit(parts._replace(path=f"/{indexes[number]}"))


def worker_redis_url(url: str, number: int, stride: int) -> str:
    """``url`` with its database index offset by ``stride * (number + 1)``."""
    parts = urlsplit(url)
    raw = parts.path.lstrip("/")
    try:
        index = int(raw) if raw else 0
    except ValueError:
        return url  # not a plain index (unix socket path, ...): leave it
    return urlunsplit(parts._replace(path=f"/{index + stride * (number + 1)}"))


def redis_url_mapping(environ: Mapping[str, str]) -> dict[int, list[int]] | None:
    """The controller's allocation under ``auto`` (``None``: fixed layout)."""
    if not redis_auto(environ):
        return None
    raw = environ.get(REDIS_MAP_ENV) or ""
    if not raw:
        raise RuntimeError(
            f"{REDIS_STRIDE_ENV}=auto but {REDIS_MAP_ENV} is not set: the "
            "xdist controller allocates the worker indexes (the "
            "sandloom.core.testing plugin's pytest_sessionstart)"
        )
    return parse_redis_map(raw)


def isolated_redis_url(
    url: str,
    number: int,
    environ: Mapping[str, str],
    mapping: Mapping[int, list[int]] | None = None,
) -> str:
    """``url`` as worker ``number`` uses it: the allocated index under ``auto``
    (``mapping``, read from ``environ`` when not given), else the fixed stride."""
    if mapping is None:
        mapping = redis_url_mapping(environ)
    if mapping is not None:
        return mapped_redis_url(url, number, mapping)
    return worker_redis_url(url, number, redis_stride(environ))


__all__ = [
    "DEFAULT_REDIS_STRIDE",
    "REDIS_MAP_ENV",
    "REDIS_SCHEMES",
    "REDIS_STRIDE_AUTO",
    "REDIS_STRIDE_ENV",
    "format_redis_map",
    "is_redis_url",
    "isolated_redis_url",
    "mapped_redis_url",
    "parse_redis_map",
    "redis_auto",
    "redis_client_installed",
    "redis_stride",
    "redis_url_mapping",
    "worker_redis_url",
]
