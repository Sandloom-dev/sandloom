"""Docker Compose, always with the repository's project, env file and the
CLI's own copy of the compose file (never ``--remove-orphans``)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sandloom.core.cli.dev import process as _process


class DockerUnavailable(RuntimeError):
    """``docker`` is missing or its daemon does not answer."""


def docker_host(environ: Mapping[str, str]) -> str:
    """The host the published ports are reached on: ``DOCKER_HOST``'s host
    for a remote engine (``tcp://`` / ``ssh://``), else ``127.0.0.1``."""
    raw = (environ.get("DOCKER_HOST") or "").strip()
    if raw.startswith(("tcp://", "ssh://", "http://", "https://")):
        host = urlsplit(raw).hostname
        if host and host not in {"localhost", "127.0.0.1", "::1"}:
            return host
    return "127.0.0.1"


def check_docker(environ: Mapping[str, str]) -> None:
    """Raise :class:`DockerUnavailable` with one line when Docker is unusable."""
    if shutil.which("docker") is None:
        raise DockerUnavailable("the docker command is not installed")
    proc = _process.run(["docker", "compose", "version"], env=environ)
    if proc.returncode != 0:
        raise DockerUnavailable("`docker compose` is not available")
    proc = _process.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"], env=environ
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise DockerUnavailable(
            "the Docker daemon is not reachable"
            + (f" ({detail[-1]})" if detail else "")
        )


@dataclass
class Compose:
    project: str
    compose_file: Path
    env_file: Path
    environ: Mapping[str, str]

    def argv(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            self.project,
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.compose_file),
            *args,
        ]

    def run(
        self, *args: str, capture: bool = False, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        proc = _process.run(self.argv(*args), env=self.environ, capture=capture)
        if check and proc.returncode != 0:
            detail = failure_reason((proc.stderr or "") + "\n" + (proc.stdout or ""))
            raise RuntimeError(
                f"`docker compose {' '.join(args)}` failed (exit {proc.returncode})"
                + (f": {detail}" if detail else "")
            )
        return proc

    def up(
        self, services: Sequence[str] = (), *, force_recreate: bool = False
    ) -> subprocess.CompletedProcess[str]:
        args = ["up", "-d", "--wait", "--pull", "missing"]
        if force_recreate:
            args += ["--force-recreate", "--no-deps"]
        return self.run(*args, *services, capture=True)

    def states(self) -> dict[str, str]:
        """``{service: state}`` of this project's containers (all, stopped too)."""
        proc = self.run("ps", "-a", "--format", "json", capture=True)
        if proc.returncode != 0:
            return {}
        out: dict[str, str] = {}
        text = proc.stdout.strip()
        rows: list[dict[str, object]] = []
        if text.startswith("["):
            rows = json.loads(text)
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        for row in rows:
            service = str(row.get("Service") or "")
            if service:
                out[service] = str(row.get("State") or "")
        return out

    def published_port(self, service: str, container_port: int) -> int | None:
        proc = self.run("port", service, str(container_port), capture=True)
        if proc.returncode != 0:
            return None
        text = proc.stdout.strip().splitlines()
        if not text:
            return None
        _, _, port = text[-1].rpartition(":")
        return int(port) if port.isdigit() and int(port) > 0 else None

    def images(self) -> list[str]:
        proc = self.run("config", "--images", capture=True)
        return proc.stdout.split() if proc.returncode == 0 else []


def failure_reason(output: str) -> str:
    """One line for a failed Docker command: the line that names the error
    with the lines it wraps onto (Docker breaks long errors, so the last line
    alone can be ``(exit status 4))``)."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    starts = [i for i, line in enumerate(lines) if line.lower().startswith("error")]
    mentions = [
        i
        for i, line in enumerate(lines)
        if "error" in line.lower() or "failed" in line.lower()
    ]
    start = (starts or mentions or [len(lines) - 1])[-1]
    reason = " ".join(lines[start:])
    if len(reason) > 400:
        reason = reason[:397] + "..."
    return reason


def missing_images(images: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    out: list[str] = []
    for image in dict.fromkeys(images):  # dev and test services share images
        proc = _process.run(["docker", "image", "inspect", image], env=environ)
        if proc.returncode != 0:
            out.append(image)
    return out


def pull(images: Sequence[str], environ: Mapping[str, str]) -> None:
    """Pull quietly: a failed pull is retried by ``up --pull missing``, which
    reports it."""
    for image in images:
        _process.run(["docker", "pull", "--quiet", image], env=environ)


def legacy_projects(name: str, environ: Mapping[str, str]) -> list[str]:
    """Working directories of containers of compose project ``name``."""
    proc = _process.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={name}",
            "--format",
            '{{.Label "com.docker.compose.project.working_dir"}}',
        ],
        env=environ,
    )
    if proc.returncode != 0:
        return []
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


__all__ = [
    "Compose",
    "DockerUnavailable",
    "check_docker",
    "docker_host",
    "failure_reason",
    "legacy_projects",
    "missing_images",
    "pull",
]
