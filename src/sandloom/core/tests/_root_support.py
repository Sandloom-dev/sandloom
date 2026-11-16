"""Build an app the way the composition root does (not a test module).

A bare ``sandloom.core.app_factory.create_app()`` composes the installed
modules and no claim-authorities map, so with a token issuer installed it
refuses the boot (SANDLOOM-B035). Kernel tests that need a second or fresh app
in the process build it through the composition root's own ``create_app``:
``sandloom_standard.main`` where the batteries are installed (the root
``conftest.py`` names it), the lean ``sandloom.main`` in the
kernel-without-batteries venv. Both factories take no arguments.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
from collections.abc import Mapping

from fastapi import FastAPI

from sandloom.core.composition import composition_root_name
from sandloom.core.testing.families import parse_infra
from sandloom.core.testing.infra import resolved_cache_url


def suite_infra_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ``INFRA`` a child process of a test needs where its root selects no
    driver: ``{}`` with the batteries installed (``sandloom_standard`` selects
    them), the suite's drivers in the kernel-without-batteries venv, whose lean
    root names none. An ``INFRA`` instance carries its own options, so the
    URLs are the ones of ``environ`` (the child's, default this process's).
    Where ``sandloom-redis`` is not installed (``kernel-without-redis``) the
    Redis-backed instances are the kernel's ``memory`` drivers, and the task
    queue joins only where ``sandloom-celery`` is installed."""
    if importlib.util.find_spec("sandloom_standard") is not None:
        return {}
    env = os.environ if environ is None else environ
    cache = resolved_cache_url(env)
    broker = (
        parse_infra(env.get("INFRA"))
        .get("taskqueue", {})
        .get("default", {})
        .get("broker", "")
    )
    redis_installed = importlib.util.find_spec("sandloom_redis") is not None
    memory: dict[str, str] = {"driver": "memory"}

    def redis_backed() -> dict[str, str]:
        return {"driver": "redis", "url": cache} if redis_installed else dict(memory)

    infra: dict[str, dict[str, dict[str, str]]] = {
        "database": {"default": {"driver": "sqlalchemy", "url": env["DATABASE_URL"]}},
        "cache": {"default": redis_backed()},
        "broadcast": {"default": redis_backed()},
        "atomic_store": {"security": redis_backed()},
        "lease": {"beat": redis_backed()},
    }
    if importlib.util.find_spec("sandloom_celery") is not None:
        infra["taskqueue"] = {"default": {"driver": "celery", "broker": broker}}
    return {"INFRA": json.dumps(infra)}


def redis_free_infra_env() -> dict[str, str]:
    """The ``INFRA`` a child that boots a GENERATED app needs without
    ``sandloom-redis``: the app's own selection names the Redis drivers
    (``sandloom new``: cache, broadcast, security store, beat lease), which a
    venv without them cannot build (SANDLOOM-B037, the ``kernel-without-redis``
    CI step). ``INFRA`` overrides them per instance with the kernel's
    ``memory`` drivers; with ``sandloom-redis`` installed it is ``{}`` and the
    app runs on its own drivers."""
    if importlib.util.find_spec("sandloom_redis") is not None:
        return {}
    memory = {"driver": "memory"}
    redis_instances: dict[str, dict[str, dict[str, str]]] = {}
    for capability, instances in generated_default_infra().items():
        for name, spec in instances.items():
            if spec.get("driver") == "redis":
                redis_instances.setdefault(capability, {})[name] = memory
    return {"INFRA": json.dumps(redis_instances)}


def generated_default_infra() -> dict[str, dict[str, dict[str, str]]]:
    """The ``DEFAULT_INFRA`` ``sandloom new`` writes into an app's
    ``__init__.py``, read from the template itself so a capability or instance
    the template adds is seen here without a second list."""
    from sandloom.core.cli import project

    tree = ast.parse(project._APP_INIT.substitute(app="probe"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [
            t.id for t in node.targets if isinstance(t, ast.Name)
        ] == ["DEFAULT_INFRA"]:
            infra: dict[str, dict[str, dict[str, str]]] = ast.literal_eval(node.value)
            return infra
    raise AssertionError("the app template no longer defines DEFAULT_INFRA")


#: The drivers a child process that boots the lean root (``import sandloom.main``,
#: ``sandloom worker`` with no ``SANDLOOM_COMPOSITION_ROOT``) selects through
#: ``INFRA``: the lean root names none, whatever is installed. The child's own
#: ``INFRA`` states the url of each instance.
_LEAN_ROOT_DRIVERS: dict[str, dict[str, dict[str, str]]] = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "cache": {"default": {"driver": "redis"}},
    "broadcast": {"default": {"driver": "redis"}},
    "taskqueue": {"default": {"driver": "celery"}},
}


def lean_root_infra(values: Mapping[str, object]) -> str:
    """The ``INFRA`` (JSON) a child that boots the lean root needs, given its
    environment ``values``: this module's driver selection for every instance
    the child's own ``INFRA`` (a JSON string or a mapping) does not give a
    driver, so an options-only entry (``{"database": {"default":
    {"tenant_rls": true}}}``) keeps its options and gains its driver. The
    durable ``streams`` bus joins where the child sets no ``EVENT_BUS_BACKEND``,
    and the security store where the child is not ``ENVIRONMENT=prod`` (a
    production child declares its own, on a server of its own: that refusal is
    what its test may probe)."""
    own_raw = values.get("INFRA") or {}
    own: dict[str, dict[str, dict[str, object]]] = json.loads(
        own_raw if isinstance(own_raw, str) else json.dumps(own_raw)
    )
    wanted = {cap: dict(instances) for cap, instances in _LEAN_ROOT_DRIVERS.items()}
    if not values.get("EVENT_BUS_BACKEND"):
        wanted["eventbus"] = {"default": {"driver": "streams"}}
    if values.get("ENVIRONMENT") != "prod":
        wanted["atomic_store"] = {"security": {"driver": "redis"}}
    merged: dict[str, dict[str, dict[str, object]]] = {
        cap: {name: dict(spec) for name, spec in instances.items()}
        for cap, instances in own.items()
    }
    for capability, instances in wanted.items():
        bucket = merged.setdefault(capability, {})
        for name, spec in instances.items():
            entry = bucket.setdefault(name, {})
            entry.setdefault("driver", spec["driver"])
    # An instance INFRA names carries its own options: the database url is not
    # filled from DATABASE_URL (the composition's defaults are), so write it.
    database_url = values.get("DATABASE_URL")
    if isinstance(database_url, str):
        merged["database"]["default"].setdefault("url", database_url)
    # A Redis instance with no url refuses startup in every environment, so a
    # redis security store INFRA names gets the url of the child's own cache
    # instance; one on another driver (``database``, ``memory``) gets none.
    cache_url = merged.get("cache", {}).get("default", {}).get("url")
    security = merged.get("atomic_store", {}).get("security")
    if (
        isinstance(cache_url, str)
        and security is not None
        and security.get("driver") == "redis"
    ):
        security.setdefault("url", cache_url)
    return json.dumps(merged)


def create_root_app() -> FastAPI:
    """A new app from the composition root's factory, with that root's trust map."""
    return importlib.import_module(composition_root_name()).create_app()  # type: ignore[no-any-return]
