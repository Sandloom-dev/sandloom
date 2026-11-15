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


def create_root_app() -> FastAPI:
    """A new app from the composition root's factory, with that root's trust map."""
    return importlib.import_module(composition_root_name()).create_app()  # type: ignore[no-any-return]
