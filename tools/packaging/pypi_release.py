"""Plan, select and verify reviewed PyPI artifacts; never handle upload credentials."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
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
    missing = missing_files(manifest, fetch)
    upload.mkdir(parents=True, exist_ok=False)
    for row in missing:
        shutil.copyfile(dist / row["filename"], upload / row["filename"])
    return bool(missing)


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
    parser.add_argument("action", choices=["plan", "select", "preflight", "verify"])
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
    else:
        verify_published(json.loads(args.manifest.read_text()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"Release verification failed: {exc}")
        raise SystemExit(1) from exc
