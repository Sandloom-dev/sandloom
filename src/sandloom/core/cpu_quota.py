"""The CPUs this process may use: the cgroup quota, else the affinity / host count.

Sizes the gunicorn web workers (:mod:`sandloom.core.cli.gunicorn_conf`) and the
Celery prefork children (:mod:`sandloom.core.worker`): a container's CPU quota,
never the host's core count (a 0.5-CPU container on a 10-core host must not
fork 10 children). Stdlib only.
"""

from __future__ import annotations

import os

_CGROUP_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def cgroup_cpu_limit(
    v2: str | None = None, v1_quota: str | None = None, v1_period: str | None = None
) -> float | None:
    """CPU limit from the cgroup quota, or ``None`` when unlimited/unknown."""
    raw = v2 if v2 is not None else _read(_CGROUP_V2_CPU_MAX)
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    return quota / period
            except ValueError:
                pass
        if parts and parts[0] == "max":
            return None
    quota_raw = v1_quota if v1_quota is not None else _read(_CGROUP_V1_QUOTA)
    period_raw = v1_period if v1_period is not None else _read(_CGROUP_V1_PERIOD)
    try:
        quota = int(quota_raw or "-1")
        period = int(period_raw or "0")
    except ValueError:
        return None
    if quota > 0 and period > 0:
        return quota / period
    return None


def available_cpus() -> float:
    limit = cgroup_cpu_limit()
    affinity = getattr(os, "sched_getaffinity", None)  # Linux only
    try:
        host = float(len(affinity(0))) if affinity else float(os.cpu_count() or 1)
    except OSError:
        host = float(os.cpu_count() or 1)
    if limit is None:
        return host
    return min(limit, host)


__all__ = ["available_cpus", "cgroup_cpu_limit"]
