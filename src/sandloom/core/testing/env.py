"""The test environment file (``.env.test``): find and load it, no pytest.

``sandloom test`` and ``sandloom migrate new`` load it before the settings
object is built, also in an install without the ``testing`` extra (no
pytest): the pytest plugin's fixture (:mod:`sandloom.core.testing.fixtures`)
wraps the same loader and reports a missing file as a usage error.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path

from dotenv import load_dotenv

import sandloom.core.config.settings as _settings
from sandloom.core.testing.connections import apply_test_pool_profile
from sandloom.core.testing.workers import apply_test_overrides, apply_worker_env

#: The test environment file every Sandloom project carries.
ENV_FILE = ".env.test"
#: This checkout's test servers, written by ``sandloom dev up`` (gitignored).
LOCAL_ENV_FILE = ".env.test.local"


#: What a refusal or an unreachable test server adds when the checkout has
#: no ``.env.test.local`` (``.env.test`` alone names the shared default).
DEV_UP_HINT = (
    "this checkout has no .env.test.local: run `uv run sandloom dev up` first "
    "(it starts this checkout's own test servers and writes that file)"
)


def dev_up_hint(starts: Iterable[Path]) -> str | None:
    """:data:`DEV_UP_HINT` when the nearest ``.env.test`` has no
    ``.env.test.local`` beside it; ``None`` otherwise."""
    path = find_env_file(list(starts))
    if path is None or (path.parent / LOCAL_ENV_FILE).is_file():
        return None
    return DEV_UP_HINT


def settings_read() -> bool:
    """Whether the process settings were loaded already (their environment
    snapshot taken). Importing the settings module is not reading it: the
    plugins import it (the kernel's imports reach it), the first attribute
    access loads it."""
    return _settings.settings_loaded()


def ignore_dotenv() -> None:
    """Make the process settings of this test run skip the developer's ``.env``.

    ``Settings`` reads shell > ``.env`` > secrets directory, so a key only the
    dev ``.env`` sets (``TENANCY_MODE=single``) would reach the tests through
    it. A test run is composed of the shell, ``.env.test``, ``.env.test.local``
    and ``SANDLOOM_TEST_*`` only: the pytest plugin calls this right after
    :func:`load_test_env`, before anything loads the settings. Idempotent."""
    _settings.Settings.model_config["env_file"] = None


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
    """Load the nearest ``.env.test`` into ``os.environ`` (override=True),
    then the ``.env.test.local`` beside it (override=False).

    ``override=True`` so a leftover shell value (a dev ``DATABASE_URL``) never
    masks the test configuration; an explicit ``SANDLOOM_TEST_DATABASE_URL`` /
    ``SANDLOOM_TEST_CACHE_URL`` / ``SANDLOOM_TEST_BROKER_URL`` then wins over the file
    (merged into the file's ``INFRA`` instances; one that finds no declared
    default cache / task queue raises).
    On a pytest-xdist worker the URLs are then rewritten to the worker's own
    backends (:mod:`sandloom.core.testing.workers`).
    Raises :class:`MissingTestEnvError` when no file is found. ``warn_if_late``
    warns when the process settings were already LOADED (:func:`settings_read`:
    importing them reads nothing, only the first use does). ``create_database=False``
    leaves an explicitly named test database alone, for a command that connects
    to none.
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

    if warn_if_late and settings_read():
        warnings.warn(
            f"the Sandloom settings were read before {path} was loaded, so they "
            "keep the values of the environment at that moment. Something used "
            "them before the Sandloom pytest plugin ran: another pytest plugin "
            "that is loaded first, or a conftest.py named in `-p`. List "
            "`-p sandloom.sdk.testing.plugin` first in addopts.",
            stacklevel=2,
        )
    load_dotenv(dotenv_path=path, override=True)
    # This checkout's test servers (``sandloom dev up``): only next to the
    # .env.test found, never walking up (a worktree nested in the main
    # checkout must not read the main checkout's file); an exported
    # SANDLOOM_TEST_* (or a held test-run slot's) wins.
    local = path.parent / LOCAL_ENV_FILE
    if local.is_file():
        load_dotenv(dotenv_path=local, override=False)
    # Small pools for a test process unless configured (connection budget).
    apply_test_pool_profile()
    # Explicit SANDLOOM_TEST_<VAR> overrides win over the file (parallel suites).
    apply_test_overrides(create_database=create_database)
    # Under pytest-xdist: move this worker onto its own database / keyed-server DBs
    # (idempotent, so later reloads never put it back on the shared ones).
    apply_worker_env(create_database=create_database)
    return path


__all__ = [
    "DEV_UP_HINT",
    "ENV_FILE",
    "LOCAL_ENV_FILE",
    "MissingTestEnvError",
    "dev_up_hint",
    "find_env_file",
    "ignore_dotenv",
    "load_test_env",
    "settings_read",
]
