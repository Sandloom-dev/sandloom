# syntax=docker/dockerfile:1.7
# Production image for projecthub (mirrors the Sandloom framework image).
# ---------- Stage 1: dependency build ----------
FROM python:3.11-slim-trixie AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY uv.loc[k] ./
# Install the LOCKED dependency set when uv.lock exists (reproducible builds);
# otherwise resolve from pyproject.toml.
# Without a lock, resolve published packages rather than the monorepo-only
# development sources outside this example's Docker build context.
RUN if [ -f uv.lock ]; then \
        uv export --frozen --no-dev --no-emit-project --output-file requirements.txt; \
    else \
        uv pip compile pyproject.toml --no-sources --output-file requirements.txt; \
    fi \
    && uv pip install --system --no-deps -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim-trixie AS production
WORKDIR /app
ARG SITE=/usr/local/lib/python3.11/site-packages
COPY --from=builder ${SITE} ${SITE}
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . /app
ENV PYTHONUNBUFFERED=1 \
    GUNICORN_BIND=0.0.0.0:8000 \
    HEALTH_URL=http://localhost:8000/health/simple
# Install the project itself (deps already present), then drop root.
RUN python -m pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
RUN install -d -o appuser -g appuser /app/data
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
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os, urllib.request as r; \
r.urlopen(os.environ['HEALTH_URL'])" || exit 1
# Worker count honours the container CPU quota (default min(2*cpus+1, 4),
# WEB_CONCURRENCY overrides) and only one worker runs the outbox relay.
CMD ["gunicorn", "-c", "python:sandloom.core.cli.gunicorn_conf", "projecthub.asgi:app"]
