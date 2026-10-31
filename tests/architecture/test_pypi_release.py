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


def test_coordinated_release_refuses_mixed_versions_before_upload(source):
    (source / "packages/sandloom-identity/pyproject.toml").write_text(
        '[project]\nversion = "0.7.0a3"\n'
    )
    with pytest.raises(ValueError, match="matching package versions"):
        release.plan_release(
            source,
            "all",
            "workflow_dispatch",
            "refs/heads/main",
            dry_run=False,
            on_main=True,
        )


def test_release_notes_and_checksums_are_prepared_without_publishing(candidate):
    folder, manifest = candidate
    assert "### Publishing" in (folder / "release-notes.md").read_text()
    assert (folder / "SHA256SUMS").read_text() == release.checksum_text(
        manifest["files"]
    )
    assert release.github_release_spec(manifest) == {
        "tag": "v0.7.0a2",
        "title": "Sandloom 0.7.0a2",
        "prerelease": True,
    }
    manifest["files"] = manifest["files"][:2]
    assert release.github_release_spec(manifest)["tag"] == "sandloom-v0.7.0a2"
    assert not release.github_release_spec(
        {
            "files": [
                {
                    **row,
                    "version": "0.7.0",
                    "filename": row["filename"].replace("0.7.0a2", "0.7.0"),
                }
                for row in manifest["files"]
            ]
        }
    )["prerelease"]


