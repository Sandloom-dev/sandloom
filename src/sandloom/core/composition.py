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

An authoring command of a checkout (``sandloom generate``, ``sandloom admin export``,
``sandloom check --boot``, the contract and route snapshots, the static steps of
``sandloom test``) also reads a project default: ``composition-root`` under
``[tool.sandloom]`` in the project's ``pyproject.toml``, behind both of the above
and ahead of ``sandloom.main``. Only a caller that passes its ``project_root`` reads
it: the runtime entrypoints (API, worker, ``sandloom migrate``, the operator CLIs)
and the pytest suites keep the three-step order, so no test boots another
composition for it. ``sandloom test`` hands ``sandloom migrate drift`` the resolved
root in the variable.
"""

from __future__ import annotations

import importlib
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sandloom.core.config.settings import settings

#: Env var naming the composition root module (default ``sandloom.main``).
COMPOSITION_ROOT_ENV = "SANDLOOM_COMPOSITION_ROOT"
#: The platform's own composition root.
DEFAULT_COMPOSITION_ROOT = "sandloom.main"


class CompositionRootError(RuntimeError):
    """The configured composition root does not expose a framework core."""


def project_composition_root(project_root: Path) -> str | None:
    """The project's own default root: ``[tool.sandloom] composition-root`` of
    ``<project_root>/pyproject.toml`` (``None``: no file, no table, no key).

    A malformed default refuses: one that silently fell through to the lean
    ``sandloom.main`` would build another application than the one named. That is
    an unreadable file, a ``tool`` or ``tool.sandloom`` that is not a table, and a
    value that is not a dotted module path.
    """
    path = project_root / "pyproject.toml"
    where = f"[tool.sandloom] composition-root in {path}"
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CompositionRootError(f"{path} cannot be read for {where}: {exc}") from exc
    tool = data.get("tool")
    if tool is None:
        return None
    if not isinstance(tool, dict):
        raise CompositionRootError(f"{where}: `tool` must be a table, not {tool!r}")
    table = tool.get("sandloom")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise CompositionRootError(
            f"{where}: `tool.sandloom` must be a table, not {table!r}"
        )
    value = table.get("composition-root")
    if value is None:
        return None
    if not isinstance(value, str) or not all(
        part.isidentifier() for part in value.split(".")
    ):
        raise CompositionRootError(
            f"{where} must be a dotted module path (like `app.asgi`), not {value!r}"
        )
    return value


def composition_root_name(
    root_name: str | None = None, *, project_root: Path | None = None
) -> str:
    """The composition root to boot.

    Explicit argument > the ``SANDLOOM_COMPOSITION_ROOT`` process env > the same
    setting from ``.env`` / ``SECRETS_DIR`` (``Settings.sandloom_composition_root``,
    so a generated app's ``.env`` is enough for local CLIs) > the project default
    (``[tool.sandloom] composition-root``, read only when ``project_root`` is
    given: the authoring commands of a checkout) > ``sandloom.main``.
    """
    if root_name:
        return root_name
    from_env = os.getenv(COMPOSITION_ROOT_ENV)
    if from_env:
        return from_env
    if settings.sandloom_composition_root:
        return settings.sandloom_composition_root
    if project_root is not None:
        return project_composition_root(project_root) or DEFAULT_COMPOSITION_ROOT
    return DEFAULT_COMPOSITION_ROOT


def root_default_infra(root_name: str | None = None) -> Mapping[str, Any] | None:
    """The driver selection the composition root's package exports as
    ``DEFAULT_INFRA`` (``None`` when the root has no package or exports none):
    what ``create_app(default_infra=...)`` is given, readable without building
    the app, for a process that boots none (``sandloom worker check``)."""
    package = composition_root_name(root_name).rpartition(".")[0]
    if not package:
        return None
    try:
        module = importlib.import_module(package)
    except ModuleNotFoundError as exc:
        if exc.name != package:
            raise
        return None
    found = getattr(module, "DEFAULT_INFRA", None)
    return found if isinstance(found, Mapping) else None


def _is_core(obj: object) -> bool:
    # Duck-typed so this module never imports the facade aggregator.
    return hasattr(obj, "loaded_modules") and hasattr(obj, "get_service")


def load_composition_core(
    root_name: str | None = None, *, project_root: Path | None = None
) -> Any:
    """Import the composition root and return its ``CoreFacade``."""
    name = composition_root_name(root_name, project_root=project_root)
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


def load_composition_app(
    root_name: str | None = None, *, project_root: Path | None = None
) -> Any:
    """Import the composition root and return its ASGI ``app``.

    For CLIs that must enter the application's lifespan (e.g. a projection
    rebuild). The app must carry the framework core on ``app.state.core``.
    """
    name = composition_root_name(root_name, project_root=project_root)
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
    "project_composition_root",
]
