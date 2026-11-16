#!/usr/bin/env python3
"""Per-branch changelog fragments: ``changelog.d/<name>.md``, folded at release.

Every branch used to add bullets under ``## [Unreleased]`` of ``CHANGELOG.md``.
Git's union merge driver keeps both sides locally, but GitHub ignores merge
drivers, so a PR went CONFLICTING whenever another one merged. A branch now adds
one file of its own, ``changelog.d/<name>.md`` (the task alias), and nothing in
``CHANGELOG.md``; no two branches touch the same file.

A change that belongs to one package's own ``packages/<package>/CHANGELOG.md``
(the notes of a single-package release) goes into
``changelog.d/<package>/<name>.md`` the same way.

A fragment is a list of bullets under ``### <kind>`` headings::

    ### Fixed

    - **The thing** (alias). What changed and the upgrade steps, if any.
      A bullet may continue on lines indented by two spaces.

``--check`` validates every fragment. ``--fold <version>`` is the release step:
it writes ``## [<version>] — <date>`` into ``CHANGELOG.md`` (every fragment's
bullets grouped by kind; the loose bullets still under ``## [Unreleased]``, a
one-time transition from before fragments, go first under ``### Changed``),
leaves ``## [Unreleased]`` empty and deletes the fragments.
``--fold <version> --package <package>`` does the same for that package's
fragments and changelog. The version must be the one in the package's
``pyproject.toml`` (``packages/sandloom`` for the root changelog), so run
``sync_release.py --version`` first. ``pypi_release.py`` refuses a release while
fragments of its scope are left.

The tool reads and writes files only; it needs nothing installed.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRAGMENT_DIR = "changelog.d"
README = "README.md"
#: The coordinated release's notes and version come from this package's metadata.
FRAMEWORK = "sandloom"
# The order a folded release lists them in: what breaks first, then what is new.
KINDS = ("Breaking changes", "Security", "Deprecated", "Added", "Changed", "Fixed")
#: Where the loose bullets already under ``[Unreleased]`` land when folded.
LEGACY_KIND = "Changed"
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$")
HEADING = re.compile(r"^### (?P<kind>.+?)\s*$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.(?:post|dev)\d+)?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNRELEASED = "## [Unreleased]"
SHAPE = (
    "only '### <kind>' headings, '- ' bullets and bullet continuations "
    "indented by two spaces are allowed"
)


def _changelog(root: Path, package: str | None) -> Path:
    if package is None:
        return root / "CHANGELOG.md"
    return root / "packages" / package / "CHANGELOG.md"


def package_scopes(root: Path) -> list[str]:
    """The directories of ``changelog.d/`` named like a package with a changelog."""
    folder = root / FRAGMENT_DIR
    if not folder.is_dir():
        return []
    return sorted(
        p.name
        for p in folder.iterdir()
        if p.is_dir() and _changelog(root, p.name).is_file()
    )


def fragment_paths(root: Path, package: str | None = None) -> list[Path]:
    """The fragment files of one scope, sorted; the directory may be absent.

    The root scope skips the package directories (they are scopes of their own);
    anything else in it that is not a fragment is listed, so ``check`` names it.
    """
    folder = root / FRAGMENT_DIR
    if package is None:
        skip = {README, *package_scopes(root)}
    else:
        folder, skip = folder / package, set()
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.name not in skip)


class _Fragment:
    """One fragment's lines, read top to bottom: ``kinds`` and ``errors``."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.kinds: dict[str, list[str]] = {}
        self.errors: list[str] = []
        self._kind: str | None = None
        self._open = False  # the last bullet may take continuation lines

    def feed(self, number: int, line: str) -> None:
        here = f"{self.where}:{number}"
        heading = HEADING.match(line)
        if heading:
            self._heading(here, heading["kind"])
        elif not line.strip():
            self._open = False
        elif line.startswith("- "):
            self._bullet(here, line)
        elif line.startswith("  ") and self._open and self._kind is not None:
            self.kinds[self._kind][-1] += "\n" + line
        else:
            self.errors.append(f"{here}: {SHAPE}")

    def _heading(self, here: str, kind: str) -> None:
        self._open = False
        self._kind = None
        if kind not in KINDS:
            self.errors.append(f"{here}: unknown kind {kind!r}; use {list(KINDS)}")
        elif kind in self.kinds:
            self.errors.append(f"{here}: kind {kind!r} appears twice in the file")
        else:
            self._kind = kind
            self.kinds[kind] = []

    def _bullet(self, here: str, line: str) -> None:
        if self._kind is None:
            self.errors.append(f"{here}: a bullet needs a known '### <kind>' above it")
        elif not line[2:].strip():
            self.errors.append(f"{here}: empty bullet")
        else:
            self.kinds[self._kind].append(line)
            self._open = True

    def finish(self) -> None:
        for kind, bullets in self.kinds.items():
            if not bullets:
                self.errors.append(f"{self.where}: kind {kind!r} has no bullet")
        if not self.kinds and not self.errors:
            self.errors.append(f"{self.where}: no '### <kind>' section with a bullet")


def _where(path: Path, package: str | None) -> str:
    return "/".join(p for p in (FRAGMENT_DIR, package, path.name) if p)


