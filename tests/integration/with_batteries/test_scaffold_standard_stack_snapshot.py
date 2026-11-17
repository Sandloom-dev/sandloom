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
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from sandloom.core.cli import project as _project
from sandloom.core.cli import scaffold_deploy as _deploy
from sandloom.core.tests.test_scaffold_deploy import probe_app

MANIFEST = Path(__file__).with_name("scaffold_standard_stack.sha256.json")
#: ``sandloom new`` runs of the snapshot: the lean app and the one with every battery.
NEW_RUNS: dict[str, list[str]] = {
    "new_lean": [],
    "new_batteries": ["identity", "tenancy", "billing", "admin"],
}
#: Left out of the hashes: written with a random secret.
RANDOM_FILES = frozenset({".env"})


def _digest(path: Path) -> str:
    data = path.read_bytes().replace(_project.FRAMEWORK_RANGE.encode(), b"<range>")
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
