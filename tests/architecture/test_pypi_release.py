"""Publishing uses reviewed refs/artifacts and fails closed on PyPI mismatches."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "pypi_release_tested", ROOT / "tools/packaging/pypi_release.py"
)
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def source(tmp_path):
    for project in release.PACKAGES:
        folder = tmp_path / "packages" / project
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_text('[project]\nversion = "0.7.0a2"\n')
    return tmp_path


@pytest.fixture
def plan(source):
    return release.plan_release(
        source,
        "all",
        "workflow_dispatch",
        "refs/heads/main",
        dry_run=False,
        on_main=True,
    )


@pytest.fixture
def candidate(tmp_path, plan):
    folder = tmp_path / "validated"
    folder.mkdir()
    hashes = {}
    for project in release.PACKAGES:
        for filename in release.filenames(project, "0.7.0a2"):
            payload = filename.encode()
            (folder / filename).write_bytes(payload)
            hashes[filename] = hashlib.sha256(payload).hexdigest()
    (folder / "sha256.json").write_text(json.dumps(hashes))
    out = tmp_path / "reviewed"
    manifest = release.select_artifacts(plan, folder, out)
    return out, manifest


@pytest.mark.parametrize("project", release.PACKAGES)
def test_tag_selects_only_its_matching_package(source, project):
    plan = release.plan_release(
        source,
        "all",
        "push",
        f"refs/tags/{project}-v0.7.0a2",
        dry_run=True,
        on_main=True,
    )
    assert plan["packages"] == [project]
    assert plan["publish"] is True


@pytest.mark.parametrize(
    "event,ref,on_main",
    [
        ("pull_request", "refs/pull/5/merge", True),
        ("workflow_dispatch", "refs/heads/feature", True),
        ("workflow_dispatch", "refs/heads/main", False),
        ("push", "refs/tags/sandloom-v0.7.0a1", True),
        ("push", "refs/tags/sandloom-v0.7.0a2", False),
        ("push", "refs/tags/unknown-v0.7.0a2", True),
    ],
)
def test_unreviewed_refs_and_mismatched_tags_cannot_publish(
    source, event, ref, on_main
):
    with pytest.raises(ValueError):
        release.plan_release(source, "all", event, ref, dry_run=False, on_main=on_main)


def test_dry_run_can_validate_a_feature_branch_without_publication(source):
    assert (
        release.plan_release(
            source,
            "all",
            "workflow_dispatch",
            "refs/heads/feature",
            dry_run=True,
            on_main=False,
        )["publish"]
        is False
    )


def test_malformed_boolean_does_not_enable_publication(monkeypatch):
    monkeypatch.setenv("RELEASE_DRY_RUN", "not-a-boolean")
    with pytest.raises(ValueError):
        release.boolean_env("RELEASE_DRY_RUN", "true")


def test_candidate_contains_exact_validated_files_and_the_verifier(candidate):
    folder, manifest = candidate
    assert len(manifest["files"]) == 8
    assert (folder / "pypi_release.py").read_bytes() == (
        ROOT / "tools/packaging/pypi_release.py"
    ).read_bytes()
    assert release.prepare_upload(
        manifest, folder / "dist", folder / "upload", fetch=lambda *_: None
    )
    assert len(list((folder / "upload").iterdir())) == 8


def published(manifest):
    return {
        project: {
            "urls": [
                {"filename": row["filename"], "digests": {"sha256": row["sha256"]}}
                for row in manifest["files"]
                if row["project"] == project
            ]
        }
        for project in release.PACKAGES
    }


def test_identical_published_files_are_verified_instead_of_uploaded_again(candidate):
    folder, manifest = candidate
    remote = published(manifest)
    assert not release.prepare_upload(
        manifest,
        folder / "dist",
        folder / "upload",
        fetch=lambda project, _version: remote[project],
    )
    assert not list((folder / "upload").iterdir())


def test_partial_upload_resumes_only_missing_matching_files(candidate):
    folder, manifest = candidate
    remote = published(manifest)
    missing = remote["sandloom"]["urls"].pop()
    assert release.prepare_upload(
        manifest,
        folder / "dist",
        folder / "upload",
        fetch=lambda project, _version: remote[project],
    )
    assert [file.name for file in (folder / "upload").iterdir()] == [
        missing["filename"]
    ]


def test_differing_immutable_pypi_file_blocks_publication(candidate):
    folder, manifest = candidate
    remote = published(manifest)
    remote["sandloom"]["urls"][0]["digests"]["sha256"] = "a" * 64
    with pytest.raises(ValueError, match="Published artifact differs"):
        release.prepare_upload(
            manifest,
            folder / "dist",
            folder / "upload",
            fetch=lambda project, _version: remote[project],
        )
    assert not (folder / "upload").exists()


def test_modified_local_file_is_not_uploaded(candidate):
    folder, manifest = candidate
    (folder / "dist" / manifest["files"][0]["filename"]).write_bytes(
        b"modified after validation"
    )
    with pytest.raises(ValueError, match="Local artifact changed"):
        release.prepare_upload(
            manifest, folder / "dist", folder / "upload", fetch=lambda *_: None
        )


def test_manifest_cannot_select_a_traversal_path(candidate):
    _folder, manifest = candidate
    manifest["files"][0]["filename"] = "../../token"
    with pytest.raises(ValueError):
        release.validate_manifest(manifest)


def test_pypi_visibility_delay_is_retried_without_reupload(candidate):
    _folder, manifest = candidate
    remote = published(manifest)
    attempts, sleeps = {}, []

    def fetch(project, _version):
        attempts[project] = attempts.get(project, 0) + 1
        return None if attempts[project] == 1 else remote[project]

    release.verify_published(manifest, timeout=10, fetch=fetch, sleep=sleeps.append)
    assert sleeps == [3]


def test_verification_fails_when_uploaded_files_remain_missing(candidate):
    _folder, manifest = candidate
    with pytest.raises(RuntimeError, match="not fully visible"):
        release.verify_published(manifest, timeout=0, fetch=lambda *_: None)


def test_only_publish_job_has_oidc_and_requires_environment_approval():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert "id-token" not in workflow["permissions"]
    assert [
        name
        for name, job in workflow["jobs"].items()
        if job.get("permissions", {}).get("id-token") == "write"
    ] == ["publish"]
    publish = workflow["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert set(publish["needs"]) == {"plan", "validate", "distributions", "prepare"}
    assert not any(
        step.get("uses", "").startswith("actions/checkout") for step in publish["steps"]
    )
    upload = next(
        step
        for step in publish["steps"]
        if "gh-action-pypi-publish" in step.get("uses", "")
    )
    assert upload["with"]["skip-existing"] is False
    assert upload["with"]["attestations"] is True