def parse_fragment(
    path: Path, package: str | None = None
) -> tuple[dict[str, list[str]], list[str]]:
    """Return ``({kind: [bullet, ...]}, errors)`` for one fragment file."""
    where = _where(path, package)
    if not path.is_file() or not NAME.match(path.name):
        return {}, [
            f"{where}: a fragment is a file named like its task alias, lowercase "
            "letters, digits, '.', '_' or '-', ending in .md"
        ]
    parsed = _Fragment(where)
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parsed.feed(number, line)
    parsed.finish()
    return parsed.kinds, parsed.errors


def check_scope(root: Path, package: str | None = None) -> list[str]:
    """Every problem of one scope's fragments, empty when it is sound."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for path in fragment_paths(root, package):
        folded = path.name.lower()
        if folded in seen:
            errors.append(
                f"{_where(path, package)}: same name as {seen[folded]} "
                "ignoring case (a case-insensitive checkout cannot hold both)"
            )
        seen[folded] = path.name
        errors.extend(parse_fragment(path, package)[1])
    return errors


def check(root: Path = ROOT) -> list[str]:
    """Every problem of the fragment directory, empty when it is sound."""
    errors = check_scope(root)
    for package in package_scopes(root):
        errors.extend(check_scope(root, package))
    return errors


def refuse_unfolded(root: Path, scopes: Iterable[str | None]) -> None:
    """Raise when a release would ship notes that miss unfolded fragments.

    ``scopes``: ``None`` is the root ``CHANGELOG.md``, a name is that package's.
    """
    left = [
        _where(path, package)
        for package in scopes
        for path in fragment_paths(root, package)
    ]
    if left:
        raise ValueError(
            f"Unfolded changelog fragments: {', '.join(left)}. Run "
            "`python3 tools/packaging/changelog_fragments.py --fold <version>` "
            "(with `--package <package>` for a package fragment) in the release "
            "preparation and commit the result before releasing."
        )


def _grouped(paths: list[Path], package: str | None, legacy: str) -> list[str]:
    """One ``### <kind>`` block per kind that has bullets, in ``KINDS`` order."""
    grouped: dict[str, list[str]] = {kind: [] for kind in KINDS}
    if legacy:
        grouped[LEGACY_KIND].append(legacy)
    for path in paths:
        for kind, bullets in parse_fragment(path, package)[0].items():
            grouped[kind].extend(bullets)
    return [
        f"### {kind}\n\n" + "\n".join(grouped[kind]) for kind in KINDS if grouped[kind]
    ]


def _framework_version(root: Path, package: str | None) -> str:
    name = package or FRAMEWORK
    pyproject = root / "packages" / name / "pyproject.toml"
    try:
        return str(tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise ValueError(
            f"Cannot read the version of {name} from {pyproject}"
        ) from error


def _split_unreleased(text: str, name: str) -> tuple[str, str, str]:
    """``(head, legacy, older)``: the text up to and with the Unreleased heading,
    the loose bullets under it, the release sections after it. A changelog that
    has no Unreleased heading yet (a package's, before its first entry) gets one
    above its first release section."""
    found = re.search(rf"^{re.escape(UNRELEASED)}[ \t]*\n", text, re.M)
    if found is None:
        first = re.search(r"^## \[", text, re.M)
        if first is None:
            raise ValueError(
                f"{name} has no '{UNRELEASED}' heading nor release section"
            )
        return text[: first.start()] + UNRELEASED + "\n", "", text[first.start() :]
    rest = text[found.end() :]
    following = re.search(r"^## \[", rest, re.M)
    cut = following.start() if following else len(rest)
    legacy = rest[:cut].strip("\n")
    if re.search(r"^### ", legacy, re.M):
        raise ValueError(
            f"{UNRELEASED} of {name} has '###' headings: move its entries "
            "into fragments by hand, then fold"
        )
    return text[: found.end()], legacy, rest[cut:]


def fold(root: Path, version: str, date: str, package: str | None = None) -> str:
    """Fold one scope's fragments into its changelog; returns the new section."""
    if not VERSION.match(version):
        raise ValueError(f"Not a release version: {version!r}")
    if not DATE.match(date):
        raise ValueError(f"Not an ISO date (YYYY-MM-DD): {date!r}")
    datetime.date.fromisoformat(date)  # ValueError for 2026-13-45
    changelog = _changelog(root, package)
    if not changelog.is_file():
        raise ValueError(f"No changelog at {changelog}")
    text = changelog.read_text(encoding="utf-8")
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.M):
        raise ValueError(f"{changelog.name} already has a section for {version}")
    expected = _framework_version(root, package)
    if version != expected:
        raise ValueError(
            f"Version {version} is not the package version {expected}: run "
            "sync_release.py --version first"
        )
    errors = check_scope(root, package)
    if errors:
        raise ValueError("Fragments are not sound:\n" + "\n".join(errors))
    head, legacy, older = _split_unreleased(text, changelog.name)
    paths = fragment_paths(root, package)
    blocks = _grouped(paths, package, legacy)
    if not blocks:
        raise ValueError("Nothing to fold: no fragment and no Unreleased entry")
    section = f"## [{version}] — {date}\n\n" + "\n\n".join(blocks) + "\n"
    folded = f"{head}\n{section}" + (f"\n{older}" if older else "")
    changelog.write_text(folded, "utf-8")
    for path in paths:
        path.unlink()
    return section


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate the fragments")
    mode.add_argument("--fold", metavar="VERSION", help="fold into the changelog")
    parser.add_argument("--package", help="fold this package's fragments and changelog")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.check:
            errors = check(args.root)
            for error in errors:
                print(error, file=sys.stderr)
            return 1 if errors else 0
        fold(args.root, args.fold, args.date, args.package)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
