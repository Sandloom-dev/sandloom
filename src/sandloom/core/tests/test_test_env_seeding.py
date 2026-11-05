"""An explicit ``SANDLOOM_TEST_<VAR>`` beats a shell-exported ``<VAR>``.

The root conftest seeds the environment before the settings snapshot. It used
to apply the explicit overrides with ``setdefault``, so a shell that still
exported ``DATABASE_URL`` (a developer's PostgreSQL) kept it while the
fixtures used ``SANDLOOM_TEST_DATABASE_URL`` (a SQLite file): settings, and
``skip_unless_db_feature`` with them, read the wrong database, and
``skip_locked`` races ran on SQLite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sandloom.core.testing.workers import seed_test_environ

REPO = Path(__file__).resolve().parents[4]
FILE = {"DATABASE_URL": "postgresql+asyncpg://u:p@h/file_db", "REDIS": "r"}
LITE = "sqlite+aiosqlite:///tmp/x_test.db"
SHELL_PG = "postgresql+asyncpg://u:p@h/shell_db"


def test_the_override_beats_the_shell_and_the_file() -> None:
    env = {"DATABASE_URL": SHELL_PG, "SANDLOOM_TEST_DATABASE_URL": LITE}
    seed_test_environ(FILE, environ=env)
    assert env["DATABASE_URL"] == LITE
    assert env["REDIS"] == "r"  # an unset key comes from the file


def test_without_an_override_the_shell_beats_the_file() -> None:
    env = {"DATABASE_URL": SHELL_PG}
    seed_test_environ(FILE, environ=env)
    assert env["DATABASE_URL"] == SHELL_PG
    env = {}
    seed_test_environ(FILE, environ=env)
    assert env["DATABASE_URL"] == FILE["DATABASE_URL"]


def test_skipped_keys_are_left_alone() -> None:
    env = {"SANDLOOM_TEST_DATABASE_URL": LITE}
    seed_test_environ(FILE, skip=frozenset({"DATABASE_URL"}), environ=env)
    assert "DATABASE_URL" not in env


def test_the_root_conftest_seeds_the_override_over_an_exported_url(
    tmp_path: Path,
) -> None:
    """The root conftest, run in a fresh process as pytest would import it."""
    lite = f"sqlite+aiosqlite:///{tmp_path / 'seed_test.db'}"
    probe = (
        "import importlib.util, os\n"
        f"spec = importlib.util.spec_from_file_location('c', {str(REPO / 'conftest.py')!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(os.environ['DATABASE_URL'])\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SANDLOOM_TEST_", "PYTEST_XDIST", "DATABASE_URL"))
    }
    env |= {"DATABASE_URL": SHELL_PG, "SANDLOOM_TEST_DATABASE_URL": lite}
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == lite
