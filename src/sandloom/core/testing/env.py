"""The test environment file (``.env.test``): find and load it, no pytest.

``sandloom test`` and ``sandloom migrate new`` load it before the settings
object is built, also in an install without the ``testing`` extra (no
pytest): the pytest plugin's fixture (:mod:`sandloom.core.testing.fixtures`)
wraps the same loader and reports a missing file as a usage error.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Iterable
from pathlib import Path

from dotenv import load_dotenv

from sandloom.core.testing.connections import apply_test_pool_profile
from sandloom.core.testing.workers import apply_test_overrides, apply_worker_env

#: The test environment file every Sandloom project carries.
ENV_FILE = ".env.test"


class MissingTestEnvError(RuntimeError):
    """No ``.env.test`` walking up from any start."""


def find_env_file(starts: Iterable[Path]) -> Path | None:
    """The nearest ``.env.test`` walking up from each start (first hit wins)."""
    for start in starts:
        here = start.resolve()
        if here.is_file():
            here = here.parent
        for directory in (here, *here.parents):
            candidate = directory / ENV_FILE
            if candidate.is_file():
                return candidate
    return None


def load_test_env(
    starts: Iterable[Path], *, warn_if_late: bool = False, create_database: bool = True
) -> Path:
    """Load the nearest ``.env.test`` into ``os.environ`` (override=True).

    ``override=True`` so a leftover shell value (a dev ``DATABASE_URL``) never
    masks the test configuration; an explicit ``SANDLOOM_TEST_DATABASE_URL`` /
    ``SANDLOOM_TEST_CACHE_URL`` / ``SANDLOOM_TEST_BROKER_URL`` then wins over the file.
    On a pytest-xdist worker the URLs are then rewritten to the worker's own
    backends (:mod:`sandloom.core.testing.workers`).
    Raises :class:`MissingTestEnvError` when no file is found. ``warn_if_late``
    warns when ``sandloom.core.config.settings`` (which snapshots the environment at
    import) was already imported. ``create_database=False`` leaves an explicitly
    named test database alone, for a command that connects to none.
    """
    starts = list(starts)
    path = find_env_file(starts)
    if path is None:
        searched = ", ".join(str(s) for s in starts) or "<nothing>"
        raise MissingTestEnvError(
            f"sandloom.core.testing: no {ENV_FILE} found walking up from {searched}. "
            f"Create one at the project root (sandloom new generates it) with at "
            f"least DATABASE_URL pointing at a disposable test database."
        )

    if warn_if_late and "sandloom.core.config.settings" in sys.modules:
        warnings.warn(
            f"sandloom.core.config.settings was imported before {path} "
            "was loaded; settings "
            "read at import time keep their previous values. Activate the plugin "
            "with `-p sandloom.core.testing.plugin` in addopts so it loads first.",
            stacklevel=2,
        )
    load_dotenv(dotenv_path=path, override=True)
    # Small pools for a test process unless configured (connection budget).
    apply_test_pool_profile()
    # Explicit SANDLOOM_TEST_<VAR> overrides win over the file (parallel suites).
    apply_test_overrides(create_database=create_database)
    # Under pytest-xdist: move this worker onto its own database / Redis DBs
    # (idempotent, so later reloads never put it back on the shared ones).
    apply_worker_env()
    return path


__all__ = ["ENV_FILE", "MissingTestEnvError", "find_env_file", "load_test_env"]
