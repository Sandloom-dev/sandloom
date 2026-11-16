"""Redis parallel-test isolation: one set of Redis database indexes per xdist worker.

The Redis half of the worker isolation of ``sandloom.core.testing.workers``.
The harness does not move a URL itself: a driver states what one worker's
instance uses through ``InfraProvider.test_isolation(options, worker_id)`` (the
Redis drivers move the database index of their URL options), and
:func:`rewrite_redis` calls that hook of every configured instance:

* the default cache (``CACHE_URL``, while a flat one is set);
* every instance ``INFRA`` declares with a Redis ``url``, ``broker`` or
  ``backend`` option, any capability (the cache, lease, atomic store, broadcast
  and event bus ``url`` and the task queue's ``broker``, which the ``celery``
  driver's hook moves together with its ``backend``), the rewritten ``INFRA``
  going back to the environment: with or without the flat settings, which
  ``.env.test`` no longer needs;
* the default task queue's flat ``BROKER_URL``, while one is set (the kernel
  fills it into the instance's ``broker`` option).

Nothing here rewrites a URL itself.

The layout is computed here and handed to the drivers
(:func:`sandloom.driverkit.testing_isolation.set_worker_layout`) before any hook
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
from collections.abc import Mapping
from typing import Any

from sandloom.driverkit import testing_isolation as _isolation
from sandloom.driverkit.testing_family import (
    INFRA_VAR,
    default_driver,
    isolated_instance_options,
    parse_infra,
)
from sandloom_redis.testing.urls import (
    instance_redis_options,
    is_redis_url,
    redis_client_installed,
)

REDIS_STRIDE_ENV = "SANDLOOM_TEST_REDIS_DB_STRIDE"
#: ``SANDLOOM_TEST_REDIS_DB_STRIDE`` value selecting allocated indexes.
REDIS_STRIDE_AUTO = "auto"
#: The controller's allocation for ``auto``: ``<base>=<i0>,<i1>,...;...``.
REDIS_MAP_ENV = "SANDLOOM_TEST_REDIS_WORKER_DBS"
#: The driver of the default cache instance when nothing selects one: this
#: package's own.
DEFAULT_CACHE_DRIVER = "redis"


def redis_stride(environ: Mapping[str, str]) -> int:
    raw = (environ.get(REDIS_STRIDE_ENV) or "").strip()
    try:
        stride = int(raw) if raw else _isolation.DEFAULT_STRIDE
    except ValueError:
        return _isolation.DEFAULT_STRIDE
    return stride if stride > 0 else _isolation.DEFAULT_STRIDE


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


def layout_from_environ(environ: Mapping[str, str]) -> _isolation.WorkerLayout:
    """The layout ``environ`` selects: the controller's allocation under
    ``auto``, else the fixed stride."""
    if not redis_auto(environ):
        return _isolation.WorkerLayout(stride=redis_stride(environ))
    raw = environ.get(REDIS_MAP_ENV) or ""
    if not raw:
        raise RuntimeError(
            f"{REDIS_STRIDE_ENV}=auto but {REDIS_MAP_ENV} is not set: the "
            "xdist controller allocates the worker indexes (the "
            "sandloom.core.testing plugin's pytest_sessionstart)"
        )
    return _isolation.WorkerLayout(mapping=parse_redis_map(raw))


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
    cache = values.get("CACHE_URL") or ""
    broker = values.get("BROKER_URL") or ""
    infra_raw = values.get(INFRA_VAR)
    infra = parse_infra(infra_raw)
    named = [
        (capability, name, spec)
        for capability, instances in infra.items()
        for name, spec in instances.items()
        if instance_redis_options(spec)
    ]
    if not (is_redis_url(cache) or is_redis_url(broker) or named):
        return {}
    layout = layout_from_environ(environ)
    _isolation.set_worker_layout(layout)
    out: dict[str, str] = {}
    if is_redis_url(cache):
        cache_driver = _driver_of(
            infra, "cache", "default", fallback=DEFAULT_CACHE_DRIVER
        )
        moved = isolated_instance_options(
            "cache", "default", str(cache_driver), {"url": cache}, worker_id
        )
        out["CACHE_URL"] = str(moved.get("url", cache))
    if is_redis_url(broker):
        # The kernel fills the default task queue's ``broker`` from BROKER_URL:
        # the driver of that instance says where its worker uses it. Without a
        # task queue driver the URL is still a Redis URL a test may point a
        # queue at, so it moves like CACHE_URL (the Redis driver's ``url``
        # option): workers never share one broker index.
        broker_driver = _driver_of(infra, "taskqueue", "default")
        if broker_driver:
            moved = isolated_instance_options(
                "taskqueue", "default", broker_driver, {"broker": broker}, worker_id
            )
            out["BROKER_URL"] = str(moved.get("broker", broker))
        else:
            moved = isolated_instance_options(
                "cache", "default", DEFAULT_CACHE_DRIVER, {"url": broker}, worker_id
            )
            out["BROKER_URL"] = str(moved.get("url", broker))
    if named:
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
            moved = isolated_instance_options(
                capability, name, driver, options, worker_id
            )
            rewritten[capability][name] = {**spec, **moved}
        out[INFRA_VAR] = json.dumps(rewritten)
    return out


__all__ = [
    "DEFAULT_CACHE_DRIVER",
    "REDIS_MAP_ENV",
    "REDIS_STRIDE_AUTO",
    "REDIS_STRIDE_ENV",
    "format_redis_map",
    "layout_from_environ",
    "parse_redis_map",
    "redis_auto",
    "redis_stride",
    "rewrite_redis",
]
