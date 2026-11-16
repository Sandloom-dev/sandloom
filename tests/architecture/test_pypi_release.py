"""Publishing uses reviewed refs/artifacts and fails closed on PyPI mismatches."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
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
#: A coordinated release: a wheel and an sdist per distribution (the GitHub
#: release adds sha256.json and release-manifest.json).
ARCHIVES = 2 * len(release.PACKAGES)
NOTES = "# Changelog\n\n## [Unreleased]\n\n## [0.7.0a2] — 2026-01-01\n\n{body}"


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
def candidate(tmp_path, plan, monkeypatch):
    # The notes come from the CHANGELOG.md of the working directory: a copy of the
    # repository's, in a directory with no changelog.d/ (a fragment pending in the
    # checkout would make the release refuse; test_changelog_fragments covers it).
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copyfile(ROOT / "CHANGELOG.md", checkout / "CHANGELOG.md")
    monkeypatch.chdir(checkout)
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


TAGGED = tuple(p.name for p in release.release_packages.PACKAGES if p.tag_release)
UNTAGGED = tuple(n for n in release.PACKAGES if n not in TAGGED)


@pytest.mark.parametrize("project", TAGGED)
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


@pytest.mark.parametrize("project", UNTAGGED)
def test_a_tag_of_a_package_the_environment_does_not_allow_is_refused(source, project):
    with pytest.raises(ValueError, match="not released by tag"):
        release.plan_release(
            source,
            "all",
            "push",
            f"refs/tags/{project}-v0.7.0a2",
            dry_run=True,
            on_main=True,
        )


def test_the_tag_allowed_packages_are_the_environment_allowed_ones():
    assert set(TAGGED) == {
        "sandloom",
        "sandloom-identity",
        "sandloom-tenancy",
        "sandloom-billing",
    }
    assert UNTAGGED, "a list with no manual-only package hides the refusal path"


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


def _kernel_requires_a_sibling(source, requirement="sandloom-redis>=0.7.0a2,<0.8"):
    (source / "packages" / "sandloom" / "pyproject.toml").write_text(
        '[project]\nversion = "0.7.0a2"\n'
        f'dependencies = ["pydantic>=2"]\n[project.optional-dependencies]\nredis = ["{requirement}"]\n'
    )


def _tag_plan(source, **kwargs):
    return release.plan_release(
        source,
        "all",
        "push",
        "refs/tags/sandloom-v0.7.0a2",
        dry_run=True,
        on_main=True,
        **kwargs,
    )


def test_a_release_refuses_a_sibling_that_no_index_has(source):
    _kernel_requires_a_sibling(source)
    asked = []

    def on_pypi(project, version):
        asked.append((project, version))
        return False

    with pytest.raises(ValueError, match="sandloom-redis") as caught:
        _tag_plan(source, on_pypi=on_pypi)
    assert "release sandloom-redis first" in str(caught.value)
    assert asked == [("sandloom-redis", "0.7.0a2")]


def test_a_sibling_already_on_pypi_does_not_block_the_release(source):
    _kernel_requires_a_sibling(source)
    plan = _tag_plan(source, on_pypi=lambda project, version: True)
    assert plan["packages"] == ["sandloom"]


def test_a_sibling_in_the_same_plan_is_not_looked_up(source):
    _kernel_requires_a_sibling(source)

    def never(project, version):
        raise AssertionError("PyPI must not be asked for a sibling in the plan")

    plan = release.plan_release(
        source,
        "all",
        "workflow_dispatch",
        "refs/heads/main",
        dry_run=False,
        on_main=True,
        on_pypi=never,
    )
    assert "sandloom-redis" in plan["packages"]


def test_a_dry_run_does_not_ask_pypi(source):
    _kernel_requires_a_sibling(source)

    def never(project, version):
        raise AssertionError("a dry run publishes nothing and asks no index")

    plan = release.plan_release(
        source,
        "sandloom",
        "workflow_dispatch",
        "refs/heads/feature",
        dry_run=True,
        on_main=False,
        on_pypi=never,
    )
    assert plan["publish"] is False


def test_the_default_lookup_is_pypi_itself(source, monkeypatch):
    _kernel_requires_a_sibling(source)
    monkeypatch.setattr(release, "fetch_release", lambda project, version: None)
    with pytest.raises(ValueError, match="sandloom-redis"):
        _tag_plan(source)
    monkeypatch.setattr(release, "fetch_release", lambda project, version: {"urls": []})
    assert _tag_plan(source)["packages"] == ["sandloom"]


def test_a_requirement_on_a_name_outside_the_list_is_not_a_sibling(source):
    _kernel_requires_a_sibling(source, requirement="sandloom-unlisted>=1")
    assert _tag_plan(source, on_pypi=lambda *_: False)["packages"] == ["sandloom"]


def test_malformed_boolean_does_not_enable_publication(monkeypatch):
    monkeypatch.setenv("RELEASE_DRY_RUN", "not-a-boolean")
    with pytest.raises(ValueError):
        release.boolean_env("RELEASE_DRY_RUN", "true")


def test_candidate_contains_exact_validated_files_and_the_verifier(candidate):
    folder, manifest = candidate
    assert len(manifest["files"]) == ARCHIVES
    assert (folder / "pypi_release.py").read_bytes() == (
        ROOT / "tools/packaging/pypi_release.py"
    ).read_bytes()
    assert release.prepare_upload(
        manifest, folder / "dist", folder / "upload", fetch=lambda *_: None
    )
    assert len(list((folder / "upload").iterdir())) == ARCHIVES


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


def _plan_all(source):
    return release.plan_release(
        source,
        "all",
        "workflow_dispatch",
        "refs/heads/main",
        dry_run=True,
        on_main=False,
    )


def _notes_of_size(size):
    """A section body whose ``changelog_section`` is exactly ``size`` characters."""
    line = "- " + "x" * 98 + "\n"
    body = line * (size // len(line))
    return body + "-" * (size - len(body) - 1) + "\n"


def test_notes_at_the_github_limit_are_accepted_and_over_it_refused_before_any_build(
    source,
):
    (source / "CHANGELOG.md").write_text(
        NOTES.format(body=_notes_of_size(release.NOTES_LIMIT))
    )
    assert _plan_all(source)["packages"] == list(release.PACKAGES)
    (source / "CHANGELOG.md").write_text(
        NOTES.format(body=_notes_of_size(release.NOTES_LIMIT + 1))
    )
    with pytest.raises(ValueError, match=r"125,001 characters, over the 125,000"):
        _plan_all(source)


def _plan_redis(source):
    return release.plan_release(
        source,
        "sandloom-redis",
        "workflow_dispatch",
        "refs/heads/main",
        dry_run=True,
        on_main=False,
    )


def test_a_single_package_is_measured_by_its_own_changelog(source):
    huge = NOTES.format(body=_notes_of_size(release.NOTES_LIMIT + 1))
    (source / "CHANGELOG.md").write_text(huge)
    changelog = source / "packages/sandloom-redis/CHANGELOG.md"
    changelog.write_text(NOTES.format(body="- A change.\n"))
    assert _plan_redis(source)["packages"] == ["sandloom-redis"]
    changelog.write_text(huge)
    with pytest.raises(ValueError, match="over the 125,000"):
        _plan_redis(source)


def test_the_plan_leaves_a_missing_section_to_the_artifact_selection(source):
    (source / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    assert _plan_all(source)["packages"] == list(release.PACKAGES)
    with pytest.raises(ValueError, match="Missing changelog notes for 0.7.0a2"):
        release.release_notes(source, list(release.PACKAGES), "0.7.0a2")


def test_the_notes_of_a_release_are_refused_over_the_limit_when_selected(source):
    (source / "CHANGELOG.md").write_text(
        NOTES.format(body=_notes_of_size(release.NOTES_LIMIT))
    )
    notes = release.release_notes(source, list(release.PACKAGES), "0.7.0a2")
    assert len(notes) == release.NOTES_LIMIT
    (source / "CHANGELOG.md").write_text(
        NOTES.format(body=_notes_of_size(release.NOTES_LIMIT + 1))
    )
    with pytest.raises(ValueError, match="over the 125,000"):
        release.release_notes(source, list(release.PACKAGES), "0.7.0a2")


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
    assert len(state["release"]["assets"]) == ARCHIVES + 2
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
    assert len(state["release"]["assets"]) == ARCHIVES + 2
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
