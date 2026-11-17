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
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

# The reviewed directory carries release_packages.py and packages.json beside
# this file (see select_artifacts), so the privileged job needs no checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from tools.packaging import release_packages
else:
    import release_packages

#: A decoded JSON object: a plan, a manifest, a PyPI or GitHub response.
JSON = dict[str, Any]
#: Reads one PyPI release's metadata, ``None`` when PyPI has no such version.
Fetch = Callable[[str, str], JSON | None]
#: Reads one GitHub REST path, ``None`` on 404.
Api = Callable[[str], Any]

#: The distributions, from the one list (``packages.json``).
PACKAGES = release_packages.NAMES
VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
#: GitHub refuses a release body over 125,000 characters.
NOTES_LIMIT = 125_000


REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def sibling_requirements(project: JSON, package: str) -> set[str]:
    """The distributions of ``packages.json`` that ``package`` requires, from the
    ``Requires-Dist`` lines its pyproject declares (dependencies and extras)."""
    lines = list(project.get("dependencies", ()))
    for extra in project.get("optional-dependencies", {}).values():
        lines.extend(extra)
    names = set()
    for line in lines:
        found = REQUIREMENT_NAME.match(line)
        if found:
            names.add(re.sub(r"[-_.]+", "-", found[1]).lower())
    return {name for name in names if name in PACKAGES and name != package}


def refuse_unpublished_siblings(
    projects: dict[str, JSON],
    versions: dict[str, str],
    selected: list[str],
    on_pypi: Callable[[str, str], bool],
) -> None:
    """A published distribution must not require a sibling that no index has.

    The sibling must be in the same plan or already on PyPI at its source
    version; otherwise an install of the published package fails (an extra such
    as ``sandloom[redis]`` names ``sandloom-redis``)."""
    for name in selected:
        for sibling in sorted(sibling_requirements(projects[name], name)):
            if sibling in selected or on_pypi(sibling, versions[sibling]):
                continue
            raise ValueError(
                f"{name} requires {sibling}, which is neither in this release "
                f"nor on PyPI at {versions[sibling]}: release {sibling} first "
                f"(the Release workflow with package {sibling}, or package all "
                "for a coordinated release)"
            )


def pypi_has(project: str, version: str) -> bool:
    return fetch_release(project, version) is not None


def refuse_unfolded_fragments(root: Path, packages: list[str]) -> None:
    """Refuse a release whose notes miss changelog fragments (changelog.d/).

    A single package's notes are its own changelog; a coordinated release also
    carries the root ``CHANGELOG.md``. Imported here: the reviewed directory
    carries only the three files ``select_artifacts`` copies at the end.
    """
    if TYPE_CHECKING:
        from tools.packaging import changelog_fragments
    else:
        import changelog_fragments

    scopes: list[str | None] = list(packages)
    if len(packages) > 1:
        scopes.append(None)
    changelog_fragments.refuse_unfolded(root, scopes)


def select_release_packages(
    package: str, event: str, ref: str, versions: dict[str, str], *, dry_run: bool
) -> tuple[list[str], bool]:
    """The packages the event releases and whether it publishes them."""
    if event == "push":
        matched = [
            name for name in PACKAGES if ref == f"refs/tags/{name}-v{versions[name]}"
        ]
        if len(matched) != 1:
            raise ValueError(
                "Release tag must exactly match a known package and its source version"
            )
        if not release_packages.by_name(matched[0]).tag_release:
            raise ValueError(
                f"{matched[0]} is not released by tag (tag_release is false in "
                "packages.json); run the Release workflow by hand"
            )
        return matched, True
    if event == "workflow_dispatch":
        if package not in (*PACKAGES, "all"):
            raise ValueError("Unknown release package")
        publish = not dry_run
        if publish and ref != "refs/heads/main":
            raise ValueError("Manual publication is allowed only from main")
        return (list(PACKAGES) if package == "all" else [package]), publish
    raise ValueError("Only a manual run or a package release tag can request a release")


