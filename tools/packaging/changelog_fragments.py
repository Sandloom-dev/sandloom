#!/usr/bin/env python3
"""Per-branch changelog fragments: ``changelog.d/<name>.md``, folded at release.

Every branch used to add bullets under ``## [Unreleased]`` of ``CHANGELOG.md``.
Git's union merge driver keeps both sides locally, but GitHub ignores merge
drivers, so a PR went CONFLICTING whenever another one merged. A branch now adds
one file of its own, ``changelog.d/<name>.md`` (the task alias), and nothing in
``CHANGELOG.md``; no two branches touch the same file.

A fragment is a list of bullets under ``### <kind>`` headings::

    ### Fixed

    - **The thing** (alias). What changed and the upgrade steps, if any.
      A bullet may continue on lines indented by two spaces.

``--check`` validates every fragment. ``--fold <version>`` is the release step:
it writes ``## [<version>] — <date>`` into ``CHANGELOG.md`` (the legacy loose
bullets of ``## [Unreleased]``, then every fragment's bullets grouped by kind),
leaves ``## [Unreleased]`` empty and deletes the fragments. The release path
(``pypi_release.py``) refuses a coordinated release while fragments are left.

The tool reads and writes files only; it needs nothing installed.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRAGMENT_DIR = "changelog.d"
README = "README.md"
# The order a folded release lists them in: what breaks first, then what is new.
KINDS = ("Breaking changes", "Security", "Deprecated", "Added", "Changed", "Fixed")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$")
HEADING = re.compile(r"^### (?P<kind>.+?)\s*$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.(?:post|dev)\d+)?$")
UNRELEASED = "## [Unreleased]"
SHAPE = (
    "only '### <kind>' headings, '- ' bullets and bullet continuations "
    "indented by two spaces are allowed"
)


def fragment_paths(root: Path) -> list[Path]:
    """The fragment files, sorted; the directory may be absent (nothing pending)."""
    folder = root / FRAGMENT_DIR
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.name != README)


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


def parse_fragment(path: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Return ``({kind: [bullet, ...]}, errors)`` for one fragment file."""
    where = f"{FRAGMENT_DIR}/{path.name}"
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


def check(root: Path = ROOT) -> list[str]:
    """Every problem of the fragment directory, empty when it is sound."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for path in fragment_paths(root):
        folded = path.name.lower()
        if folded in seen:
            errors.append(
                f"{FRAGMENT_DIR}/{path.name}: same name as {seen[folded]} "
                "ignoring case (a case-insensitive checkout cannot hold both)"
            )
        seen[folded] = path.name
        errors.extend(parse_fragment(path)[1])
    return errors


def refuse_unfolded(root: Path = ROOT) -> None:
    """Raise when a release would ship notes that miss unfolded fragments."""
    names = [p.name for p in fragment_paths(root)]
    if names:
        raise ValueError(
            f"Unfolded changelog fragments in {FRAGMENT_DIR}/: {', '.join(names)}. "
            "Run `python3 tools/packaging/changelog_fragments.py --fold <version>` "
            "in the release preparation and commit the result before releasing."
        )


def _grouped(paths: list[Path]) -> list[str]:
    """One ``### <kind>`` block per kind that has bullets, in ``KINDS`` order."""
    grouped: dict[str, list[str]] = {kind: [] for kind in KINDS}
    for path in paths:
        for kind, bullets in parse_fragment(path)[0].items():
            grouped[kind].extend(bullets)
    return [
        f"### {kind}\n\n" + "\n".join(grouped[kind]) for kind in KINDS if grouped[kind]
    ]


def fold(root: Path, version: str, date: str) -> str:
    """Fold the fragments into ``CHANGELOG.md``; returns the new section's text."""
    if not VERSION.match(version):
        raise ValueError(f"Not a release version: {version!r}")
    errors = check(root)
    if errors:
        raise ValueError("Fragments are not sound:\n" + "\n".join(errors))
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.M):
        raise ValueError(f"CHANGELOG.md already has a section for {version}")
    found = re.search(rf"^{re.escape(UNRELEASED)}[ \t]*\n", text, re.M)
    if found is None:
        raise ValueError(f"CHANGELOG.md has no '{UNRELEASED}' heading")
    rest = text[found.end() :]
    following = re.search(r"^## \[", rest, re.M)
    cut = following.start() if following else len(rest)
    legacy, older = rest[:cut].strip("\n"), rest[cut:]
    paths = fragment_paths(root)
    blocks = [b for b in [legacy] if b] + _grouped(paths)
    if not blocks:
        raise ValueError("Nothing to fold: no fragment and no Unreleased entry")
    section = f"## [{version}] — {date}\n\n" + "\n\n".join(blocks) + "\n"
    folded = f"{text[: found.end()]}\n{section}" + (f"\n{older}" if older else "")
    changelog.write_text(folded, "utf-8")
    for path in paths:
        path.unlink()
    return section


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate the fragments")
    mode.add_argument("--fold", metavar="VERSION", help="fold into CHANGELOG.md")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.check:
            errors = check(args.root)
            for error in errors:
                print(error, file=sys.stderr)
            return 1 if errors else 0
        fold(args.root, args.fold, args.date)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
