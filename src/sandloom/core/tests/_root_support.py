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

import importlib
import importlib.util
import json
import os
from collections.abc import Mapping

from fastapi import FastAPI

from sandloom.core.composition import composition_root_name


def suite_infra_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ``INFRA`` a child process of a test needs where its root selects no
    driver: ``{}`` with the batteries installed (``sandloom_standard`` selects
    them), the suite's drivers in the kernel-without-batteries venv, whose lean
    root names none. An ``INFRA`` instance carries its own options, so the
    URLs are the ones of ``environ`` (the child's, default this process's)."""
    if importlib.util.find_spec("sandloom_standard") is not None:
        return {}
    env = os.environ if environ is None else environ
    cache = env["CACHE_URL"]
    infra = {
        "database": {"default": {"driver": "sqlalchemy", "url": env["DATABASE_URL"]}},
        "cache": {"default": {"driver": "redis", "url": cache}},
        "broadcast": {"default": {"driver": "redis", "url": cache}},
        "taskqueue": {
            "default": {
                "driver": "celery",
                "broker": env["BROKER_URL"],
                "backend": cache,
            }
        },
        "atomic_store": {"security": {"driver": "redis", "url": cache}},
        "lease": {"beat": {"driver": "redis", "url": cache}},
    }
    return {"INFRA": json.dumps(infra)}


#: The drivers a child process that boots the lean root (``import sandloom.main``,
#: ``sandloom worker`` with no ``SANDLOOM_COMPOSITION_ROOT``) selects through
#: ``INFRA``: the lean root names none, whatever is installed. Flat settings
#: (``CACHE_URL``, ``BROKER_URL``) fill their options.
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
    if str(values.get("EVENT_BUS_BACKEND", "auto")) == "auto":
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
    return json.dumps(merged)


def create_root_app() -> FastAPI:
    """A new app from the composition root's factory, with that root's trust map."""
    return importlib.import_module(composition_root_name()).create_app()  # type: ignore[no-any-return]
