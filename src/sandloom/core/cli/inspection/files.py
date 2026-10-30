"Contained stable reads and explicit file policy; no application imports."

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tempfile
from pathlib import Path

from sandloom.core.cli.inspection.model import Doc, InspectionError, sha

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


def safe(root: Path, locator: str) -> Path:
    path = Path(locator)
    if path.is_absolute() or ".." in path.parts:
        raise InspectionError("unsafe_path", "Use a contained project-relative path")
    target = root / path
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


def read(root: Path, locator: str) -> str:
    path = safe(root, locator)
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
            text = content.decode("utf-8")
            if "\x00" in text or _SECRET.search(text):
                raise InspectionError(
                    "sensitive_content", f"Content cannot be exported: {locator}"
                )
            return text
    raise InspectionError(
        "source_changing", f"Source changed during capture: {locator}"
    )


def candidates(root: Path, directory: str) -> list[str]:
    base = safe(root, directory)
    result = []
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if any(part in _SKIP or part.startswith(".") for part in relative.parts):
            continue
        if path.suffix in {".py", ".md"} and path.is_file():
            safe(root, path.relative_to(root).as_posix())
            result.append(path.relative_to(root).as_posix())
    return result


def instructions(root: Path, directory: str, role: str | None = None) -> list[str]:
    current = safe(root, directory)
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
    path = safe(root, locator)
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


def atomic(
    root: Path, locator: str, text: str, *, force: bool, protected: list[str]
) -> None:
    target = safe(root, locator)
    if target in {safe(root, p) for p in protected} or any(
        locator == p or target.is_relative_to(root / p)
        for p in protected
        if (root / p).is_dir()
    ):
        raise InspectionError(
            "unsafe_output", "Output cannot replace a source or managed input"
        )
    if target.exists() and not force:
        raise InspectionError(
            "output_exists",
            "Output exists; choose another path or explicitly use --force",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=".inspection-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        # Recheck containment immediately before publishing the completed artifact.
        safe(root, locator)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def file_record(root: Path, path: str, *, reason: str) -> Doc:
    content = read(root, path)
    return {
        "id": f"file:{path}",
        "path": path,
        "content": content,
        "sha256": sha(content.encode()),
        "reason": reason,
    }


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