@pytest.fixture
def github_candidate(candidate):
    folder, manifest = candidate
    sha = "a" * 40
    manifest["source_sha"] = sha
    (folder / "release-manifest.json").write_text(json.dumps(manifest))
    calls = []
    state = {"ref": None, "release": None}

    def api(path):
        if "/git/ref/" in path:
            return state["ref"]
        if "/releases?" in path:
            return [] if state["release"] is None else [state["release"]]
        # REST's tag endpoint cannot resolve an unpublished draft's pending tag.
        if state["release"] and state["release"]["draft"]:
            return None
        return state["release"]

    def command(args):
        calls.append(args)
        if args[1] == "create":
            state["release"] = {
                "tag_name": "v0.7.0a2",
                "draft": True,
                "prerelease": True,
                "assets": [],
                "target_commitish": sha,
            }
        elif args[1] == "upload":
            for name in args[3 : args.index("--repo")]:
                path = Path(name)
                state["release"]["assets"].append(
                    {
                        "name": path.name,
                        "digest": "sha256:"
                        + hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        elif args[1] == "edit":
            state["release"]["draft"] = False
            state["ref"] = {"object": {"type": "commit", "sha": sha}}
        else:
            raise AssertionError(args)

    def publish():
        release.publish_github_release(
            folder / "release-manifest.json",
            folder / "dist",
            repository="Sandloom-dev/sandloom",
            source_sha=sha,
            api=api,
            command=command,
            fetch=lambda project, _version: published(manifest)[project],
        )

    return folder, manifest, state, calls, publish


def test_github_release_publishes_verified_assets_last_and_reruns_are_noops(
    github_candidate,
):
    _folder, _manifest, state, calls, publish = github_candidate
    publish()
    assert [call[1] for call in calls] == ["create", "upload", "edit"]
    assert "--draft" in calls[0]
    assert "--prerelease=true" in calls[0]
    assert len(state["release"]["assets"]) == 10
    assert state["release"]["draft"] is False
    assert not any("--clobber" in call for call in calls)
    calls.clear()
    publish()
    assert calls == []


def test_github_release_resumes_a_partial_draft_without_overwriting(github_candidate):
    _folder, _manifest, state, calls, publish = github_candidate
    publish()
    state["release"]["draft"] = True
    state["release"]["assets"].pop()
    calls.clear()
    publish()
    assert [call[1] for call in calls] == ["upload", "edit"]
    assert len(calls[0][3 : calls[0].index("--repo")]) == 1


def test_github_release_waits_for_a_new_draft_without_repeating_writes(
    github_candidate, monkeypatch
):
    """The create response can precede the draft becoming visible in REST reads."""
    _folder, _manifest, state, calls, publish = github_candidate
    original = release.find_github_release
    hidden = 2
    sleeps = []

    def delayed(api, base, tag):
        nonlocal hidden
        if state["release"] is not None and hidden:
            hidden -= 1
            return None
        return original(api, base, tag)

    monkeypatch.setattr(release, "find_github_release", delayed)
    monkeypatch.setattr(release.time, "sleep", sleeps.append)
    publish()
    assert [call[1] for call in calls] == ["create", "upload", "edit"]
    assert sleeps == [2, 2]
    assert len(state["release"]["assets"]) == 10
    assert state["release"]["draft"] is False


def test_github_release_visibility_wait_is_bounded_and_never_recreates(
    github_candidate, monkeypatch
):
    """A permanently invisible draft fails closed before any upload or publish."""
    _folder, _manifest, _state, calls, publish = github_candidate
    sleeps = []
    monkeypatch.setattr(release, "find_github_release", lambda *args: None)
    monkeypatch.setattr(release.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="not visible after 6 reads"):
        publish()
    assert [call[1] for call in calls] == ["create"]
    assert sleeps == [2] * 5


@pytest.mark.parametrize("conflict", ["tag", "asset", "source", "checksums", "draft"])
def test_github_release_refuses_conflicts_without_writing(github_candidate, conflict):
    folder, manifest, state, calls, publish = github_candidate
    publish()
    calls.clear()
    if conflict == "tag":
        state["ref"]["object"]["sha"] = "b" * 40
    elif conflict == "asset":
        state["release"]["assets"][0]["digest"] = "sha256:" + "b" * 64
    elif conflict == "source":
        manifest["source_sha"] = "b" * 40
        (folder / "release-manifest.json").write_text(json.dumps(manifest))
    elif conflict == "checksums":
        (folder / "SHA256SUMS").write_text("tampered")
    else:
        state["ref"] = None
        state["release"].update(draft=True, target_commitish="b" * 40)
    with pytest.raises(ValueError):
        publish()
    assert calls == []


def test_github_release_only_runs_after_pypi_success_and_has_separate_permissions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    job = workflow["jobs"]["github-release"]
    assert set(job["needs"]) == {"plan", "publish"}
    assert job["if"] == "${{ needs.plan.outputs.publish == 'true' }}"
    assert job["permissions"] == {"contents": "write"}
    assert not any("checkout" in s.get("uses", "") for s in job["steps"])
    assert all(
        "contents" not in j.get("permissions", {})
        or j["permissions"]["contents"] != "write"
        for name, j in workflow["jobs"].items()
        if name != "github-release"
    )


def test_draft_lookup_follows_pages_and_never_uses_an_unrelated_release():
    draft = {"tag_name": "v0.7.0a2", "draft": True}
    seen = []

    def api(path):
        seen.append(path)
        if "/tags/" in path:
            return None
        if path.endswith("page=1"):
            return [{"tag_name": "v0.6.0", "draft": False}] * 100
        return [draft]

    assert (
        release.find_github_release(api, "repos/Sandloom-dev/sandloom", "v0.7.0a2")
        is draft
    )
    assert seen[-1].endswith("page=2")


@pytest.mark.parametrize("status", [403, 404, 502])
def test_github_api_only_treats_404_as_an_absent_release(monkeypatch, status):
    from types import SimpleNamespace

    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=json.dumps({"status": str(status)})
        ),
    )
    if status == 404:
        assert (
            release.github_api("repos/Sandloom-dev/sandloom/releases/tags/missing")
            is None
        )
    else:
        with pytest.raises(RuntimeError, match="GitHub API request failed"):
            release.github_api("repos/Sandloom-dev/sandloom/releases/tags/missing")
