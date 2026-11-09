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

from fastapi import FastAPI

from sandloom.core.composition import composition_root_name


def create_root_app() -> FastAPI:
    """A new app from the composition root's factory, with that root's trust map."""
    return importlib.import_module(composition_root_name()).create_app()  # type: ignore[no-any-return]
