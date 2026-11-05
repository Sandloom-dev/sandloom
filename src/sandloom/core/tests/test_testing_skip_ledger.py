"""The skip ledger plugin (``-p sandloom.core.testing.skip_ledger``).

Each CI lane writes the tests it ran and skipped; ``tools/ci/skip_ledger.py``
fails on a test skipped on every lane. Each case runs a tiny project in a real
``pytest`` subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS = """
import pytest

def test_passes():
    pass

@pytest.mark.skip(reason="needs a thing")
def test_marked_skip():
    pass

def test_skips_in_its_body():
    pytest.skip("found no thing")

@pytest.mark.xfail(reason="known")
def test_expected_failure():
    assert False

def test_fails():
    assert False

@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    if n == 2:
        pytest.skip("two")

@pytest.mark.deselect_me
def test_deselected():
    pass
"""


def _project(root: Path) -> Path:
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = ["-p", "no:cacheprovider"]\n'
        'markers = ["deselect_me: x"]\n'
    )
    (root / "sub" / "test_things.py").write_text(_TESTS)
    return root / "sub"


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    base = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    base = {k: v for k, v in base.items() if not k.startswith("SANDLOOM_SKIP_")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "sandloom.core.testing.skip_ledger",
            "-m",
            "not deselect_me",
            *args,
        ],
        cwd=cwd,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr  # test_fails fails
    return proc.stdout


_EXPECTED_RAN = [
    "test_things.py::test_expected_failure",
    "test_things.py::test_fails",
    "test_things.py::test_param[1]",
    "test_things.py::test_passes",
]
_EXPECTED_SKIPPED = {
    "test_things.py::test_marked_skip": "needs a thing",
    "test_things.py::test_param[2]": "two",
    "test_things.py::test_skips_in_its_body": "found no thing",
}


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("xdist", [False, True])
def test_the_ledger_holds_what_ran_and_what_skipped_with_the_reason(
    tmp_path: Path, xdist: bool
) -> None:
    root = _project(tmp_path)
    ledger = tmp_path / "out" / "ledger.json"
    workers = ["-n", "2"] if xdist else ["-p", "no:xdist"]
    out = _run(
        root,
        *workers,
        f"--sandloom-skip-ledger={ledger}",
        "--sandloom-skip-ledger-lane=sqlite",
    )
    data = json.loads(ledger.read_text())
    assert data["version"] == 1 and data["lane"] == "sqlite"
    assert data["ran"] == _EXPECTED_RAN
    assert data["skipped"] == _EXPECTED_SKIPPED
    assert "test_deselected" not in json.dumps(data)
    assert "sandloom-skip-ledger: lane sqlite, 4 ran, 3 skipped" in out


@pytest.mark.subprocess_probe
def test_the_environment_stands_for_the_options_and_the_base_prefixes_keys(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    ledger = tmp_path / "ledger.json"
    _run(
        root,
        "-p",
        "no:xdist",
        env={
            "SANDLOOM_SKIP_LEDGER": str(ledger),
            "SANDLOOM_SKIP_LEDGER_LANE": "postgresql",
            "SANDLOOM_SKIP_LEDGER_BASE": str(tmp_path),
        },
    )
    data = json.loads(ledger.read_text())
    assert data["lane"] == "postgresql"
    assert data["ran"] == [f"sub/{key}" for key in _EXPECTED_RAN]
    assert set(data["skipped"]) == {f"sub/{key}" for key in _EXPECTED_SKIPPED}


@pytest.mark.subprocess_probe
def test_without_a_path_nothing_is_written(tmp_path: Path) -> None:
    root = _project(tmp_path)
    out = _run(root, "-p", "no:xdist")
    assert "sandloom-skip-ledger" not in out
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("xdist", [False, True])
def test_a_module_skipped_while_collected_is_a_skipped_key(
    tmp_path: Path, xdist: bool
) -> None:
    root = _project(tmp_path)
    (root / "test_needs_lib.py").write_text(
        "import pytest\n"
        'pytest.importorskip("no_such_library_for_the_ledger")\n'
        "def test_uses_it():\n    pass\n"
    )
    (root / "test_skips_itself.py").write_text(
        "import pytest\n"
        'pytest.skip("not here", allow_module_level=True)\n'
        "def test_never():\n    pass\n"
    )
    ledger = tmp_path / "ledger.json"
    workers = ["-n", "2"] if xdist else ["-p", "no:xdist"]
    _run(root, *workers, f"--sandloom-skip-ledger={ledger}")
    skipped = json.loads(ledger.read_text())["skipped"]
    assert "no_such_library_for_the_ledger" in skipped["test_needs_lib.py"]
    assert skipped["test_skips_itself.py"] == "not here"
