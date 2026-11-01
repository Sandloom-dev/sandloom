"""Synchronize a coordinated release from packages/sandloom/pyproject.toml.

Check: python tools/packaging/sync_release.py
Bump:  python tools/packaging/sync_release.py --version <version> --write

Package pins are discovered in current docs, READMEs, examples and scaffolding.
Prose with a current version uses an inline <!-- sandloom-release --> marker.
Historical changelogs, ADRs, research and the dependency audit are immutable.
Locks are regenerated with uv lock (root and benchmarks), never text-edited.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "sandloom",
    "sandloom-identity",
    "sandloom-tenancy",
    "sandloom-billing",
    "sandloom-admin",
)
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?"
VALID_VERSION = re.compile(VERSION + r"\Z")
PIN = re.compile(
    r"(?P<prefix>(?P<package>sandloom(?:-(?:identity|tenancy|billing|<module>))?)"
    r"(?:\[[^]\n]+\])?(?:==|>=))(?P<version>" + VERSION + r")"
    r"(?P<upper>,<[0-9]+\.[0-9]+(?:\.[0-9]+)?)?"
)
PYPI = re.compile(
    r"(?P<prefix>(?:https://pypi\.org/project/"
    r"(?P<package>sandloom(?:-(?:identity|tenancy|billing))?)/"
    r"|https://img\.shields\.io/badge/PyPI-))(?P<version>" + VERSION + r")"
)
MARKER = "<!-- sandloom-release -->"
HISTORY = (
    "docs/adr/",
    "docs/design/",
    "docs/contributing/dependency-security-audit.md",
)


def version(root: Path) -> str:
    value = tomllib.loads((root / "packages/sandloom/pyproject.toml").read_text())[
        "project"
    ]["version"]
    if not VALID_VERSION.fullmatch(value):
        raise ValueError(f"Invalid canonical version: {value}")
    return value


def upper_bound(value: str) -> str:
    major, minor = map(int, value.split(".")[:2])
    return f"{major}.{minor + 1}" if major == 0 else str(major + 1) + ".0"


def current_files(root: Path) -> list[Path]:
    selected = {
        root / "pyproject.toml",
        root / "src/sandloom/__init__.py",
        root / "src/sandloom/core/cli/project.py",
        root / "src/sandloom/core/tests/test_project.py",
    }
    for pattern in (
        "**/README.md",
        "docs/**/*.md",
        "website/**/*.html",
        "website/**/*.txt",
        "examples/**/pyproject.toml",
        "packages/*/pyproject.toml",
    ):
        # Restrict README discovery to source directories, excluding build/venvs.
        bases = (root,)
        if pattern == "**/README.md":
            selected.add(root / "README.md")
            bases = tuple(
                root / name
                for name in ("packages", "examples", "docs", "website", "deploy")
            )
        for base in bases:
            selected.update(base.glob(pattern))
    return sorted(
        path
        for path in selected
        if path.is_file()
        and not path.relative_to(root).as_posix().startswith(HISTORY)
        and path.name != "CHANGELOG.md"
    )


def synchronized(
    text: str,
    value: str,
    *,
    generator: bool = False,
    versions: dict[str, str] | None = None,
) -> str:
    bound = upper_bound(value)
    versions = versions or {}
    text = PIN.sub(
        lambda m: (
            m["prefix"]
            + versions.get(m["package"], value)
            + (
                f",<{upper_bound(versions.get(m['package'], value))}"
                if m["upper"]
                else ""
            )
        ),
        text,
    )
    text = PYPI.sub(lambda m: m["prefix"] + versions.get(m["package"], value), text)
    text = re.sub(
        r"(sync_release\.py --version )" + VERSION, lambda m: m[1] + value, text
    )
    text = re.sub(
        r"(sandloom(?:-(?:identity|tenancy|billing))?-v)" + VERSION,
        lambda m: m[1] + value,
        text,
    )
    if generator:
        text = re.sub(
            r'^FRAMEWORK_RANGE = "[^"\n]+"$',
            f'FRAMEWORK_RANGE = ">={value},<{bound}"',
            text,
            flags=re.M,
        )
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if MARKER in line:
            lines[i] = re.sub(VERSION, value, line)
    return "".join(lines)


def changes(root: Path, value: str, *, coordinated: bool = False) -> dict[Path, str]:
    if not VALID_VERSION.fullmatch(value):
        raise ValueError(f"Invalid release version: {value}")
    metadata = {
        root / "pyproject.toml",
        *(root / "packages" / name / "pyproject.toml" for name in PACKAGES),
    }
    versions = {
        name: value
        if coordinated or name == "sandloom"
        else tomllib.loads((root / "packages" / name / "pyproject.toml").read_text())[
            "project"
        ]["version"]
        for name in PACKAGES
    }
    result = {}
    for path in current_files(root):
        original = path.read_text()
        module_metadata = path in metadata and path.parent.name in PACKAGES[1:]
        updated = (
            original
            if module_metadata and not coordinated
            else synchronized(
                original,
                value,
                generator=path == root / "src/sandloom/core/cli/project.py",
                versions=versions,
            )
        )
        if path in metadata:
            target = versions.get(path.parent.name, value)
            updated = re.sub(
                r'^version = "' + VERSION + r'"$',
                f'version = "{target}"',
                updated,
                count=1,
                flags=re.M,
            )
        if updated != original:
            result[path] = updated
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", help="prepare a coordinated version (requires --write)"
    )
    parser.add_argument(
        "--write", action="store_true", help="update current release references"
    )
    args = parser.parse_args(argv)
    if args.version and not args.write:
        parser.error(
            "--version requires --write; checks use the canonical package version"
        )
    value = args.version or version(ROOT)
    if not VALID_VERSION.fullmatch(value):
        parser.error("expected X.Y.Z or X.Y.ZaN/bN/rcN")
    pending = changes(ROOT, value, coordinated=bool(args.version))
    for path, updated in pending.items():
        relative = path.relative_to(ROOT).as_posix()
        if args.write:
            path.write_text(updated)
            print(f"Updated {relative}")
        else:
            sys.stdout.writelines(
                difflib.unified_diff(
                    path.read_text().splitlines(True),
                    updated.splitlines(True),
                    fromfile=relative,
                    tofile=relative,
                )
            )
    if pending and not args.write:
        print(
            "Release references are stale: "
            "python tools/packaging/sync_release.py --write",
            file=sys.stderr,
        )
        return 1
    print(f"Release references synchronized: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
