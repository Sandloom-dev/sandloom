"""tools/ci/no_redis.py: the kernel-without-redis step's plugin and list.

The CI step (ADR-0016) runs the kernel suites in a venv with no Redis client
and no Redis or Celery driver package. It is only as good as its plugin: a
venv that still imports one of them must refuse the run, the pending list
only goes down, a listed test that passes without Redis fails as a strict
xfail, and an entry that names nothing is stale.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.ci import no_redis

REPO = Path(__file__).resolve().parents[2]


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


def test_every_listed_file_and_test_names_an_existing_file() -> None:
    files, tests = no_redis.load_pending()
    assert no_redis.stale_files(files, REPO) == []
    for test in tests:
        assert (REPO / test.split("::", 1)[0]).is_file(), test


#: What the no-Redis venv lacks, by top-level import name (the client, its C
#: parser, both driver packages and the Celery stack the Celery driver needs).
REDIS_STACK = frozenset(
    {"redis", "hiredis", "sandloom_redis", "sandloom_celery", "celery", "kombu"}
)
#: The kernel suites the no-Redis lane collects (.github/workflows/ci.yml).
KERNEL_SUITES = (
    "src/sandloom/core/tests",
    "tests/sdk",
    "tests/architecture",
    "tests/integration/replaceability",
)


#: Calls that import a module by the string they get. ``find_spec`` is left
#: out on purpose: it only probes, which is how a kernel test skips itself on a
#: lean venv without importing the driver.
DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})


def _called_name(call: ast.Call) -> str:
    """``f`` of ``f(...)``, ``attr`` of ``x.attr(...)``, else ``""``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def redis_stack_importers(files: list[Path], root: Path = REPO) -> dict[str, list[str]]:
    """``{path: names}`` of the files that import a name of :data:`REDIS_STACK`
    anywhere (module level or inside a function body: the latter only fails
    when the test runs), through an import statement or through a string
    constant given as the first argument of ``import_module`` / ``__import__``
    (a name built at run time is not seen; a ``find_spec`` probe imports
    nothing and is not counted)."""
    found: dict[str, list[str]] = {}
    for path in files:
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
            elif (
                isinstance(node, ast.Call)
                and _called_name(node) in DYNAMIC_IMPORTS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value.split(".")[0])
        if hit := sorted(names & REDIS_STACK):
            found[path.relative_to(root).as_posix()] = hit
    return found


def test_only_pending_kernel_tests_import_the_redis_stack() -> None:
    """A kernel test file that imports Redis or Celery without being named by
    tools/ci/no_redis_pending.json would pass the collect-only pass of the lane
    (a function-level import fails only when it runs) and then run, unchecked,
    only where the driver is installed: it belongs in the pending list or in
    the driver package's tests."""
    files, tests = no_redis.load_pending()
    pending = set(files) | {test.split("::", 1)[0] for test in tests}
    sources = [path for suite in KERNEL_SUITES for path in (REPO / suite).rglob("*.py")]
    assert sources
    importers = redis_stack_importers(sources)
    assert sorted(set(importers) - pending) == []


def test_the_import_scan_sees_a_function_level_and_a_dotted_import(
    tmp_path: Path,
) -> None:
    (tmp_path / "t_dotted.py").write_text("import redis.asyncio\n")
    (tmp_path / "t_nested.py").write_text(
        "def test_a():\n    from sandloom_celery.worker import CeleryWorkerHost\n"
    )
    (tmp_path / "t_clean.py").write_text(
        "import sandloom.core\nfrom . import redis\nNAME = 'import celery'\n"
    )
    paths = sorted(tmp_path.glob("*.py"))
    assert redis_stack_importers(paths, tmp_path) == {
        "t_dotted.py": ["redis"],
        "t_nested.py": ["sandloom_celery"],
    }


def test_the_import_scan_sees_a_string_given_to_a_dynamic_import(
    tmp_path: Path,
) -> None:
    (tmp_path / "t_import_module.py").write_text(
        "import importlib\n\ndef test_a():\n    importlib.import_module('celery.app')\n"
    )
    (tmp_path / "t_dunder.py").write_text("m = __import__('hiredis')\n")
    (tmp_path / "t_clean.py").write_text(
        "import importlib\nimportlib.import_module('sandloom.core')\n"
        "importlib.import_module(name)\nprint('redis')\n"
        "OK = importlib.util.find_spec('kombu') is not None\n"
    )
    paths = sorted(tmp_path.glob("*.py"))
    assert redis_stack_importers(paths, tmp_path) == {
        "t_dunder.py": ["hiredis"],
        "t_import_module.py": ["celery"],
    }


def test_the_pending_list_never_grows_past_its_ceilings() -> None:
    files, tests = no_redis.load_pending()
    assert no_redis.above_ceiling(files, tests) == []


def test_a_list_above_a_ceiling_is_reported() -> None:
    files, tests = {"a.py": "x", "b.py": "x"}, {"a.py::t": "x"}
    assert no_redis.above_ceiling(files, tests, 2, 1) == []
    assert no_redis.above_ceiling(files, tests, 1, 1) == ["files: 2 entries, ceiling 1"]
    assert no_redis.above_ceiling(files, tests, 2, 0) == ["tests: 1 entries, ceiling 0"]


def test_a_test_is_listed_by_its_exact_id_or_by_its_id_without_parameters() -> None:
    tests = {"t.py::test_a": "whole", "t.py::test_b[x]": "one parameter"}
    assert no_redis.listed_reason(tests, "t.py::test_a[1]") == "whole"
    assert no_redis.listed_reason(tests, "t.py::test_a") == "whole"
    assert no_redis.listed_reason(tests, "t.py::test_b[x]") == "one parameter"
    assert no_redis.listed_reason(tests, "t.py::test_b[y]") is None
    assert no_redis.listed_reason(tests, "t.py::test_c") is None


