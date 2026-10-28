"""Isolated Docker release checks. Uses only Python's standard library on the host."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
PROJECT = "sandloom-release-qualification"


def compose(*args: str, **kwargs):
    # Preserve the selected production/proxy configuration during fault recovery.
    # Paths are supplied by the operator, never inferred from unrelated stacks.
    extra = []
    for envfile in os.environ.get("SANDLOOM_QUALIFICATION_ENV_FILES", "").split(os.pathsep):
        if envfile:
            extra.extend(["--env-file", str(Path(envfile).resolve())])
    for override in os.environ.get("SANDLOOM_QUALIFICATION_COMPOSE_FILES", "").split(
        os.pathsep
    ):
        if override:
            extra.extend(["-f", str(Path(override).resolve())])
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "--env-file",
            str(STATE / ".env"),
            "-f",
            str(ROOT / "compose.yml"),
            *extra,
            *args,
        ],
        check=True,
        **kwargs,
    )


def prepare(*, no_build: bool = False) -> None:
    STATE.mkdir(mode=0o700, exist_ok=True)
    envfile = STATE / ".env"
    if not envfile.exists():
        values = {
            key: secrets.token_hex(32)
            for key in (
                "JWT_SECRET",
                "TWOFA_ENCRYPTION_KEY",
                "INTERNAL_API_TOKEN",
                "DB_PASSWORD",
                "APP_PASSWORD",
            )
        }
        fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("".join(f"{k}={v}\n" for k, v in values.items()))
    compose("up", "-d", "--no-build" if no_build else "--build", "app")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:18222/ready", timeout=3
            ) as response:
                if response.status == 200:
                    print("Strict Docker deployment ready", flush=True)
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    compose("logs", "--tail", "100", "app")
    raise RuntimeError("deployment did not become ready")


def token(tenant: str = "5e000000-0000-4000-8000-00000000000a") -> str:
    # Mint only synthetic qualification users; signing material stays inside Docker.
    tenant = str(UUID(tenant))
    return compose(
        "exec",
        "-T",
        "app",
        "python",
        "-c",
        f"from benchsuite.s5 import mint; print(mint({tenant!r}))",
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "token", "down", "status"])
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="prepare using an already built/loaded qualification image",
    )
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(no_build=args.no_build)
    elif args.action == "token":
        print(token())
    elif args.action == "down":
        compose("--profile", "restore", "down")  # Retain volumes and backups.
    else:
        compose("ps")


if __name__ == "__main__":
    main()
