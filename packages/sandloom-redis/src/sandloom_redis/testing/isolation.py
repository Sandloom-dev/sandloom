"""Redis parallel-test isolation: one set of Redis database indexes per xdist worker.

The Redis half of the worker isolation of ``sandloom.core.testing.workers``.
The harness does not move a URL itself: a driver states what one worker's
instance uses through ``InfraProvider.test_isolation(options, worker_id)`` (the
Redis drivers move the database index of their URL options), and
:func:`rewrite_redis` calls that hook of every configured instance:

* every instance ``INFRA`` declares with a Redis ``url``, ``broker`` or
  ``backend`` option, any capability (the cache, lease, atomic store, broadcast
  and event bus ``url`` and the task queue's ``broker``, which the ``celery``
  driver's hook moves together with its ``backend``), the rewritten ``INFRA``
  going back to the environment.

Nothing here rewrites a URL itself.

The layout is computed here and handed to the drivers as a URL mover
(:func:`sandloom.driverkit.testing_isolation.set_url_mover`) before any hook
runs, so a hook and the harness agree and a driver reads no environment. Two
layouts, chosen by ``SANDLOOM_TEST_REDIS_DB_STRIDE``:

* a number (default 16): the index moves by ``stride * (N + 1)`` for worker
  ``gw<N>``;
* ``auto``: the xdist controller claims free indexes for every worker before
  they start and hands the workers the mapping in
  ``SANDLOOM_TEST_REDIS_WORKER_DBS`` (``<base>=<i0>,<i1>,...;...``); use it on a
  Redis shared with other runs, whose markers break any fixed layout.

Importing this module imports no Redis client. It is imported only by a process
that has Redis to isolate (:mod:`sandloom_redis.testing.urls` decides).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from sandloom.driverkit import testing_isolation as _isolation
from sandloom.driverkit.testing_family import (
    INFRA_VAR,
    default_driver,
    isolated_instance_options,
    parse_infra,
)
from sandloom_redis.testing.urls import (
    REDIS_SCHEMES,
    instance_redis_options,
    redis_client_installed,
)

#: Database indexes between two workers' instances, by default.
DEFAULT_STRIDE = 16

REDIS_STRIDE_ENV = "SANDLOOM_TEST_REDIS_DB_STRIDE"
#: ``SANDLOOM_TEST_REDIS_DB_STRIDE`` value selecting allocated indexes.
REDIS_STRIDE_AUTO = "auto"
#: The controller's allocation for ``auto``: ``<base>=<i0>,<i1>,...;...``.
REDIS_MAP_ENV = "SANDLOOM_TEST_REDIS_WORKER_DBS"
#: The driver of the default cache instance when nothing selects one: this
#: package's own.
DEFAULT_CACHE_DRIVER = "redis"


@dataclass(frozen=True, slots=True)
class WorkerLayout:
    """Which database index each worker's URL moves to.

    ``mapping`` is the allocation of the shared-Redis ``auto`` mode,
    ``{base index: (index of gw0, index of gw1, ...)}``; without it the index
    moves by ``stride * (N + 1)``.
    """

    stride: int = DEFAULT_STRIDE
    mapping: Mapping[int, Sequence[int]] | None = None


def _plain_index(url: str) -> tuple[SplitResult, int] | None:
    parts = urlsplit(url)
    raw = parts.path.lstrip("/")
    try:
        return parts, (int(raw) if raw else 0)
    except ValueError:
        return None  # not a plain index (unix socket path, ...)


def worker_redis_url(url: str, number: int, stride: int) -> str:
    """``url`` with its database index offset by ``stride * (number + 1)``."""
    found = _plain_index(url)
    if found is None:
        return url
    parts, index = found
    return urlunsplit(parts._replace(path=f"/{index + stride * (number + 1)}"))


def mapped_redis_url(
    url: str, number: int, mapping: Mapping[int, Sequence[int]]
) -> str:
    """``url`` moved to worker ``number``'s allocated index (``auto``)."""
    found = _plain_index(url)
    if found is None:
        return url
    parts, index = found
    indexes = mapping.get(index)
    if not indexes:
        known = ";".join(
            f"{b}={','.join(map(str, i))}" for b, i in sorted(mapping.items())
        )
        raise RuntimeError(
            f"{REDIS_STRIDE_ENV}=auto: no Redis index allocated for base "
            f"index {index}, worker gw{number} "
            f"({REDIS_MAP_ENV}={known!r})"
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


def moved_redis_url(url: str, number: int, layout: WorkerLayout) -> str:
    """``url`` as worker ``number`` uses it under ``layout``; a URL that is not a
    Redis one comes back unchanged."""
    if not url.startswith(REDIS_SCHEMES):
        return url
    if layout.mapping is not None:
        return mapped_redis_url(url, number, layout.mapping)
    return worker_redis_url(url, number, layout.stride)


def use_layout(layout: WorkerLayout | None = None) -> None:
    """Make ``layout`` (the default one when omitted) this process's URL mover."""
    chosen = layout or WorkerLayout()
    _isolation.set_url_mover(lambda url, number: moved_redis_url(url, number, chosen))


def redis_stride(environ: Mapping[str, str]) -> int:
    raw = (environ.get(REDIS_STRIDE_ENV) or "").strip()
    try:
        stride = int(raw) if raw else DEFAULT_STRIDE
    except ValueError:
        return DEFAULT_STRIDE
    return stride if stride > 0 else DEFAULT_STRIDE


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


def layout_from_environ(environ: Mapping[str, str]) -> WorkerLayout:
    """The layout ``environ`` selects: the controller's allocation under
    ``auto``, else the fixed stride."""
    if not redis_auto(environ):
        return WorkerLayout(stride=redis_stride(environ))
    raw = environ.get(REDIS_MAP_ENV) or ""
    if not raw:
        raise RuntimeError(
            f"{REDIS_STRIDE_ENV}=auto but {REDIS_MAP_ENV} is not set: the "
            "xdist controller allocates the worker indexes (the "
            "sandloom.core.testing plugin's pytest_sessionstart)"
        )
    return WorkerLayout(mapping=parse_redis_map(raw))


def _driver_of(
    infra: Mapping[str, Mapping[str, Mapping[str, Any]]],
    capability: str,
    name: str,
    *,
    chosen: str | None = None,
    fallback: str | None = None,
) -> str | None:
    """The driver of instance ``capability/name``: ``INFRA``'s, else the one
    the settings choose (``chosen``), else the one the composition root selects,
    else ``fallback``."""
    declared = infra.get(capability, {}).get(name, {}).get("driver")
    if declared:
        return str(declared)
    return chosen or default_driver(capability, name) or fallback


def _chosen_driver(
    values: Mapping[str, str | None], capability: str, name: str
) -> str | None:
    """The driver of a ``default`` instance the settings name outside ``INFRA``:
    the bus ``EVENT_BUS_BACKEND`` names (unset leaves it to the composition)."""
    if (capability, name) == ("eventbus", "default"):
        backend = (values.get("EVENT_BUS_BACKEND") or "").strip().lower()
        # The retired spelling ``auto`` still means unset until a release refuses it.
        return None if backend in ("", "auto") else backend
    return None


def rewrite_redis(
    values: Mapping[str, str | None],
    environ: Mapping[str, str],
    worker_id: str,
    number: int,
) -> dict[str, str]:
    """The replacements of worker ``worker_id``'s Redis settings in ``values``.

    Empty without the redis client or a Redis URL (no layout is read then: a
    run on memory drivers needs no ``auto`` allocation). Each configured
    instance is resolved once and its hook called once on the resolved
    options, so a URL moves exactly once.
    """
    if not redis_client_installed():
        return {}
    infra_raw = values.get(INFRA_VAR)
    infra = parse_infra(infra_raw)
    named = [
        (capability, name, spec)
        for capability, instances in infra.items()
        for name, spec in instances.items()
        if instance_redis_options(spec)
    ]
    if not named:
        return {}
    use_layout(layout_from_environ(environ))
    rewritten: dict[str, Any] = json.loads(infra_raw or "{}")
    for capability, name, spec in named:
        driver = _driver_of(
            infra,
            capability,
            name,
            chosen=_chosen_driver(values, capability, name),
            fallback=DEFAULT_CACHE_DRIVER
            if (capability, name) == ("cache", "default")
            else None,
        )
        if not driver:
            continue  # the registry refuses it when the settings load
        options = {k: v for k, v in spec.items() if k != "driver"}
        moved = isolated_instance_options(capability, name, driver, options, worker_id)
        rewritten[capability][name] = {**spec, **moved}
    return {INFRA_VAR: json.dumps(rewritten)}


__all__ = [
    "DEFAULT_CACHE_DRIVER",
    "DEFAULT_STRIDE",
    "REDIS_MAP_ENV",
    "REDIS_STRIDE_AUTO",
    "REDIS_STRIDE_ENV",
    "WorkerLayout",
    "format_redis_map",
    "layout_from_environ",
    "mapped_redis_url",
    "moved_redis_url",
    "parse_redis_map",
    "redis_auto",
    "redis_stride",
    "rewrite_redis",
    "use_layout",
    "worker_redis_url",
]
