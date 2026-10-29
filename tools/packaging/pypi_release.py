"""Reviewed PyPI artifacts and GitHub Releases; PyPI uploads stay in the OIDC action."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

PACKAGES = ("sandloom", "sandloom-identity", "sandloom-tenancy", "sandloom-billing")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")


def plan_release(
    root: Path, package: str, event: str, ref: str, *, dry_run: bool, on_main: bool
) -> dict:
    versions = {
        name: tomllib.loads((root / "packages" / name / "pyproject.toml").read_text())[
            "project"
        ]["version"]
        for name in PACKAGES
    }
    if not all(VERSION.fullmatch(value) for value in versions.values()):
        raise ValueError(
            "Package versions must be public X.Y.Z or alpha/beta/rc versions"
        )
    if event == "push":
        matched = [
            name for name in PACKAGES if ref == f"refs/tags/{name}-v{versions[name]}"
        ]
        if len(matched) != 1:
            raise ValueError(
                "Release tag must exactly match a known package and its source version"
            )
        selected, publish = matched, True
    elif event == "workflow_dispatch":
        if package not in (*PACKAGES, "all"):
            raise ValueError("Unknown release package")
        selected, publish = (
            list(PACKAGES) if package == "all" else [package],
            not dry_run,
        )
        if publish and ref != "refs/heads/main":
            raise ValueError("Manual publication is allowed only from main")
    else:
        raise ValueError(
            "Only a manual run or a package release tag can request a release"
        )
    if publish and not on_main:
        raise ValueError("Publication requires a commit reachable from origin/main")
    if len(selected) > 1 and len({versions[name] for name in selected}) != 1:
        raise ValueError("A coordinated release requires matching package versions")
    return {"packages": selected, "versions": versions, "publish": publish}


def filenames(project: str, version: str) -> tuple[str, str]:
    if project not in PACKAGES or not VERSION.fullmatch(version):
        raise ValueError("Invalid artifact project/version")
    stem = project.replace("-", "_")
    return f"{stem}-{version}-py3-none-any.whl", f"{stem}-{version}.tar.gz"


def select_artifacts(plan: dict, artifacts: Path, output: Path) -> dict:
    versions = plan["versions"]
    if (
        set(versions) != set(PACKAGES)
        or not plan["packages"]
        or not set(plan["packages"]) <= set(PACKAGES)
    ):
        raise ValueError("Incomplete release plan")
    expected = {
        filename
        for project in PACKAGES
        for filename in filenames(project, versions[project])
    }
    checksums = json.loads((artifacts / "sha256.json").read_text())
    if set(checksums) != expected or {
        p.name for p in artifacts.iterdir()
    } != expected | {"sha256.json"}:
        raise ValueError(
            "Validated artifact set is incomplete or contains unexpected files"
        )
    for filename in expected:
        file = artifacts / filename
        if (
            file.is_symlink()
            or not file.is_file()
            or hashlib.sha256(file.read_bytes()).hexdigest() != checksums[filename]
        ):
            raise ValueError(f"Artifact checksum mismatch: {filename}")
    # Refuse an existing destination; never replace a previously reviewed set.
    output.mkdir(parents=True, exist_ok=False)
    dist = output / "dist"
    dist.mkdir()
    rows = []
    for project in plan["packages"]:
        version = versions[project]
        for filename in filenames(project, version):
            shutil.copyfile(artifacts / filename, dist / filename)
            rows.append(
                {
                    "project": project,
                    "version": version,
                    "filename": filename,
                    "sha256": checksums[filename],
                }
            )
    manifest = {"source_sha": os.environ.get("GITHUB_SHA", ""), "files": rows}
    (output / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text(checksum_text(rows))
    project = plan["packages"][0] if len(plan["packages"]) == 1 else None
    changelog = (
        Path.cwd() / "packages" / project / "CHANGELOG.md"
        if project
        else Path.cwd() / "CHANGELOG.md"
    )
    version = versions[plan["packages"][0]]
    (output / "release-notes.md").write_text(changelog_section(changelog, version))
    shutil.copyfile(Path(__file__), output / "pypi_release.py")
    return manifest


def fetch_release(project: str, version: str) -> dict | None:
    filenames(project, version)  # validate before constructing a network path
    connection = http.client.HTTPSConnection("pypi.org", timeout=20)
    try:
        connection.request(
            "GET",
            f"/pypi/{project}/{version}/json",
            headers={"Accept": "application/json"},
        )
        response = connection.getresponse()
        if response.status == 404:
            return None
        if response.status != 200:
            raise RuntimeError(
                f"PyPI metadata request failed: {project} HTTP {response.status}"
            )
        return json.loads(response.read())
    finally:
        connection.close()


def validate_manifest(manifest: dict) -> list[dict]:
    rows = manifest["files"]
    seen = set()
    for row in rows:
        name = row["filename"]
        if (
            name not in filenames(row["project"], row["version"])
            or name in seen
            or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
        ):
            raise ValueError("Invalid or duplicate manifest artifact")
        seen.add(name)
    if not rows:
        raise ValueError("Empty release manifest")
    for row in rows:
        if not set(filenames(row["project"], row["version"])) <= seen:
            raise ValueError("Every selected package needs both a wheel and sdist")
    return rows


def missing_files(manifest: dict, fetch=fetch_release) -> list[dict]:
    """Identical published files are reusable; differing immutable files fail."""
    rows = validate_manifest(manifest)
    missing, releases = [], {}
    for row in rows:
        key = (row["project"], row["version"])
        if key not in releases:
            releases[key] = fetch(*key)
        release = releases[key]
        existing = (
            {file["filename"]: file["digests"]["sha256"] for file in release["urls"]}
            if release
            else {}
        )
        expected = set(filenames(*key))
        if set(existing) - expected:
            raise ValueError(f"Unexpected published files for {key[0]} {key[1]}")
        if row["filename"] not in existing:
            missing.append(row)
        elif existing[row["filename"]] != row["sha256"]:
            raise ValueError(f"Published artifact differs: {row['filename']}")
    return missing


def prepare_upload(
    manifest: dict, dist: Path, upload: Path, fetch=fetch_release
) -> bool:
    rows = validate_local_artifacts(manifest, dist)
    missing = missing_files(manifest, fetch)
    upload.mkdir(parents=True, exist_ok=False)
    for row in missing:
        shutil.copyfile(dist / row["filename"], upload / row["filename"])
    return bool(missing)


def validate_local_artifacts(manifest: dict, dist: Path) -> list[dict]:
    rows = validate_manifest(manifest)
    if {p.name for p in dist.iterdir()} != {row["filename"] for row in rows}:
        raise ValueError("Distribution directory does not match the reviewed manifest")
    for row in rows:
        file = dist / row["filename"]
        if (
            file.is_symlink()
            or hashlib.sha256(file.read_bytes()).hexdigest() != row["sha256"]
        ):
            raise ValueError(f"Local artifact changed: {row['filename']}")
    return rows


def checksum_text(rows: list[dict]) -> str:
    return "".join(
        f"{row['sha256']}  {row['filename']}\n"
        for row in sorted(rows, key=lambda row: row["filename"])
    )


def changelog_section(path: Path, version: str) -> str:
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        path.read_text(),
        re.M | re.S,
    )
    if match is None or not match[1].strip():
        raise ValueError(f"Missing changelog notes for {version}: {path.name}")
    return match[1].strip() + "\n"


def github_release_spec(manifest: dict) -> dict:
    rows = validate_manifest(manifest)
    versions = {row["version"] for row in rows}
    projects = {row["project"] for row in rows}
    if len(versions) != 1 or not (projects == set(PACKAGES) or len(projects) == 1):
        raise ValueError("A GitHub release needs one package or a coordinated version")
    version = next(iter(versions))
    name = "Sandloom" if projects == set(PACKAGES) else next(iter(projects))
    prefix = "" if projects == set(PACKAGES) else f"{name}-"
    return {
        "tag": f"{prefix}v{version}",
        "title": f"{name} {version}",
        "prerelease": bool(re.search(r"(?:a|b|rc)\d+$", version)),
    }


def github_command(args: list[str]) -> str:
    return subprocess.run(
        ["gh", *args], check=True, text=True, capture_output=True
    ).stdout


def github_api(path: str) -> Any:
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", path],
        check=False,
        text=True,
        capture_output=True,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub API returned no valid JSON response") from exc
    if result.returncode:
        if data.get("status") == "404":
            return None
        raise RuntimeError(f"GitHub API request failed: {path}")
    return data


def find_github_release(api, base: str, tag: str) -> dict | None:
    published = api(f"{base}/releases/tags/{tag}")
    if published is not None:
        return published
    # A draft's pending tag may not exist yet, so REST lookup by tag returns
    # 404. Authenticated release listings include drafts; inspect their exact
    # pending tag rather than mistaking an existing draft for an absent release.
    page = 1
    while True:
        releases = api(f"{base}/releases?per_page=100&page={page}")
        matches = [r for r in releases if r["draft"] and r["tag_name"] == tag]
        if len(matches) > 1:
            raise ValueError("Multiple drafts have the same pending release tag")
        if matches:
            return matches[0]
        if len(releases) < 100:
            return None
        page += 1


def github_tag_source(api, base: str, tag: str) -> str | None:
    ref = api(f"{base}/git/ref/tags/{tag}")
    if ref is None:
        return None
    target = ref["object"]
    for _ in range(5):
        if target["type"] != "tag":
            break
        annotation = api(f"{base}/git/tags/{target['sha']}")
        if annotation is None:
            raise ValueError("Release tag annotation is missing")
        target = annotation["object"]
    if target.get("type") != "commit":
        raise ValueError("Release tag does not resolve to a commit")
    return target["sha"]


def publish_github_release(
    manifest_path: Path,
    dist: Path,
    *,
    repository: str,
    source_sha: str,
    api=github_api,
    command=github_command,
    fetch=fetch_release,
) -> None:
    """Publish a draft only after PyPI and every GitHub asset hash are verified.

    Reruns fill missing assets; an existing tag or asset is never overwritten.
    No checkout, build, package installation or PyPI credentials are needed.
    """
    manifest = json.loads(manifest_path.read_text())
    rows = validate_local_artifacts(manifest, dist)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not re.fullmatch(
        r"[a-f0-9]{40}", source_sha
    ):
        raise ValueError("Invalid GitHub repository/source")
    if manifest.get("source_sha") != source_sha:
        raise ValueError("Release source does not match the reviewed manifest")
    spec = github_release_spec(manifest)
    folder = manifest_path.parent
    checksums = folder / "SHA256SUMS"
    if checksums.read_text() != checksum_text(rows):
        raise ValueError("Release checksum file differs from the reviewed manifest")
    notes = folder / "release-notes.md"
    if not notes.read_text().strip():
        raise ValueError("Release notes are empty")
    verify_published(manifest, fetch=fetch)
    base = f"repos/{repository}"
    tag = spec["tag"]
    tag_source = github_tag_source(api, base, tag)
    if tag_source is not None and tag_source != source_sha:
        raise ValueError("Existing release tag points to another source commit")
    existing = find_github_release(api, base, tag)
    if (
        existing is not None
        and tag_source is None
        and (not existing["draft"] or existing.get("target_commitish") != source_sha)
    ):
        raise ValueError("Existing draft does not target the reviewed source commit")
    if existing is None:
        command(
            [
                "release",
                "create",
                tag,
                "--repo",
                repository,
                "--target",
                source_sha,
                "--title",
                spec["title"],
                "--notes-file",
                str(notes),
                "--draft",
                "--latest=false",
                f"--prerelease={str(spec['prerelease']).lower()}",
            ]
        )
    elif bool(existing["prerelease"]) != spec["prerelease"]:
        raise ValueError("Existing release has a different prerelease status")
    assets = [dist / row["filename"] for row in rows] + [checksums, manifest_path]
    wanted = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in assets}

    def check_assets() -> set[str]:
        current = find_github_release(api, base, tag)
        if current is None:
            raise RuntimeError("GitHub release was not created")
        found = set()
        for asset in current["assets"]:
            name = asset["name"]
            if name not in wanted or asset.get("digest") != f"sha256:{wanted[name]}":
                raise ValueError(f"Existing GitHub release asset differs: {name}")
            found.add(name)
        return found

    present = check_assets()
    missing = [str(p) for p in assets if p.name not in present]
    if missing:
        command(["release", "upload", tag, *missing, "--repo", repository])
    if check_assets() != set(wanted):
        raise RuntimeError("GitHub release assets are incomplete")
    if existing is None or existing["draft"]:
        command(["release", "edit", tag, "--repo", repository, "--draft=false"])
    final = find_github_release(api, base, tag)
    if (
        final is None
        or final["draft"]
        or github_tag_source(api, base, tag) != source_sha
    ):
        raise RuntimeError("GitHub release is not published at the reviewed source")
    print(
        f"Verified GitHub release: https://github.com/{repository}/releases/tag/{tag}"
    )


def verify_published(
    manifest: dict, *, timeout: float = 120, fetch=fetch_release, sleep=time.sleep
) -> None:
    deadline = time.monotonic() + timeout
    while missing_files(manifest, fetch):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Release is not fully visible on PyPI yet; verify before retrying"
            )
        sleep(3)
    print("All published filenames and SHA-256 digests match the reviewed artifacts")


def output(name: str, value: Any) -> None:
    if "GITHUB_OUTPUT" in os.environ:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"{name}={json.dumps(value, separators=(',', ':'))}\n")


def boolean_env(name: str, default: str) -> bool:
    value = os.environ.get(name, default)
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "select", "preflight", "verify", "github-release"]
    )
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--upload", type=Path)
    args = parser.parse_args()
    if args.action == "plan":
        plan = plan_release(
            Path.cwd(),
            os.environ.get("RELEASE_PACKAGE", "all"),
            os.environ["GITHUB_EVENT_NAME"],
            os.environ["GITHUB_REF"],
            dry_run=boolean_env("RELEASE_DRY_RUN", "true"),
            on_main=boolean_env("RELEASE_ON_MAIN", "false"),
        )
        output("manifest", plan)
        output("publish", plan["publish"])
        print(json.dumps(plan, indent=2))
    elif args.action == "select":
        select_artifacts(
            json.loads(os.environ["RELEASE_PLAN"]), args.artifacts, args.out
        )
    elif args.action == "preflight":
        output(
            "has_uploads",
            prepare_upload(
                json.loads(args.manifest.read_text()), args.dist, args.upload
            ),
        )
    elif args.action == "verify":
        verify_published(json.loads(args.manifest.read_text()))
    else:
        publish_github_release(
            args.manifest,
            args.dist,
            repository=os.environ["GITHUB_REPOSITORY"],
            source_sha=os.environ["GITHUB_SHA"],
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Release verification failed: {exc}")
        raise SystemExit(1) from exc
