"""The standard stack's scaffold is byte-for-byte what it was before the
backing-service text moved out of the kernel (redis-out, S5).

``sandloom new`` and ``sandloom add deploy`` render the standard stack's compose
files, URLs and CI service from data the stack's driver package supplies
(``sandloom_redis.stack``), not from text of the kernel. The data moved, the output
must not: this test hashes every file the two commands write and compares the hashes
with ``scaffold_standard_stack.sha256.json``, taken before the move. A changed byte
in a compose file, a url or a workflow fails it, naming the file. A change meant to
alter the scaffold (a new image, a new option) updates the manifest in the same
commit and reruns the DX eval (``benchmarks/dx/README.md``): the manifest is the
reason the eval is not rerun for a refactor.

The one random file (``.env``: a fresh ``JWT_SECRET``) is left out, and the version
range of the framework dependency is normalised so a release does not move the
manifest.

The same bytes come out of the other path too: a bare kernel (no stack package
importable) writes the app with the stack's markers, and ``sandloom add stack`` run
where the package is installed (in this interpreter, and as a child process, the way
the new app's own environment runs it) fills them.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import add as _add
from sandloom.core.cli import project as _project
from sandloom.core.cli import scaffold_deploy as _deploy
from sandloom.core.cli import stack_files as _stack_files
from sandloom.core.cli import stack_service as _stack
from sandloom.core.tests.test_scaffold_deploy import probe_app

MANIFEST = Path(__file__).with_name("scaffold_standard_stack.sha256.json")
#: ``sandloom new`` runs of the snapshot: the lean app and the one with every battery.
NEW_RUNS: dict[str, list[str]] = {
    "new_lean": [],
    "new_batteries": ["identity", "tenancy", "billing", "admin"],
}
#: Left out of the hashes: written with a random secret.
RANDOM_FILES = frozenset({".env"})


#: The checkout the code runs from: a source checkout's ``pyproject.toml`` of
#: the new app names the framework by this absolute path, which differs on every
#: machine (a developer's, CI's runner).
CHECKOUT = Path(_project.__file__).resolve().parents[4]


def _digest(path: Path) -> str:
    data = path.read_bytes().replace(_project.FRAMEWORK_RANGE.encode(), b"<range>")
    data = data.replace(CHECKOUT.as_posix().encode(), b"<checkout>")
    return hashlib.sha256(data).hexdigest()


def _tree(root: Path, prefix: str) -> dict[str, str]:
    return {
        f"{prefix}/{path.relative_to(root).as_posix()}": _digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.relative_to(root).as_posix() not in RANDOM_FILES
        and ".git/" not in path.as_posix()
    }


def render_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """``{<run>/<relative path>: sha256}`` of every file the commands write."""
    found: dict[str, str] = {}
    for run, batteries in NEW_RUNS.items():
        parent = tmp_path / run
        parent.mkdir()
        _project.generate_project("snapapp", parent, with_batteries=batteries)
        found |= _tree(parent / "snapapp", run)
    app = next(probe_app(tmp_path / "deploy", monkeypatch))
    for module, service in (("probecat", "catalog"), ("probeord", "orders")):
        _deploy.deploy(_deploy.Project.find(app), [module], service)
    found |= _tree(app / "deploy", "deploy")
    for name in [m for m in sys.modules if m.startswith("probeapp")]:
        del sys.modules[name]
    return found


def render_new_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, subprocess_cli: bool
) -> dict[str, str]:
    """``sandloom new`` on a bare kernel (no stack package importable while it
    runs), then ``sandloom add stack`` where the package is installed: in this
    interpreter, or as the new app's own environment runs it (a child process
    of the ``sandloom`` console entry)."""
    found: dict[str, str] = {}
    for run, batteries in NEW_RUNS.items():
        parent = tmp_path / run
        parent.mkdir()
        _generate_bare(parent, batteries, monkeypatch)
        root = parent / "snapapp"
        assert _stack_files.pending(root), "a bare kernel leaves the stack's lines"
        if subprocess_cli:
            done = subprocess.run(  # this interpreter, fixed arguments
                [
                    sys.executable,
                    "-c",
                    "import sys, sandloom; sys.exit(sandloom.main())",
                    "add",
                    "--root",
                    str(root),
                    "stack",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert done.returncode == 0, done.stdout + done.stderr
        else:
            assert _add.main(["--root", str(root), "stack"]) == 0
        assert not _stack_files.pending(root)
        found |= _tree(root, run)
    return found


def _generate_bare(
    parent: Path, batteries: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(driver: str) -> Any:
        raise _stack.StackNotInstalled(driver)

    with monkeypatch.context() as bare:
        bare.setattr(_stack, "_discover", missing)
        _project.generate_project("snapapp", parent, with_batteries=batteries)


@pytest.mark.parametrize("subprocess_cli", [False, True], ids=["in-process", "child"])
def test_a_bare_kernel_completed_by_the_package_is_byte_identical(
    subprocess_cli: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The venv path of ``sandloom new``: the kernel without the stack's package
    writes the skeleton, the app's own environment (``sandloom add stack``)
    writes the rest: the files of the standard stack are the manifest's bytes
    on this path too."""
    expected = {
        name: digest
        for name, digest in json.loads(MANIFEST.read_text(encoding="utf-8")).items()
        if name.split("/")[0] in NEW_RUNS
    }
    actual = render_new_bare(tmp_path, monkeypatch, subprocess_cli=subprocess_cli)
    changed = sorted(name for name in expected if actual.get(name) != expected[name])
    added = sorted(set(actual) - set(expected))
    assert not changed and not added, (
        f"a bare kernel plus `sandloom add stack` differs from the standard stack's "
        f"scaffold: {changed} (missing or different); new files: {added}"
    )
    assert len(expected) > 10


def test_the_env_file_is_the_same_on_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` is the one file the manifest leaves out (a random
    ``JWT_SECRET``): with that line masked, both paths write the same bytes."""
    here, bare = tmp_path / "here", tmp_path / "bare"
    here.mkdir()
    bare.mkdir()
    _project.generate_project("snapapp", here)
    _generate_bare(bare, [], monkeypatch)
    assert _add.main(["--root", str(bare / "snapapp"), "stack"]) == 0

    def masked(root: Path) -> str:
        text = (root / "snapapp" / ".env").read_text(encoding="utf-8")
        return re.sub(r"JWT_SECRET=\S+", "JWT_SECRET=<secret>", text)

    assert masked(bare) == masked(here)
    assert "@@STACK" not in masked(bare)
    assert "Redis" in masked(bare)  # the label the package supplies, in the block


def test_the_standard_stack_scaffold_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual = render_all(tmp_path, monkeypatch)
    changed = sorted(name for name in expected if actual.get(name) != expected[name])
    added = sorted(set(actual) - set(expected))
    assert not changed and not added, (
        f"the scaffold of the standard stack changed: {changed} (files now "
        f"missing or different); new files: {added}"
    )
    assert len(expected) > 20  # a manifest with a handful of files proves nothing
