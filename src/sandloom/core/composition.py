"""Composition-root resolution shared by every framework entrypoint.

The API, the Celery worker (``sandloom.core.worker``), ``sandloom-migrate`` and the
operational CLIs (``sandloom outbox`` / ``sandloom streams`` / ``sandloom projection``)
must
all act on EXACTLY the application the deployment runs: the platform's
``sandloom.main`` by default, or a downstream app's ASGI module (an ``sandloom new``
project uses ``<app>.asgi``) named by ``SANDLOOM_COMPOSITION_ROOT``. The root module
exposes ``core`` (a ``CoreFacade``) or ``app.state.core``.

``SANDLOOM_COMPOSITION_ROOT`` is read from the process environment first (it
selects which application to boot) and then from ``Settings`` (``.env`` /
``SECRETS_DIR``), so a generated app's ``.env`` configures local CLIs too.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from sandloom.core.config.settings import settings

#: Env var naming the composition root module (default ``sandloom.main``).
COMPOSITION_ROOT_ENV = "SANDLOOM_COMPOSITION_ROOT"
#: The platform's own composition root.
DEFAULT_COMPOSITION_ROOT = "sandloom.main"


class CompositionRootError(RuntimeError):
    """The configured composition root does not expose a framework core."""


def composition_root_name(root_name: str | None = None) -> str:
    """The composition root to boot.

    Explicit argument > the ``SANDLOOM_COMPOSITION_ROOT`` process env > the same
    setting from ``.env`` / ``SECRETS_DIR`` (``Settings.sandloom_composition_root``,
    so a generated app's ``.env`` is enough for local CLIs) > ``sandloom.main``.
    """
    if root_name:
        return root_name
    from_env = os.getenv(COMPOSITION_ROOT_ENV)
    if from_env:
        return from_env

    return settings.sandloom_composition_root or DEFAULT_COMPOSITION_ROOT


def _is_core(obj: object) -> bool:
    # Duck-typed so this module never imports the facade aggregator.
    return hasattr(obj, "loaded_modules") and hasattr(obj, "get_service")


def load_composition_core(root_name: str | None = None) -> Any:
    """Import the composition root and return its ``CoreFacade``."""
    name = composition_root_name(root_name)
    root = importlib.import_module(name)
    core = getattr(root, "core", None)
    if not _is_core(core):
        app = getattr(root, "app", None)
        core = getattr(getattr(app, "state", None), "core", None)
    if not _is_core(core):
        raise CompositionRootError(
            f"composition root {name!r} exposes neither `core` nor "
            f"`app.state.core`; set {COMPOSITION_ROOT_ENV} to your ASGI module"
        )
    return core


def load_composition_app(root_name: str | None = None) -> Any:
    """Import the composition root and return its ASGI ``app``.

    For CLIs that must enter the application's lifespan (e.g. a projection
    rebuild). The app must carry the framework core on ``app.state.core``.
    """
    name = composition_root_name(root_name)
    root = importlib.import_module(name)
    app = getattr(root, "app", None)
    if app is None or not _is_core(getattr(getattr(app, "state", None), "core", None)):
        raise CompositionRootError(
            f"composition root {name!r} exposes no `app` with `app.state.core`; "
            f"set {COMPOSITION_ROOT_ENV} to your ASGI module"
        )
    return app


__all__ = [
    "COMPOSITION_ROOT_ENV",
    "DEFAULT_COMPOSITION_ROOT",
    "CompositionRootError",
    "composition_root_name",
    "load_composition_app",
    "load_composition_core",
]
