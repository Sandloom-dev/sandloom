"""Public snapshots are an explicit reviewed boundary, independent of Git hooks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.packaging import export_repository as exporter


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "tools/packaging").mkdir(parents=True)
    (root / "README.md").write_text("Reviewed source\n")
    (root / "pyproject.toml").write_text('[project]\nlicense = "Apache-2.0"\n')
    (root / exporter.POLICY).write_text(
        json.dumps(
            {
                "schema": 1,
                "include": [
                    {
                        "reason": "fixture source",
                        "paths": [
                            "README.md",
                            "pyproject.toml",
                            exporter.POLICY,
                        ],
                    }
                ],
                "exclude_files": {},
                "exclude_trees": {},
            }
        )
    )
    subprocess.run(
        ["git", "init", "--quiet", str(root)],
        check=True,
        env=exporter.git_environment(),
    )
    return root


def track(root: Path) -> None:
    """Stage the working tree: only Git-tracked files are export candidates."""
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        check=True,
        env=exporter.git_environment(),
    )


def inspect(root: Path) -> dict:
    track(root)
    return exporter.inspect_source(root)


def approve(root: Path, name: str) -> None:
    path = root / exporter.POLICY
    data = json.loads(path.read_text())
    data["include"][0]["paths"].append(name)
    path.write_text(json.dumps(data))


def test_repository_selection_is_complete_and_safe() -> None:
    report = exporter.inspect_source()
    paths = {item["path"] for item in report["public_files"]}
    assert {"LICENSE", "NOTICE", "THIRD_PARTY.md", exporter.POLICY} <= paths
    assert "tests/fixtures/specs/layout-path-map.json" in paths
    assert "benchmarks/tests/test_s5_secure_read.py" in paths
    assert not any(
        p.startswith(("private/", "benchmarks/native/", "benchmarks/deps_cg/"))
        for p in paths
    )
    assert "RELEASE_READINESS.md" not in paths
    assert "src/sandloom_identity/examples.py.bak" not in paths


def test_unreviewed_file_is_refused_before_any_destination(
    source: Path, tmp_path: Path
) -> None:
    (source / "tools/unreviewed.py").write_text("value = 1\n")
    with pytest.raises(exporter.ExportError, match="Unreviewed source paths"):
        inspect(source)
    assert not (tmp_path / "destination").exists()


def test_untracked_file_is_never_a_candidate_or_exported(
    source: Path, tmp_path: Path
) -> None:
    # ci_local writes temporary workflows such as this one into the checkout:
    # an untracked file is neither reviewed nor exported, wherever it lies.
    track(source)
    (source / ".github/workflows").mkdir(parents=True)
    (source / ".github/workflows/.ci-local-docs.yml").write_text("jobs: {}\n")
    (source / "tools/scratch.py").write_text("value = 1\n")
    report = exporter.inspect_source(source)
    paths = {item["path"] for item in report["public_files"]}
    assert paths == {"README.md", "pyproject.toml", exporter.POLICY}
    destination = tmp_path / "snapshot"
    exporter.export_source(destination, report, source)
    assert not (destination / ".github").exists()
    assert not (destination / "tools/scratch.py").exists()


def test_approved_but_untracked_file_is_refused(source: Path) -> None:
    track(source)
    (source / "tools/new.py").write_text("value = 1\n")
    approve(source, "tools/new.py")
    subprocess.run(
        ["git", "-C", str(source), "add", exporter.POLICY],
        check=True,
        env=exporter.git_environment(),
    )
    with pytest.raises(exporter.ExportError, match="not tracked by Git"):
        exporter.inspect_source(source)


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "tools/../escape", "tools\\escape", "./README.md"],
)
def test_noncanonical_approved_paths_are_refused(source: Path, name: str) -> None:
    approve(source, name)
    with pytest.raises(exporter.ExportError, match="Non-canonical"):
        inspect(source)


@pytest.mark.parametrize(
    "name",
    [
        "private/notes.md",
        ".env",
        "tools/.env.prod",
        "tools/state.key",
        "tools/state.pem",
        "tools/file.bak",
        "tools/file.orig",
        "tools/file.rej",
        "tools/file~",
        "tools/node_modules/file.js",
        "tools/build/output.txt",
        "tools/.coverage.foo",
    ],
)
def test_forbidden_paths_cannot_be_approved(source: Path, name: str) -> None:
    approve(source, name)
    with pytest.raises(exporter.ExportError, match="Forbidden"):
        inspect(source)


@pytest.mark.parametrize("external", [False, True])
def test_symlink_file_is_refused_even_when_target_is_internal(
    source: Path, tmp_path: Path, external: bool
) -> None:
    target = tmp_path / "external" if external else source / "README.md"
    if external:
        target.write_text("outside\n")
    (source / "tools/link").symlink_to(target)
    approve(source, "tools/link")
    with pytest.raises(exporter.ExportError, match="Symlink"):
        inspect(source)


def test_symlink_ancestor_is_refused(source: Path, tmp_path: Path) -> None:
    # A tracked file whose directory is later replaced by a link outside.
    (source / "src").mkdir()
    (source / "src/file.py").write_text("value = 1\n")
    approve(source, "src/file.py")
    track(source)
    outside = tmp_path / "outside"
    (source / "src").rename(outside)
    (source / "src").symlink_to(outside, target_is_directory=True)
    with pytest.raises(exporter.ExportError, match="Symlink"):
        exporter.inspect_source(source)


def test_missing_source_is_refused(source: Path) -> None:
    track(source)
    (source / "README.md").unlink()
    with pytest.raises(exporter.ExportError, match="missing"):
        exporter.inspect_source(source)


def test_credentials_are_refused_without_echoing_values(source: Path) -> None:
    value = "ghp_" + "a" * 36
    (source / "README.md").write_text(value)
    with pytest.raises(exporter.ExportError, match="values withheld") as failure:
        inspect(source)
    assert value not in str(failure.value)


def test_duplicate_and_conflicting_policy_is_refused(source: Path) -> None:
    approve(source, "README.md")
    with pytest.raises(exporter.ExportError, match="repeated"):
        inspect(source)
    path = source / exporter.POLICY
    data = json.loads(path.read_text())
    data["include"][0]["paths"].pop()
    data["exclude_files"]["README.md"] = "keep local"
    path.write_text(json.dumps(data))
    with pytest.raises(exporter.ExportError, match="Conflicting"):
        inspect(source)


def test_source_change_after_review_is_refused(source: Path, tmp_path: Path) -> None:
    report = inspect(source)
    (source / "README.md").write_text("new bytes\n")
    destination = tmp_path / "destination"
    with pytest.raises(exporter.ExportError, match="changed since inspection"):
        exporter.export_source(destination, report, source)
    assert not destination.exists()


def test_parent_link_inserted_after_review_is_refused(
    source: Path, tmp_path: Path
) -> None:
    (source / "src").mkdir()
    (source / "src/one.py").write_text("original\n")
    approve(source, "src/one.py")
    report = inspect(source)
    (source / "src").rename(tmp_path / "other")
    (source / "src").symlink_to(tmp_path / "other", target_is_directory=True)
    with pytest.raises(exporter.ExportError, match="Symlink"):
        exporter.export_source(tmp_path / "destination", report, source)


def test_no_existing_or_nested_destination_is_modified(
    source: Path, tmp_path: Path
) -> None:
    report = inspect(source)
    with pytest.raises(exporter.ExportError):
        exporter.export_source(source / "nested", report, source)
    other = tmp_path / "existing"
    other.mkdir()
    (other / "keep").write_text("untouched")
    with pytest.raises(exporter.ExportError):
        exporter.export_source(other, report, source)
    assert (other / "keep").read_text() == "untouched"


def test_failed_copy_removes_only_its_own_destination(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = inspect(source)
    destination = tmp_path / "failed"

    def fail(*args: object) -> None:
        raise OSError("injected copy failure")

    monkeypatch.setattr(exporter.shutil, "copy2", fail)
    with pytest.raises(OSError, match="injected"):
        exporter.export_source(destination, report, source)
    assert not destination.exists()
    assert (source / "README.md").read_text() == "Reviewed source\n"


def test_fresh_history_and_manifest_ignore_inherited_git_environment(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = exporter.git_environment()
    track(source)
    git_dir = source / ".git"
    config_before = (git_dir / "config").read_bytes()
    monkeypatch.setenv("GIT_DIR", str(git_dir))
    monkeypatch.setenv("GIT_WORK_TREE", str(source))
    monkeypatch.setenv("GIT_INDEX_FILE", str(git_dir / "foreign-index"))
    report = exporter.inspect_source(source)
    destination = tmp_path / "snapshot"
    exporter.export_source(destination, report, source)
    assert (git_dir / "config").read_bytes() == config_before
    assert not (git_dir / "foreign-index").exists()
    assert (destination / ".git").is_dir()
    receipt = json.loads((destination / "SOURCE_EXPORT.json").read_text())
    assert receipt["license"] == "Apache-2.0"
    assert receipt["public_files"] == report["public_files"]
    assert (
        subprocess.check_output(["git", "-C", str(destination), "remote"], env=clean)
        == b""
    )
    assert set(
        subprocess.check_output(["git", "-C", str(destination), "ls-files"], env=clean)
        .decode()
        .splitlines()
    ) == {item["path"] for item in report["public_files"]}
    assert (destination / ".gitignore").exists() is False
    assert (source / "README.md").read_bytes() == (
        destination / "README.md"
    ).read_bytes()


@pytest.mark.parametrize("target", ["../../src", "../../outside", "/absolute"])
def test_packaging_link_target_must_match_review(
    source: Path, tmp_path: Path, target: str
) -> None:
    (source / "src").mkdir()
    (source / "src/file.py").write_text("value = 1\n")
    (source / "packages/editable").mkdir(parents=True)
    link = source / "packages/editable/sandloom"
    link.symlink_to(target, target_is_directory=True)
    approve(source, "src/file.py")
    approve(source, "packages/editable/sandloom")
    policy = source / exporter.POLICY
    raw = json.loads(policy.read_text())
    raw["symlinks"] = {"packages/editable/sandloom": "../../src"}
    policy.write_text(json.dumps(raw))
    if target != "../../src":
        with pytest.raises(exporter.ExportError, match="Symlink"):
            inspect(source)
        return
    report = inspect(source)
    destination = tmp_path / "linked-snapshot"
    exporter.export_source(destination, report, source)
    exported = destination / "packages/editable/sandloom"
    assert exported.readlink() == Path(target)
    assert (exported / "file.py").read_text() == "value = 1\n"
    assert exported.resolve().is_relative_to(destination)


def test_dangling_destination_link_is_refused(source: Path, tmp_path: Path) -> None:
    destination = tmp_path / "link"
    target = tmp_path / "must-not-create"
    destination.symlink_to(target)
    with pytest.raises(exporter.ExportError, match="symlink"):
        exporter.export_source(destination, inspect(source), source)
    assert not target.exists()


def test_dx_classification_and_triggers_follow_the_exported_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re
    import runpy
    import sys

    monkeypatch.setattr(sys, "path", list(sys.path))
    namespace = runpy.run_path(str(exporter.ROOT / "benchmarks/dx/run_eval.py"))

    def kind(path: str) -> str:
        return next(
            label
            for pattern, label in namespace["OUTSIDE_KINDS"]
            if re.search(pattern, path)
        )

    assert kind("pyproject.toml") == "wiring"
    assert kind(".env.example") == "wiring"
    assert kind("architecture_rules.yml") == "framework"
    assert kind("public_api_baseline.json") == "framework"
    assert kind("tools/checks/check_architecture.py") == "framework"
    assert kind("src/sandloom_tenancy/api/organizations.py") == "other-module"
    for path in ("architecture_rules.yml", "public_api_baseline.json"):
        assert any(pattern.search(path) for pattern in namespace["TRIGGERS"])


def test_public_ignore_transformation_preserves_source_and_records_both_hashes(
    source: Path, tmp_path: Path
) -> None:
    import hashlib

    original = b"build/\n"
    (source / ".gitignore").write_bytes(original)
    approve(source, ".gitignore")
    report = inspect(source)
    target = tmp_path / "with-ignore"
    exporter.export_source(target, report, source)
    assert (source / ".gitignore").read_bytes() == original
    assert "private/" in (target / ".gitignore").read_text().splitlines()
    receipt = json.loads((target / "SOURCE_EXPORT.json").read_text())
    row = next(item for item in receipt["public_files"] if item["path"] == ".gitignore")
    assert row["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert (
        row["sha256"]
        == hashlib.sha256((target / ".gitignore").read_bytes()).hexdigest()
    )


def test_snapshot_receipt_is_explicitly_excluded_on_reinspection(
    source: Path, tmp_path: Path
) -> None:
    policy = source / exporter.POLICY
    raw = json.loads(policy.read_text())
    raw["exclude_files"]["SOURCE_EXPORT.json"] = "Generated receipt, not source"
    policy.write_text(json.dumps(raw))
    destination = tmp_path / "self-checking-snapshot"
    exporter.export_source(destination, inspect(source), source)
    # Inspect the actual exporter output, with no test-side staging step.
    inspected = exporter.inspect_source(destination)
    assert "SOURCE_EXPORT.json" not in {
        item["path"] for item in inspected["public_files"]
    }
