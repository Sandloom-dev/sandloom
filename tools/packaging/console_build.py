"""The packaged admin console must be the build of the current console source.

``admin/scripts/package-console.mjs`` copies the build into
``src/sandloom_admin/console`` and records this hash in ``console.json``. Building
needs Node; checking and packaging don't, so a Python contributor and the release
build get the console without it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMIN = ROOT / "admin"
CONSOLE = ROOT / "src/sandloom_admin/console"
_ROOT_INPUTS = ("index.html", "package-lock.json", "vite.config.ts", "tsconfig.json")
_PER_APPLICATION = {"public/admin-manifest.json", "public/config.json"}
_NOT_BUILT = re.compile(r"\.(test|stories)\.[a-z]+$")


def _inputs() -> list[str]:
    names = list(_ROOT_INPUTS)
    for directory in ("src", "public"):
        names += [
            path.relative_to(ADMIN).as_posix()
            for path in (ADMIN / directory).rglob("*")
            if path.is_file()
        ]
    return sorted(
        name
        for name in names
        if not _NOT_BUILT.search(name)
        and not name.endswith(".DS_Store")
        and name not in _PER_APPLICATION
    )


def source_hash() -> str:
    digest = hashlib.sha256()
    for name in _inputs():
        digest.update(f"{name}\n".encode())
        digest.update((ADMIN / name).read_bytes())
    return digest.hexdigest()


def recorded_hash() -> str:
    described = json.loads((CONSOLE / "console.json").read_text())
    return str(described["source_sha256"])
