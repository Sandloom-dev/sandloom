"""Secrets never enter an application image (``sandloom new``, ``sandloom add deploy``).

The generators write secrets next to the code: ``.env`` (``sandloom new``),
``deploy/.env.microservices`` with every service's private keys (``sandloom keys
dev-env``), and key files (``sandloom keys generate --out-dir``: ``jwt_private_key``,
``rpc_private_key``, ``rpc_dh_private_key``). An app's build context is its
project root, so its ``.dockerignore`` must exclude them AT ANY DEPTH (a
Docker pattern without ``**/`` matches the context root only), and its
Dockerfile copies only what it builds.

:data:`SECRET_PATTERNS` is the block every generated ``.dockerignore`` ends
with; :func:`patch_dockerignore` appends the missing lines to an existing
app's file (``sandloom add deploy`` runs it); :func:`excluded` evaluates a
``.dockerignore`` with Docker's rules and :func:`secret_files` lists what a
build context (or an image's file tree) would still ship.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

#: The ``.dockerignore`` lines that keep every generated secret out of the
#: build context (last match wins: the example env file stays).
SECRET_PATTERNS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "!**/.env.example",
    "**/jwt_private_key",
    "**/rpc_private_key",
    "**/rpc_dh_private_key",
    "**/*.pem",
)
_HEADER = "# secrets never enter an image: env files and key files at any depth"

#: File names :func:`secret_files` reports (the generators' secret files).
_SECRET_NAMES = ("jwt_private_key", "rpc_private_key", "rpc_dh_private_key")


def dockerignore_block() -> str:
    """The secrets block of a generated ``.dockerignore``."""
    return "\n".join((_HEADER, *SECRET_PATTERNS)) + "\n"


def missing_patterns(text: str) -> list[str]:
    """The :data:`SECRET_PATTERNS` lines ``text`` lacks."""
    lines = {line.strip() for line in text.splitlines()}
    return [p for p in SECRET_PATTERNS if p not in lines]


def patch_dockerignore(root: Path) -> bool:
    """Append the secrets block to ``root/.dockerignore`` when any line of it
    is missing (creating the file); whether it changed the file."""
    path = root / ".dockerignore"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not missing_patterns(text):
        return False
    prefix = text if not text or text.endswith("\n") else text + "\n"
    path.write_text(
        prefix + ("\n" if prefix else "") + dockerignore_block(), encoding="utf-8"
    )
    return True


def _regex(pattern: str) -> re.Pattern[str]:
    """A Docker ``.dockerignore`` pattern as an anchored regex (``**`` any
    number of directories, ``*`` / ``?`` within one path segment)."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _rules(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line.lstrip("!").strip().strip("/")
        if line.startswith("./"):
            line = line[2:]
        if line:
            rules.append((negated, _regex(line)))
    return rules


def excluded(dockerignore: str, relpath: str) -> bool:
    """Docker's verdict for ``relpath`` (``/``-separated, relative to the
    context): the last matching pattern wins, and a pattern that matches a
    parent directory matches everything below it."""
    parts = relpath.strip("/").split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    verdict = False
    for negated, regex in _rules(dockerignore):
        if any(regex.match(c) for c in candidates):
            verdict = not negated
    return verdict


def is_secret(relpath: str) -> bool:
    """A generated secret file: an env file other than ``.env.example``, a
    private key file, or a PEM file."""
    name = relpath.rsplit("/", 1)[-1]
    if name == ".env.example":
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in _SECRET_NAMES
        or fnmatch.fnmatch(name, "*.pem")
    )


def _walk(root: Path) -> Iterator[str]:
    for path in sorted(root.rglob("*")):
        if path.is_file() or path.is_symlink():
            yield path.relative_to(root).as_posix()


def context_files(root: Path, files: Iterable[str] | None = None) -> list[str]:
    """The files of ``root`` its ``.dockerignore`` lets into a build."""
    ignore = root / ".dockerignore"
    text = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    return [f for f in (files or _walk(root)) if not excluded(text, f)]


def secret_files(root: Path) -> list[str]:
    """The secret files under ``root`` (a build context exported by Docker,
    or an image's file tree)."""
    return [f for f in _walk(root) if is_secret(f)]


__all__ = [
    "SECRET_PATTERNS",
    "context_files",
    "dockerignore_block",
    "excluded",
    "is_secret",
    "missing_patterns",
    "patch_dockerignore",
    "secret_files",
]