def test_stale_tests_name_a_gone_file_or_a_test_the_file_no_longer_holds(
    tmp_path: Path,
) -> None:
    (tmp_path / "t.py").write_text("")
    tests = {
        "t.py::test_a": "x",
        "t.py::test_b[x]": "x",
        "t.py::test_gone": "x",
        "t.py::test_b[gone]": "x",
        "missing.py::test_a": "x",
        "other.py::test_a": "x",
    }
    (tmp_path / "other.py").write_text("")
    nodeids = ["t.py::test_a[1]", "t.py::test_b[x]"]
    assert no_redis.stale_tests(tests, nodeids, tmp_path) == [
        "missing.py::test_a",
        "t.py::test_b[gone]",
        "t.py::test_gone",
    ]


def test_a_venv_with_a_redis_name_importable_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(no_redis, "importable_redis", lambda: ["sandloom_redis"])
    with pytest.raises(pytest.UsageError, match="sandloom_redis importable"):
        no_redis.pytest_configure(SimpleNamespace(rootpath=REPO))


def test_the_lane_refusal_and_the_importer_scan_name_the_same_stack() -> None:
    assert set(no_redis.REDIS_DISTRIBUTIONS) == REDIS_STACK
    assert len(no_redis.REDIS_DISTRIBUTIONS) == len(REDIS_STACK)


def test_importable_redis_sees_an_importable_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {"redis": object(), "sandloom_celery": object()}
    monkeypatch.setattr(
        no_redis.importlib.util, "find_spec", lambda name: seen.get(name)
    )
    assert no_redis.importable_redis() == ["redis", "sandloom_celery"]


def test_a_listed_file_is_not_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        no_redis, "load_pending", lambda: ({"tests/needs_redis.py": "x"}, {})
    )
    config = SimpleNamespace(rootpath=tmp_path)
    assert (
        no_redis.pytest_ignore_collect(tmp_path / "tests/needs_redis.py", config)
        is True
    )
    assert no_redis.pytest_ignore_collect(tmp_path / "tests/test_ok.py", config) is None


def test_a_listed_test_runs_as_a_strict_xfail_and_a_stale_entry_refuses_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t.py").write_text("")
    monkeypatch.setattr(no_redis, "load_pending", lambda: ({}, {"t.py::test_a": "why"}))
    items = [_Item("t.py::test_a[1]"), _Item("t.py::test_b")]
    no_redis.pytest_collection_modifyitems(SimpleNamespace(rootpath=tmp_path), items)
    (marker,) = items[0].markers
    assert marker.mark.name == "xfail" and marker.mark.kwargs["strict"] is True
    assert "why" in marker.mark.kwargs["reason"]
    assert items[1].markers == []
    monkeypatch.setattr(no_redis, "load_pending", lambda: ({}, {"gone.py::t": "x"}))
    with pytest.raises(pytest.UsageError, match="gone.py::t"):
        no_redis.pytest_collection_modifyitems(
            SimpleNamespace(rootpath=tmp_path), [_Item("t.py::test_b")]
        )


def test_the_plugin_end_to_end_in_a_child_pytest(tmp_path: Path) -> None:
    """A listed file is skipped, a listed failing test is an xfail, a listed
    test that passes fails the run, and one parameter of a test can be listed
    alone; an unlisted failure still fails."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_needs_driver_import.py").write_text(
        "import sandloom_redis_not_installed\n\n\ndef test_x():\n    pass\n"
    )
    (tmp_path / "test_probe.py").write_text(
        "import pytest\n\n\n"
        "def test_passes_now():\n    pass\n\n\n"
        "def test_still_needs_redis():\n    raise RuntimeError('needs redis')\n\n\n"
        "@pytest.mark.parametrize('name', ['redis', 'shop'])\n"
        "def test_one_name_needs_redis(name):\n    assert name != 'redis'\n\n\n"
        "def test_unlisted_failure():\n    raise RuntimeError('a real failure')\n"
    )
    pending = tmp_path / "pending.json"
    pending.write_text(
        '{"files": {"w": ["test_needs_driver_import.py"]}, "tests": {"w": ['
        '"test_probe.py::test_passes_now", '
        '"test_probe.py::test_still_needs_redis", '
        '"test_probe.py::test_one_name_needs_redis[redis]"]}}'
    )
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\n"
        "from tools.ci import no_redis\n"
        f"_PENDING = Path({str(pending)!r})\n"
        "_load = no_redis.load_pending\n"
        "no_redis.importable_redis = lambda: []\n"
        "no_redis.load_pending = lambda path=_PENDING: _load(path)\n"
        "no_redis.CEILING_FILES = 1\n"
        "no_redis.CEILING_TESTS = 3\n"
        "pytest_configure = no_redis.pytest_configure\n"
        "pytest_ignore_collect = no_redis.pytest_ignore_collect\n"
        "pytest_collection_modifyitems = no_redis.pytest_collection_modifyitems\n"
    )
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rfxX", "-q"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    out = done.stdout
    assert done.returncode == 1, out
    assert "test_needs_driver_import" not in out  # not collected: no ImportError
    assert "FAILED test_probe.py::test_passes_now" in out  # XPASS(strict)
    assert "FAILED test_probe.py::test_unlisted_failure" in out
    assert "XFAIL test_probe.py::test_still_needs_redis" in out
    assert "XFAIL test_probe.py::test_one_name_needs_redis[redis]" in out
    # the other parameter was not listed: it ran and passed
    assert "test_one_name_needs_redis[shop]" not in out
    assert "1 passed" in out