def plan_release(
    root: Path,
    package: str,
    event: str,
    ref: str,
    *,
    dry_run: bool,
    on_main: bool,
    on_pypi: Callable[[str, str], bool] = pypi_has,
) -> JSON:
    """``on_pypi(project, version)``: whether PyPI has it; the default asks
    PyPI, and only for a publication (no network for a dry run)."""
    projects = {
        name: tomllib.loads((root / "packages" / name / "pyproject.toml").read_text())[
            "project"
        ]
        for name in PACKAGES
    }
    versions = {name: project["version"] for name, project in projects.items()}
    if not all(VERSION.fullmatch(value) for value in versions.values()):
        raise ValueError(
            "Package versions must be public X.Y.Z or alpha/beta/rc versions"
        )
    selected, publish = select_release_packages(
        package, event, ref, versions, dry_run=dry_run
    )
    if publish and not on_main:
        raise ValueError("Publication requires a commit reachable from origin/main")
    if len(selected) > 1 and len({versions[name] for name in selected}) != 1:
        raise ValueError("A coordinated release requires matching package versions")
    # Before any build: a dry run fails on a fragment that is not folded too.
    refuse_unfolded_fragments(root, selected)
    # Also before any build: the notes must fit the GitHub release body.
    check_release_notes_size(root, selected, versions[selected[0]])
    if publish:
        refuse_unpublished_siblings(projects, versions, selected, on_pypi)
    return {"packages": selected, "versions": versions, "publish": publish}


def filenames(project: str, version: str) -> tuple[str, str]:
    if project not in PACKAGES or not VERSION.fullmatch(version):
        raise ValueError("Invalid artifact project/version")
    stem = project.replace("-", "_")
    return f"{stem}-{version}-py3-none-any.whl", f"{stem}-{version}.tar.gz"


def select_artifacts(plan: JSON, artifacts: Path, output: Path) -> JSON:
    versions = plan["versions"]
    if (
        set(versions) != set(PACKAGES)
        or not plan["packages"]
        or not set(plan["packages"]) <= set(PACKAGES)
    ):
        raise ValueError("Incomplete release plan")
    # Again, as the plan job did: this one runs on the artifacts' own checkout.
    refuse_unfolded_fragments(Path.cwd(), plan["packages"])
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
    notes = release_notes(Path.cwd(), plan["packages"], versions[plan["packages"][0]])
    (output / "release-notes.md").write_text(notes)
    for tool in ("pypi_release.py", "release_packages.py", "packages.json"):
        shutil.copyfile(Path(__file__).with_name(tool), output / tool)
    return manifest


def fetch_release(project: str, version: str) -> JSON | None:
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
        release: JSON = json.loads(response.read())
        return release
    finally:
        connection.close()


def validate_manifest(manifest: JSON) -> list[JSON]:
    rows: list[JSON] = manifest["files"]
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


def missing_files(manifest: JSON, fetch: Fetch = fetch_release) -> list[JSON]:
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
    manifest: JSON, dist: Path, upload: Path, fetch: Fetch = fetch_release
) -> bool:
    validate_local_artifacts(manifest, dist)
    missing = missing_files(manifest, fetch)
    upload.mkdir(parents=True, exist_ok=False)
    for row in missing:
        shutil.copyfile(dist / row["filename"], upload / row["filename"])
    return bool(missing)


def validate_local_artifacts(manifest: JSON, dist: Path) -> list[JSON]:
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


def checksum_text(rows: list[JSON]) -> str:
    return "".join(
        f"{row['sha256']}  {row['filename']}\n"
        for row in sorted(rows, key=lambda row: row["filename"])
    )


class MissingNotesError(ValueError):
    """The changelog has no section (or an empty one) for the version."""


def notes_path(root: Path, packages: list[str]) -> Path:
    """A single package's notes are its own changelog, a coordinated release's
    the root ``CHANGELOG.md``."""
    if len(packages) == 1:
        return root / "packages" / packages[0] / "CHANGELOG.md"
    return root / "CHANGELOG.md"


def refuse_oversized_notes(path: Path, version: str, notes: str) -> None:
    """The release is created after the PyPI upload: a body GitHub rejects
    there would leave PyPI published and no release."""
    if len(notes) > NOTES_LIMIT:
        raise ValueError(
            f"Release notes of {version} in {path.name} are {len(notes):,} "
            f"characters, over the {NOTES_LIMIT:,} GitHub accepts in a release "
            "body: shorten the section"
        )


def check_release_notes_size(root: Path, packages: list[str], version: str) -> None:
    """The plan's check, before any build. A missing section is not judged
    here: ``select_artifacts`` refuses it, on the reviewed artifacts."""
    path = notes_path(root, packages)
    if not path.is_file():
        return
    try:
        notes = changelog_section(path, version)
    except MissingNotesError:
        return
    refuse_oversized_notes(path, version, notes)


