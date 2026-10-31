"Contained stable reads and explicit file policy; no application imports."

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tempfile
from pathlib import Path

from sandloom.core.cli.inspection.model import InspectionError

MAX_FILE = 2 * 1024 * 1024
_SKIP = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    ".sandloom",
    ".pytest_cache",
}
_SECRET = re.compile(
    r"(?:-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{40,})\b)"
)
_SENSITIVE = {".env", "credentials", "secrets", "id_rsa", "id_ed25519"}


def contained(root: Path, locator: str) -> Path:
    """Resolve owned source metadata without imposing export-content policy."""
    path = Path(locator)
    if path.is_absolute() or ".." in path.parts:
        raise InspectionError("unsafe_path", "Use a contained project-relative path")
    target = root / path
    cursor = root
    for part in path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise InspectionError(
                "unsafe_path", "Inspection paths cannot traverse symlinks"
            )
    if not target.resolve().is_relative_to(root.resolve()):
        raise InspectionError("unsafe_path", "Path is outside the project")
    return target


def safe(root: Path, locator: str) -> Path:
    target = contained(root, locator)
    path = Path(locator)
    for part in path.parts:
        if (
            part in _SENSITIVE
            or part.startswith(".env")
            or Path(part).suffix.lower()
            in {".pem", ".key", ".p12", ".sqlite", ".db", ".log", ".dump"}
        ):
            raise InspectionError(
                "unsafe_path", "Sensitive paths cannot enter inspection output"
            )
    return target


def read(root: Path, locator: str) -> str:
    return checked_content(_read(safe(root, locator), locator), locator)


def source(root: Path, locator: str) -> str:
    """Bounded source read for metadata, never a source-body export."""
    return _read(contained(root, locator), locator).decode("utf-8")


def _read(path: Path, locator: str) -> bytes:
    for _ in range(2):
        before = path.stat()
        if before.st_size > MAX_FILE:
            raise InspectionError(
                "file_too_large", f"File exceeds {MAX_FILE} bytes: {locator}"
            )
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE + 1)
        after = path.stat()
        if len(content) <= MAX_FILE and (
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
        ) == (after.st_ino, after.st_mtime_ns, after.st_size):
            return content
    raise InspectionError(
        "source_changing", f"Source changed during capture: {locator}"
    )


def checked_content(content: bytes, locator: str) -> str:
    text = content.decode("utf-8")
    if "\x00" in text or _SECRET.search(text):
        raise InspectionError(
            "sensitive_content", f"Content cannot be exported: {locator}"
        )
    return text


def candidates(root: Path, directory: str) -> list[str]:
    base = contained(root, directory)
    result = []
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if any(part in _SKIP or part.startswith(".") for part in relative.parts):
            continue
        if path.suffix in {".py", ".md"} and path.is_file():
            contained(root, path.relative_to(root).as_posix())
            result.append(path.relative_to(root).as_posix())
    return result


def instructions(root: Path, directory: str, role: str | None = None) -> list[str]:
    current = contained(root, directory)
    folders = [*reversed(current.relative_to(root).parents), current.relative_to(root)]
    paths = {
        str(folder / name)
        for folder in folders
        for name in ("AGENTS.md", "CLAUDE.md")
        if (root / folder / name).is_file()
    }
    if role and (root / role).is_file():
        paths.add(role)
    return sorted(paths)


def fingerprint(root: Path, locator: str) -> str:
    """Stable raw-byte identity, independent of export/content policies."""
    path = contained(root, locator)
    for _ in range(2):
        before = path.stat()
        hashed = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(131072), b""):
                hashed.update(chunk)
        after = path.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) == (
            after.st_ino,
            after.st_mtime_ns,
            after.st_size,
        ):
            return hashed.hexdigest()
    raise InspectionError(
        "source_changing", f"Source changed during capture: {locator}"
    )


def fingerprints(root: Path, paths: list[str]) -> dict[str, str]:
    return {p: fingerprint(root, p) for p in sorted(set(paths))}


def atomic(root: Path, locator: str, text: str) -> None:
    """Publish a framework-owned artifact atomically, through contained paths."""
    target = safe(root, locator)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=".inspection-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        safe(root, locator)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def ignored(root: Path, locator: str) -> bool:
    policy = root / ".sandloomignore"
    if not policy.is_file():
        return False
    patterns = [
        line.strip()
        for line in read(root, ".sandloomignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if any(pattern.startswith("!") for pattern in patterns):
        raise InspectionError(
            "unsupported_ignore", ".sandloomignore does not support negation"
        )
    return any(fnmatch.fnmatchcase(locator, pattern) for pattern in patterns)
