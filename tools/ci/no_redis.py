"""Run the kernel's tests with no Redis client and no Redis driver installed.

The ``kernel-without-redis`` step of the ``kernel-without-batteries`` CI job
(ADR-0016): the kernel wheel names no Redis, so its test harness and its suites
must run in a venv where ``redis``, ``sandloom-redis`` and ``sandloom-celery``
(whose ``celery[redis]`` brings the client) are uninstalled. The suite then
selects the kernel's ``memory`` drivers for the instances it backs with Redis
(``conftest.py``), and the harness runs its no-family path (a process with no
client loads no family).

``pytest -p tools.ci.no_redis ...``
    refuses to run while one of :data:`REDIS_DISTRIBUTIONS` is importable, and
    applies ``no_redis_pending.json``: the kernel tests that still need a Redis
    or Celery driver and are not yet moved to ``packages/sandloom-redis/tests``
    or ``packages/sandloom-celery/tests``. The lists only go down:

    * a listed file is not collected (it imports a driver at module level);
    * a listed test runs as a strict xfail: one that passes without Redis
      fails the run (``XPASS(strict)``) until its entry is removed;
    * an entry whose file is gone, or whose collected file no longer holds the
      test, is stale and refuses the run;
    * more entries than :data:`CEILING_FILES` / :data:`CEILING_TESTS` refuse
      the run: a new kernel test that needs Redis fails the step instead of
      joining the list.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

#: Importable names that make the venv "with Redis": the client, the Redis
#: driver package and the Celery one (its ``celery[redis]`` pulls the client).
REDIS_DISTRIBUTIONS = ("redis", "sandloom_redis", "sandloom_celery")
PENDING = Path(__file__).with_name("no_redis_pending.json")
#: How many files / tests the pending list may hold. They only go down: lower
#: a ceiling with the entries it lost, never raise one (review holds that).
CEILING_FILES = 24
CEILING_TESTS = 96


def importable_redis() -> list[str]:
    """The Redis names this interpreter could import (none in the lane's venv)."""
    return [name for name in REDIS_DISTRIBUTIONS if importlib.util.find_spec(name)]


def load_pending(path: Path = PENDING) -> tuple[dict[str, str], dict[str, str]]:
    """``(files, tests)``: ``{path or test id without parameters: why}``.

    The file has a ``files`` and a ``tests`` section, each mapping a reason to
    its entries; keys starting with ``_`` are notes.
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    def section(name: str) -> dict[str, str]:
        return {entry: why for why, entries in data[name].items() for entry in entries}

    return section("files"), section("tests")


def above_ceiling(
    files: dict[str, str],
    tests: dict[str, str],
    ceiling_files: int | None = None,
    ceiling_tests: int | None = None,
) -> list[str]:
    """What the lists hold beyond their ceilings (empty: within them)."""
    over = []
    allowed_files = CEILING_FILES if ceiling_files is None else ceiling_files
    allowed_tests = CEILING_TESTS if ceiling_tests is None else ceiling_tests
    if len(files) > allowed_files:
        over.append(f"files: {len(files)} entries, ceiling {allowed_files}")
    if len(tests) > allowed_tests:
        over.append(f"tests: {len(tests)} entries, ceiling {allowed_tests}")
    return over


def stale_files(files: dict[str, str], root: Path) -> list[str]:
    """The listed files that are gone from ``root``."""
    return sorted(path for path in files if not (root / path).is_file())


def _base_id(nodeid: str) -> str:
    """``nodeid`` without its parametrization suffix."""
    return nodeid.split("[", 1)[0]


def listed_reason(tests: dict[str, str], nodeid: str) -> str | None:
    """Why ``nodeid`` is listed: by its exact id (one parameter set) or by its
    id without parameters (the whole parametrized test); ``None``: not listed."""
    return tests.get(nodeid) or tests.get(_base_id(nodeid))


def stale_tests(tests: dict[str, str], nodeids: list[str], root: Path) -> list[str]:
    """The listed tests that name nothing.

    An entry whose file is gone from ``root`` is stale whatever was collected;
    one whose file was collected but holds no such test (id, or id without
    parameters) is stale too. An entry in an existing file this run did not
    collect is not judged, so a run of part of the suites reports no more.
    """
    known = set(nodeids) | {_base_id(nodeid) for nodeid in nodeids}
    files = {nodeid.split("::", 1)[0] for nodeid in nodeids}
    stale = []
    for test in tests:
        path = test.split("::", 1)[0]
        if not (root / path).is_file() or (path in files and test not in known):
            stale.append(test)
    return sorted(stale)


def pytest_configure(config: Any) -> None:
    """Refuse a run with Redis importable, a stale file entry or a grown list."""
    import pytest

    found = importable_redis()
    if found:
        raise pytest.UsageError(
            f"tools.ci.no_redis: {', '.join(found)} importable; uninstall them "
            "(`uv pip uninstall redis sandloom-redis sandloom-celery`) first"
        )
    files, tests = load_pending()
    grown = above_ceiling(files, tests)
    if grown:
        raise pytest.UsageError(
            "tools/ci/no_redis_pending.json only goes down; over the ceilings "
            "in tools/ci/no_redis.py: " + "; ".join(grown) + ". Move the test "
            "to packages/sandloom-redis/tests or run it on the memory drivers."
        )
    gone = stale_files(files, Path(config.rootpath))
    if gone:
        raise pytest.UsageError(
            "tools/ci/no_redis_pending.json names files that no longer exist "
            "(moved, renamed, deleted or fixed): remove them and lower "
            "CEILING_FILES:\n  " + "\n  ".join(gone)
        )


def pytest_ignore_collect(collection_path: Path, config: Any) -> bool | None:
    """Do not collect a listed file: it imports a Redis driver at module level."""
    files, _ = load_pending()
    try:
        relative = collection_path.relative_to(Path(config.rootpath)).as_posix()
    except ValueError:
        return None
    return True if relative in files else None


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Run the listed tests as strict xfails; refuse stale entries."""
    import pytest

    _, tests = load_pending()
    stale = stale_tests(tests, [item.nodeid for item in items], Path(config.rootpath))
    if stale:
        raise pytest.UsageError(
            "tools/ci/no_redis_pending.json names tests that no longer exist "
            "(moved, renamed, deleted or fixed): remove them and lower "
            "CEILING_TESTS:\n  " + "\n  ".join(stale)
        )
    for item in items:
        why = listed_reason(tests, item.nodeid)
        if why is not None:
            item.add_marker(
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"tools/ci/no_redis_pending.json ({why}): passes without "
                        "Redis now, remove its entry and lower CEILING_TESTS"
                    ),
                )
            )