def release_notes(root: Path, packages: list[str], version: str) -> str:
    """The GitHub release body: the changelog section of the selected scope,
    refused when missing or over ``NOTES_LIMIT``."""
    path = notes_path(root, packages)
    notes = changelog_section(path, version)
    refuse_oversized_notes(path, version, notes)
    return notes


def changelog_section(path: Path, version: str) -> str:
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        path.read_text(),
        re.M | re.S,
    )
    if match is None or not match[1].strip():
        raise MissingNotesError(f"Missing changelog notes for {version}: {path.name}")
    return match[1].strip() + "\n"


def github_release_spec(manifest: JSON) -> JSON:
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


def find_github_release(api: Api, base: str, tag: str) -> JSON | None:
    published: JSON | None = api(f"{base}/releases/tags/{tag}")
    if published is not None:
        return published
    # A draft's pending tag may not exist yet, so REST lookup by tag returns
    # 404. Authenticated release listings include drafts; inspect their exact
    # pending tag rather than mistaking an existing draft for an absent release.
    page = 1
    while True:
        releases: list[JSON] = api(f"{base}/releases?per_page=100&page={page}")
        matches = [r for r in releases if r["draft"] and r["tag_name"] == tag]
        if len(matches) > 1:
            raise ValueError("Multiple drafts have the same pending release tag")
        if matches:
            return matches[0]
        if len(releases) < 100:
            return None
        page += 1


def github_tag_source(api: Api, base: str, tag: str) -> str | None:
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
    sha: str = target["sha"]
    return sha


def wait_github_release(api: Api, base: str, tag: str) -> JSON:
    """Bound delayed REST visibility after creation; retry reads, never writes."""
    for attempt in range(6):
        current = find_github_release(api, base, tag)
        if current is not None:
            return current
        if attempt < 5:
            time.sleep(2)
    raise RuntimeError("GitHub release is not visible after 6 reads")


def require_reviewed_source(manifest: JSON, repository: str, source_sha: str) -> None:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not re.fullmatch(
        r"[a-f0-9]{40}", source_sha
    ):
        raise ValueError("Invalid GitHub repository/source")
    if manifest.get("source_sha") != source_sha:
        raise ValueError("Release source does not match the reviewed manifest")


def read_release_files(manifest_path: Path, rows: list[JSON]) -> tuple[Path, Path]:
    """The reviewed checksum file and release notes, each checked."""
    folder = manifest_path.parent
    checksums = folder / "SHA256SUMS"
    if checksums.read_text() != checksum_text(rows):
        raise ValueError("Release checksum file differs from the reviewed manifest")
    notes = folder / "release-notes.md"
    if not notes.read_text().strip():
        raise ValueError("Release notes are empty")
    return checksums, notes


def ensure_release_draft(
    api: Api,
    command: Callable[[list[str]], str],
    base: str,
    spec: JSON,
    repository: str,
    source_sha: str,
    notes: Path,
) -> JSON | None:
    """The existing release of the tag (``None``: a draft was created for it).

    An existing tag or draft must point at the reviewed source commit."""
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
    return existing


def publish_github_release(
    manifest_path: Path,
    dist: Path,
    *,
    repository: str,
    source_sha: str,
    api: Api = github_api,
    command: Callable[[list[str]], str] = github_command,
    fetch: Fetch = fetch_release,
) -> None:
    """Publish a draft only after PyPI and every GitHub asset hash are verified.

    Reruns fill missing assets; an existing tag or asset is never overwritten.
    No checkout, build, package installation or PyPI credentials are needed.
    """
    manifest = json.loads(manifest_path.read_text())
    rows = validate_local_artifacts(manifest, dist)
    require_reviewed_source(manifest, repository, source_sha)
    spec = github_release_spec(manifest)
    checksums, notes = read_release_files(manifest_path, rows)
    verify_published(manifest, fetch=fetch)
    base = f"repos/{repository}"
    tag = spec["tag"]
    existing = ensure_release_draft(
        api, command, base, spec, repository, source_sha, notes
    )
    assets = [dist / row["filename"] for row in rows] + [checksums, manifest_path]
    wanted = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in assets}

    def check_assets() -> set[str]:
        current = wait_github_release(api, base, tag)
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
    manifest: JSON,
    *,
    timeout: float = 120,
    fetch: Fetch = fetch_release,
    sleep: Callable[[float], None] = time.sleep,
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
