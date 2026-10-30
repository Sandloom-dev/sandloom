"""Current release pins stay synchronized without rewriting historical evidence."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "release_sync_tested", ROOT / "tools/packaging/sync_release.py"
)
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def source(root: Path, version: str = "0.7.0a3") -> None:
    for name in sync.PACKAGES:
        p = root / "packages" / name / "pyproject.toml"
        p.parent.mkdir(parents=True)
        p.write_text(f'[project]\nversion = "{version}"\n')


def test_release_references_match_canonical_package():
    assert sync.changes(ROOT, sync.version(ROOT)) == {}


def test_discovers_stale_pin_in_a_new_document_and_preserves_other_versions(tmp_path):
    source(tmp_path)
    p = tmp_path / "docs" / "new-page.md"
    p.parent.mkdir()
    p.write_text(
        'uvx --from "sandloom[standard]==0.7.0a1" sandloom new demo\nuv 0.13.4\n'
    )
    assert sync.changes(tmp_path, sync.version(tmp_path))[p] == (
        'uvx --from "sandloom[standard]==0.7.0a3" sandloom new demo\nuv 0.13.4\n'
    )
    assert "0.7.0a1" in p.read_text(), "check must never write"


def test_prose_and_badges_update_but_audit_and_changelog_remain_historical(tmp_path):
    source(tmp_path)
    text = "<!-- sandloom-release --> Current alpha: 0.7.0a1\n"
    p = tmp_path / "README.md"
    p.write_text(text + "[PyPI](https://pypi.org/project/sandloom/0.7.0a1/)\n")
    audit = tmp_path / "docs/contributing/dependency-security-audit.md"
    audit.parent.mkdir(parents=True)
    audit.write_text("Verified sandloom==0.7.0a1\n")
    changelog = tmp_path / "packages/sandloom/CHANGELOG.md"
    changelog.write_text("## 0.7.0a1\nInstalled sandloom==0.7.0a1\n")
    pending = sync.changes(tmp_path, "0.7.0a3")
    assert set(pending) == {p}
    assert "0.7.0a1" not in pending[p]
    assert audit.read_text().endswith("0.7.0a1\n")
    assert "0.7.0a1" in changelog.read_text()


def test_independent_module_versions_are_preserved_without_coordinated_bump(tmp_path):
    source(tmp_path)
    module = tmp_path / "packages/sandloom-identity/pyproject.toml"
    module.write_text(
        '[project]\nversion = "0.7.1"\ndependencies = ["sandloom>=0.7.0a1,<0.8"]\n'
    )
    p = tmp_path / "README.md"
    p.write_text("sandloom-identity>=0.7.0a1,<0.8\n")
    pending = sync.changes(tmp_path, sync.version(tmp_path))
    assert module not in pending
    assert pending[p] == "sandloom-identity>=0.7.1,<0.8\n"


def test_coordinated_bump_updates_all_metadata_and_next_breaking_bound(tmp_path):
    source(tmp_path)
    p = tmp_path / "README.md"
    p.write_text("sandloom[standard]>=0.7.0a3,<0.8\n")
    pending = sync.changes(tmp_path, "0.8.0a1", coordinated=True)
    assert pending[p] == "sandloom[standard]>=0.8.0a1,<0.9\n"
    for name in sync.PACKAGES:
        assert (
            'version = "0.8.0a1"'
            in pending[tmp_path / "packages" / name / "pyproject.toml"]
        )
    assert sync.upper_bound("1.2.3") == "2.0"


@pytest.mark.parametrize("value", ["../0.7.0", "0.7", "0.7.0+local", "0.7.0\n"])
def test_invalid_release_version_cannot_generate_edits(tmp_path, value):
    with pytest.raises(ValueError, match="Invalid release version"):
        sync.changes(tmp_path, value)


@pytest.mark.parametrize("fault", [None, "stale-bytes", "wrong-version", "editable"])
def test_wheel_quickstart_rejects_wrong_artifacts_before_running_app(tmp_path, fault):
    import argparse
    import json

    spec = importlib.util.spec_from_file_location(
        "quickstart_wheels_tested", ROOT / "tools/smoke/quickstart_check.py"
    )
    assert spec and spec.loader
    quickstart = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quickstart)
    source(tmp_path)
    runtime = tmp_path / "src/sandloom/__init__.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("# reviewed runtime\n")
    installed = tmp_path / "installed/sandloom/__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_text(
        "# stale runtime\n" if fault == "stale-bytes" else runtime.read_text()
    )
    migration = tmp_path / "migrations/core/versions/001.py"
    migration.parent.mkdir(parents=True)
    migration.write_text("# reviewed migration\n")
    runner = quickstart.Runner(argparse.Namespace(repo=tmp_path, dist_dir=tmp_path))
    runner.batteries = []
    replies = iter(
        [
            json.dumps(
                {
                    "init": str(runtime if fault == "editable" else installed),
                    "files": {
                        "sandloom/__init__.py": str(installed),
                        "sandloom/core/migrations/core_chain/versions/001.py": str(
                            migration
                        ),
                    },
                }
            ),
            json.dumps(
                {"sandloom": "0.7.0a2" if fault == "wrong-version" else "0.7.0a3"}
            ),
        ]
    )
    runner.python = lambda _code: next(replies)
    if fault:
        with pytest.raises(quickstart.QuickstartError):
            runner.check_framework()
    else:
        runner.check_framework()
