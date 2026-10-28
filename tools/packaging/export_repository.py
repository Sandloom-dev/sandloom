"""Prepare a reviewed source snapshot; --check / --audit-report never copy it.

Only exact paths in public-source.json may be copied. No commit, remote,
installation, build, test, eval or publication is performed by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.checks.check_secrets import findings  # noqa: E402

POLICY = "tools/packaging/public-source.json"
SCOPES = (
    "src",
    "tests",
    "packages",
    "migrations",
    "admin",
    "docs",
    "examples",
    "deploy",
    "tools",
    "benchmarks",
    "website",
    "stubs",
    ".github",
    ".agents",
    ".claude",
)
EXCLUDED = {
    ".git",
    "private",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "dist",
    "build",
    "site",
    "test-results",
    "playwright-report",
    "storybook-static",
    "data",
    "test_data",
    "storage",
    ".serena",
    "side-session-notes",
    ".ci-local",
    "htmlcov",
    "target",
}
SUFFIXES = {
    ".pyc",
    ".pyo",
    ".bak",
    ".orig",
    ".rej",
    ".old",
    ".swp",
    ".swo",
    ".pem",
    ".key",
}


class ExportError(ValueError):
    """An explicit review or source correction is required."""


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or str(path) != name
    ):
        raise ExportError(f"Non-canonical repository path: {name!r}")
    return path


def forbidden(name: str) -> bool:
    path = safe_name(name)
    return (
        bool(set(path.parts) & EXCLUDED)
        or path.suffix.lower() in SUFFIXES
        or path.name
        in {".DS_Store", ".observed_effects.json", ".observed_effects.json.lock"}
        or path.name.endswith("~")
        or path.name.startswith(".coverage")
        or (
            path.name.startswith(".env")
            and path.name not in {".env.example", ".env.test"}
        )
    )


def approved_paths(groups: list[dict]) -> dict[str, str]:
    approved: dict[str, str] = {}
    for group in groups:
        if not group["reason"].strip():
            raise ExportError("Every inclusion group requires a reason")
        for name in group["paths"]:
            if forbidden(name) or name in approved:
                raise ExportError(f"Forbidden or repeated approved path: {name}")
            approved[name] = group["reason"]
    return approved


def load_policy(root: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    raw = json.loads((root / POLICY).read_text())
    if raw.get("schema") != 1:
        raise ExportError("Unsupported public source policy schema")
    approved = approved_paths(raw["include"])
    excluded = raw["exclude_files"]
    prefixes = raw["exclude_trees"]
    for name, reason in {**excluded, **prefixes}.items():
        safe_name(name)
        if not reason.strip():
            raise ExportError(f"Exclusion requires a reason: {name}")
    for name in approved:
        if name in excluded or any(
            name == prefix or name.startswith(prefix + "/") for prefix in prefixes
        ):
            raise ExportError(f"Conflicting inclusion/exclusion: {name}")
    if POLICY not in approved:
        raise ExportError("The public source policy must include itself")
    return approved, excluded, prefixes


def tracked_files(root: Path) -> set[str]:
    """Paths in the Git index of ``root``: the only files that can be exported.

    Untracked files (local CI workflows, scratch output, editor files) are
    never candidates, whatever the policy says. Inherited ``GIT_*`` variables
    are dropped so a hook cannot point the query at another repository.
    """
    git = shutil.which("git")
    if git is None:
        raise ExportError("Git is required to list the tracked source files")
    result = subprocess.run(  # noqa: S603 - static argv, reviewed root, no shell
        [git, "-C", str(root), "ls-files", "-z", "--cached", "--full-name"],
        env=git_environment(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ExportError(
            f"Source root is not a Git work tree: {root} ({result.stderr.strip()})"
        )
    return {name for name in result.stdout.split("\0") if name}


def candidates(root: Path, tracked: set[str]) -> set[str]:
    """Discover tracked additions for review inside the public source roots."""
    return {
        name
        for name in tracked
        if ("/" not in name or name.split("/", 1)[0] in SCOPES) and not forbidden(name)
    }


def inspect_file(
    root: Path, name: str, reason: str, approved: dict, symlinks: dict
) -> dict:
    source = root / name
    # One packaging namespace link is intentional. No directory-link
    # traversal is permitted; its target bytes are reviewed separately.
    if any(
        p.is_symlink() for p in source.parents if p != root and p.is_relative_to(root)
    ):
        raise ExportError(f"Symlink ancestor is not approved for source export: {name}")
    if source.is_symlink():
        link = source.readlink()
        resolved = source.resolve()
        if (
            name not in symlinks
            or str(link) != symlinks[name]
            or link.is_absolute()
            or not resolved.is_relative_to(root)
            or not resolved.exists()
        ):
            raise ExportError(f"Symlink is not approved for source export: {name}")
        target_name = resolved.relative_to(root).as_posix()
        if not any(
            p == target_name or p.startswith(target_name + "/") for p in approved
        ):
            raise ExportError(f"Symlink target has no reviewed source files: {name}")
        payload = str(link).encode()
        return {
            "path": name,
            "kind": "symlink",
            "target": str(link),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "reason": reason,
        }
    if name in symlinks:
        raise ExportError(f"Reviewed symlink is missing: {name}")
    if not source.is_file() or not stat.S_ISREG(source.stat().st_mode):
        raise ExportError(f"Approved regular source file is missing: {name}")
    payload = source.read_bytes()
    try:
        content = payload.decode("utf-8")
    except UnicodeError:
        content = None
    if content is not None:
        matches = findings(Path(name), content)
        if matches:
            positions = ", ".join(f"{line}:{rule}" for line, rule in matches)
            raise ExportError(
                f"Credential-shaped material (values withheld): {name}: {positions}"
            )
    return {
        "path": name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "reason": reason,
    }


def inspect_source(root: Path = ROOT) -> dict:
    approved, excluded, prefixes = load_policy(root)
    tracked = tracked_files(root)
    unknown = sorted(
        name
        for name in candidates(root, tracked)
        if name not in approved
        and name not in excluded
        and not any(name.startswith(prefix + "/") for prefix in prefixes)
    )
    if unknown:
        raise ExportError(
            "Unreviewed source paths; classify them in public-source.json:\n"
            + "\n".join(unknown)
        )
    symlinks = json.loads((root / POLICY).read_text()).get("symlinks", {})
    if not set(symlinks) <= set(approved):
        raise ExportError("Every reviewed symlink must also be an included path")
    rows = [
        inspect_file(root, name, reason, approved, symlinks)
        for name, reason in sorted(approved.items())
    ]
    untracked = sorted(set(approved) - tracked)
    if untracked:
        raise ExportError(
            "Approved source paths are not tracked by Git (only tracked files "
            "are exported):\n" + "\n".join(untracked)
        )
    return {
        "schema": 1,
        "status": "source selection checked; no export or publication performed",
        "policy_sha256": hashlib.sha256((root / POLICY).read_bytes()).hexdigest(),
        "public_files": rows,
        "excluded_files": excluded,
        "excluded_trees": prefixes,
        "limits": [
            "Credential patterns are narrow; historical clearance is separate.",
            "Path approval does not replace code review of later content changes.",
            "No builds, tests, installations or paid agent evaluations were run.",
        ],
    }


def public_files() -> list[Path]:
    return [ROOT / item["path"] for item in inspect_source()["public_files"]]


def git_environment() -> dict[str, str]:
    env = dict(os.environ)
    # Hooks export GIT_DIR / GIT_INDEX_FILE: git -C alone does not isolate a repo.
    for name in tuple(env):
        if name.startswith("GIT_"):
            env.pop(name)
    return env


def export_source(destination: Path, report: dict, root: Path = ROOT) -> None:
    destination = destination.expanduser()
    if destination.is_symlink():
        raise ExportError("Destination cannot be a symlink")
    destination = destination.resolve()
    if destination.exists() or destination.is_relative_to(root.resolve()):
        raise ExportError("Destination must be new and outside the source checkout")
    # Recheck links, policy and bytes BEFORE creating a destination.
    current = inspect_source(root)
    if current != report:
        raise ExportError("Source or policy changed since inspection")
    destination.mkdir(parents=True)
    try:
        for item in report["public_files"]:
            source = root / item["path"]
            target = destination / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.get("kind") == "symlink":
                target.symlink_to(item["target"], target_is_directory=source.is_dir())
                payload = str(target.readlink()).encode()
            else:
                shutil.copy2(source, target)
                payload = target.read_bytes()
            if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise ExportError(f"Source changed while copying: {item['path']}")
        copied = [dict(item) for item in report["public_files"]]
        ignore = destination / ".gitignore"
        if ignore.exists() and "private/" not in ignore.read_text().splitlines():
            with ignore.open("a") as stream:
                stream.write(
                    "\n# Internal preparation material stays outside public source.\n"
                    "private/\n"
                )
            row = next(item for item in copied if item["path"] == ".gitignore")
            row["source_sha256"] = row["sha256"]
            row["sha256"] = hashlib.sha256(ignore.read_bytes()).hexdigest()
        license_expression = tomllib.loads((root / "pyproject.toml").read_text())[
            "project"
        ].get("license")
        (destination / "SOURCE_EXPORT.json").write_text(
            json.dumps(
                {
                    **report,
                    "public_files": copied,
                    "status": "local preparation; not built, tested or published",
                    "remote_configured": False,
                    "builds_tests_and_evals_run": False,
                    "license": license_expression,
                },
                indent=2,
            )
            + "\n"
        )
        git = shutil.which("git")
        if git is None:
            raise ExportError("Git is required to initialize a fresh snapshot")
        subprocess.run(  # noqa: S603 - static argv, reviewed destination, no shell
            [git, "-C", str(destination), "init", "--initial-branch=main"],
            check=True,
            env=git_environment(),
            capture_output=True,
            text=True,
        )
        # Only reviewed paths enter the index. The receipt and later build
        # artifacts remain untracked; inherited hook variables are isolated.
        subprocess.run(  # noqa: S603 - manifest paths, static argv, no shell
            [
                git,
                "-C",
                str(destination),
                "add",
                "--force",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            input="".join(item["path"] + "\0" for item in copied),
            check=True,
            env=git_environment(),
            capture_output=True,
            text=True,
        )
    except BaseException:
        # Only this newly-created destination belongs to this attempt.
        shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    parser.add_argument(
        "--check", action="store_true", help="Check selection without copying files"
    )
    parser.add_argument(
        "--audit-report",
        type=Path,
        help="Write a selection/byte-hash report without exporting",
    )
    args = parser.parse_args()
    if args.destination and (args.check or args.audit_report):
        parser.error("Export and read-only selection modes must be invoked separately")
    if not (args.destination or args.check or args.audit_report):
        parser.error("Choose --check, --audit-report or --destination")
    try:
        report = inspect_source()
        if args.destination:
            export_source(args.destination, report)
        if args.audit_report:
            args.audit_report.parent.mkdir(parents=True, exist_ok=True)
            args.audit_report.write_text(json.dumps(report, indent=2) + "\n")
    except (ExportError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Source export refused: {exc}\n")
    action = "Prepared" if args.destination else "Checked"
    print(f"{action} {len(report['public_files'])} reviewed source files")
    return 0


if __name__ == "__main__":
    # Support the documented direct script invocation from any working directory.
    raise SystemExit(main())
