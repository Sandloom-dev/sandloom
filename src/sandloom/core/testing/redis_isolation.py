"""Redis parallel-test isolation: one set of Redis database indexes per xdist worker.

The Redis half of the worker isolation of :mod:`sandloom.core.testing.workers`,
in one module so it can move into a ``sandloom-redis`` pytest plugin with the
Redis drivers. The harness does not move a URL itself: a driver states what one
worker's instance uses through ``InfraProvider.test_isolation(options,
worker_id)`` (the Redis drivers move the database index of their URL options),
and :func:`rewrite_redis` calls that hook of every configured instance:

* the default cache (``CACHE_URL``);
* every named instance ``INFRA`` declares with its own ``url``, any capability,
  the rewritten ``INFRA`` going back to the environment.

One exception, until ``idk-worker-port-kit`` gives the Celery task queue its own
hook: ``BROKER_URL`` has no driver to ask, so :func:`rewrite_redis` moves it
itself, by the same layout. Nothing else rewrites a URL here.

The layout is computed here and handed to the drivers
(:func:`sandloom.core.infra.worker_isolation.set_worker_layout`) before any hook runs,
so a hook and the harness agree and a driver reads no environment. Two layouts,
chosen by ``SANDLOOM_TEST_REDIS_DB_STRIDE``:

* a number (default 16): the index moves by ``stride * (N + 1)`` for worker
  ``gw<N>``;
* ``auto``: the xdist controller claims free indexes for every worker before
  they start and hands the workers the mapping in
  ``SANDLOOM_TEST_REDIS_WORKER_DBS`` (``<base>=<i0>,<i1>,...;...``); use it on a
  Redis shared with other runs, whose markers break any fixed layout.

Importing this module imports no Redis client. It is imported only by a process
that has Redis to isolate (:mod:`sandloom.core.testing.redis_urls` decides).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import sandloom.core.infra.worker_isolation as _isolation
from sandloom.core.drivers.bootstrap import BUILTIN_DRIVER_ROWS
from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.registry import InfraRegistry, load_selected_plugins
from sandloom.core.testing.redis_urls import (
    INFRA_VAR,
    is_redis_url,
    parse_infra,
    redis_client_installed,
)

if TYPE_CHECKING:
    from sandloom.core.infra.ports._base import InfraProvider

REDIS_STRIDE_ENV = "SANDLOOM_TEST_REDIS_DB_STRIDE"
#: ``SANDLOOM_TEST_REDIS_DB_STRIDE`` value selecting allocated indexes.
REDIS_STRIDE_AUTO = "auto"
#: The controller's allocation for ``auto``: ``<base>=<i0>,<i1>,...;...``.
REDIS_MAP_ENV = "SANDLOOM_TEST_REDIS_WORKER_DBS"
#: The driver of the default cache instance, and of a named instance that
#: names no driver of its own and overrides the default one.
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


def _driver_class(
    capability: str, name: str, driver: str
) -> type[InfraProvider] | None:
    """The provider class of ``capability/driver``, resolved through a registry
    of the built-in drivers and the installed driver packages, the way the
    application's own registry resolves it; nothing is built or opened."""
    registry = InfraRegistry()
    for spec in (*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES):
        registry.register_capability(spec)
    for (
        row_capability,
        row_driver,
        target,
        requires,
        install,
        delivered,
    ) in BUILTIN_DRIVER_ROWS:
        registry.register_driver(
            row_capability,
            row_driver,
            target,
            requires=requires,
            install=install,
            delivered_by=delivered,
        )
    load_selected_plugins(
        registry,
        {capability: {name: InstanceConfig(capability, name, driver)}},
    )
    return registry.driver_class(capability, driver)


def _eventbus_driver(values: Mapping[str, str | None]) -> str:
    """The default event bus driver ``EVENT_BUS_BACKEND`` selects (``auto``:
    streams in production, memory elsewhere)."""
    backend = (values.get("EVENT_BUS_BACKEND") or "auto").strip().lower()
    if backend != "auto":
        return backend
    prod = (values.get("ENVIRONMENT") or "").strip().lower() == "prod"
    return "streams" if prod else "memory"


def _isolated_options(
    capability: str,
    name: str,
    driver: str,
    options: Mapping[str, Any],
    worker_id: str,
) -> Mapping[str, Any]:
    """What the driver says worker ``worker_id``'s instance uses."""
    provider = _driver_class(capability, name, driver)
    if provider is None:  # a plain factory: no class to ask
        return options
    return provider.test_isolation(options, worker_id)


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
        if isinstance(spec.get("url"), str) and is_redis_url(spec["url"])
    ]
    if not (is_redis_url(cache) or is_redis_url(broker) or named):
        return {}
    layout = layout_from_environ(environ)
    _isolation.set_worker_layout(layout)
    out: dict[str, str] = {}
    if is_redis_url(cache):
        cache_driver = (
            infra.get("cache", {}).get("default", {}).get("driver")
        ) or DEFAULT_CACHE_DRIVER
        moved = _isolated_options(
            "cache", "default", str(cache_driver), {"url": cache}, worker_id
        )
        out["CACHE_URL"] = str(moved.get("url", cache))
    if is_redis_url(broker):
        # The one URL the harness moves itself: the Celery task queue has no
        # test_isolation hook until idk-worker-port-kit; it then goes through the
        # hook like the others, and this branch goes away.
        out["BROKER_URL"] = _isolation.moved_redis_url(broker, number, layout)
    if named:
        rewritten: dict[str, Any] = json.loads(infra_raw or "{}")
        for capability, name, spec in named:
            driver = spec.get("driver") or (
                DEFAULT_CACHE_DRIVER
                if (capability, name) == ("cache", "default")
                else _eventbus_driver(values)
                if (capability, name) == ("eventbus", "default")
                else None
            )
            if not driver:
                continue  # the registry refuses it when the settings load
            options = {k: v for k, v in spec.items() if k != "driver"}
            moved = _isolated_options(capability, name, str(driver), options, worker_id)
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
