# syntax=docker/dockerfile:1.7
# Production image for tasklist (mirrors the Sandloom framework image): a virtualenv
# (/opt/venv) built by uv, byte-compiled, copied into a clean runtime with no
# uv / pip, running as a non-root user.
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
FROM ghcr.io/astral-sh/uv:0.13.4 AS uv

# ---------- Stage 1: build the virtualenv ----------
FROM ${PYTHON_IMAGE} AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml ./
COPY uv.loc[k] ./
# Dependencies first (cached until pyproject.toml / uv.lock change): the
# LOCKED set when uv.lock exists (reproducible), else resolved from
# pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
    && if [ -f uv.lock ]; then \
        uv export --frozen --no-dev --extra fast --no-emit-project \
            --output-file requirements.txt; \
    else \
        uv pip compile pyproject.toml --extra fast --output-file requirements.txt; \
    fi \
    && uv pip install --python /opt/venv/bin/python --no-deps -r requirements.txt
# Then the project itself; only its own package is byte-compiled here.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=0 uv pip install --python /opt/venv/bin/python --no-deps . \
    && /opt/venv/bin/python -m compileall -q -j 0 \
        /opt/venv/lib/python3*/site-packages/tasklist

# ---------- Stage 2: runtime ----------
FROM ${PYTHON_IMAGE} AS production
RUN python -m pip uninstall -y -q pip setuptools wheel 2>/dev/null || true; \
    useradd --create-home --uid 10001 appuser \
    && install -d -o appuser -g appuser /app /app/data
COPY --from=builder /opt/venv /opt/venv
RUN python - <<'HARDEN'
"""Build-time hardening of the Debian runtime; never removes essential packages."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ADMIN_PACKAGES = frozenset({"apt", "libapt-pkg7.0", "mount", "passwd", "adduser"})


def packages_to_remove(records: str) -> list[str]:
    """Select only allowlisted administration packages, rejecting protected ones."""
    selected = []
    for line in records.splitlines():
        package, essential, protected = line.split("\t")
        name = package.partition(":")[0]
        if name not in ADMIN_PACKAGES:
            continue
        if essential == "yes" or protected == "yes":
            raise ValueError(f"Refusing essential or protected package: {package}")
        selected.append(name)
    return selected


def strip_privileged_bits(roots: tuple[Path, ...]) -> None:
    """Remove privilege bits from regular files, preserving ordinary execution."""
    for root in roots:
        for directory, _, files in os.walk(root, followlinks=False):
            for name in files:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
                    path.chmod(stat.S_IMODE(mode) & ~(stat.S_ISUID | stat.S_ISGID))


def main() -> None:
    """Minimize the reviewed Debian base without bypassing dpkg safety guards."""
    records = subprocess.check_output(
        [
            "/usr/bin/dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Essential}\t${Protected}\n",
        ],
        text=True,
    )
    packages = packages_to_remove(records)
    if set(packages) != ADMIN_PACKAGES:
        raise ValueError("Review hardening before changing the Debian base")
    if packages:
        # dpkg respects dependency and essential/protected guards. No force
        # flags or autoremove: native Python extensions can need libstdc++.
        subprocess.run(
            [
                "/usr/bin/dpkg",
                "--purge",
                "apt",
                "libapt-pkg7.0",
                "mount",
                "adduser",
                "passwd",
            ],
            check=True,
        )
    strip_privileged_bits((Path("/usr"), Path("/opt")))
    print("Runtime admin packages removed:", ", ".join(packages))


if __name__ == "__main__":
    main()
HARDEN
WORKDIR /app
COPY --chown=appuser:appuser . /app
ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GUNICORN_BIND=0.0.0.0:8000 \
    HEALTH_URL=http://localhost:8000/health/simple
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os, urllib.request as r; \
r.urlopen(os.environ['HEALTH_URL'])" || exit 1
# Worker count honours the container CPU quota (default min(2*cpus+1, 4),
# WEB_CONCURRENCY overrides), uvicorn workers run uvloop + httptools,
# GUNICORN_KEEPALIVE (default 75 s) must exceed the load balancer's idle
# timeout, and only one worker runs the outbox relay.
CMD ["gunicorn", "-c", "python:sandloom.core.cli.gunicorn_conf", "tasklist.asgi:app"]
