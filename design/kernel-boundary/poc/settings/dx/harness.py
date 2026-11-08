"""Boot the app in a fresh interpreter: (exit code, last stderr line)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/boot_checks_test",
    "CACHE_URL": "redis://localhost:6379/3",
    "BROKER_URL": "redis://localhost:6379/4",
    "JWT_SECRET": "boot-checks-not-a-real-secret-0123456789",
    "EVENT_BUS_BACKEND": "memory",
    "SANDLOOM_MODULES": '["catalog"]',
}
PROD = {
    **BASE,
    "ENVIRONMENT": "prod",
    "JWT_SECRET": "9f1c4e7a2b8d6035e1f94c7ab23d58e06f7b1a9c4d2e85f3071b6c9ae4d2f815",
}


def boot(extra: dict[str, str], *, prod: bool = False) -> tuple[int, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env.update(PROD if prod else BASE)
    env.update(extra)
    env["PYTHONPATH"] = str(HERE)
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-c", "import sandloom.main"],
            cwd=tmp, env=env, capture_output=True, text=True, check=False,
        )
    lines = [l for l in proc.stderr.strip().splitlines() if l.strip()]
    return proc.returncode, (lines[-1] if lines else "")


def app():  # in-process app for route tests (dev env)
    for k, v in BASE.items():
        os.environ.setdefault(k, v)
    import sandloom.main

    return sandloom.main.app


__all__ = ["boot", "app", "json"]
