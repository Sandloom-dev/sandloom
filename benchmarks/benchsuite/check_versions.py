"""Fairness guard: the shared stack must match the framework's lock exactly.

Fails (exit 1) when an installed package that Sandloom and the bare implementations
share differs from ``uv.lock``, or when the bare apps' revocation key
layout drifted from Sandloom's. Run in CI before measuring:

    python -m benchsuite.check_versions
"""

from __future__ import annotations

import os
import sys
import tomllib
from importlib import metadata
from pathlib import Path

SHARED = (
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic-core",
    "uvicorn",
    "uvloop",
    "httptools",
    "h11",
    "sqlalchemy",
    "asyncpg",
    "redis",
    "hiredis",
    "pyjwt",
    "orjson",
    "greenlet",
)
BACKEND_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"


def locked_versions(lock: Path = BACKEND_LOCK) -> dict[str, str]:
    data = tomllib.loads(lock.read_text())
    return {p["name"]: p["version"] for p in data.get("package", [])}


def main() -> int:
    locked = locked_versions()
    problems = []
    for name in SHARED:
        want = locked.get(name)
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = None
        if want is None:
            continue  # not part of the framework's lock (platform-specific)
        if have != want:
            problems.append(f"{name}: installed {have}, uv.lock has {want}")
        else:
            print(f"ok  {name}=={have}")

    # The revocation key the bare apps read must be Sandloom's.
    from benchsuite import common

    os.environ.setdefault("ENVIRONMENT", "dev")
    os.environ.setdefault("DATABASE_URL", common.database_url())
    os.environ.setdefault("CACHE_URL", common.cache_url())
    os.environ.setdefault("BROKER_URL", common.broker_url())
    os.environ.setdefault("JWT_SECRET", common.jwt_secret())
    from sandloom.core.auth.marks import REVOCATION_KEY_PREFIX

    if REVOCATION_KEY_PREFIX != common.REVOCATION_KEY_PREFIX:
        problems.append(
            f"revocation key prefix drifted: Sandloom {REVOCATION_KEY_PREFIX!r} vs "
            f"bench {common.REVOCATION_KEY_PREFIX!r}"
        )
    for line in problems:
        print(f"MISMATCH {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
