"""Run-level guards, checked once before any test runs.

* **Ownership** (:mod:`sandloom.core.testing.ownership`): every database / Redis
  index the run will reset — under pytest-xdist each worker's own — belongs to
  this run (claimed if unmarked); another run's resource aborts the session.
* **Connection budget** (:mod:`sandloom.core.testing.connections`): the server has
  room for the run's pools (x workers); ``-n auto`` is capped to it.
* **Self-connection** (:func:`self_connecting_urls`): no URL can reach itself instead
  of the service.

Both are wired by the ``sandloom.core.testing`` plugin (and the framework suite's
root conftest): :func:`check_run_resources` from ``pytest_sessionstart`` on
the controller / a plain run (workers are covered by it), and
:func:`cap_auto_workers` from xdist's ``pytest_xdist_auto_num_workers``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg2  # type: ignore[import-untyped]
import pytest
import redis

from sandloom.core.testing import connections, ownership, resources, workers
from sandloom.core.testing.fixtures import env_starts, find_env_file

#: Where Linux's default ephemeral range starts; macOS and Windows start higher.
EPHEMERAL_PORTS_START = 32768


def _numprocesses(config: pytest.Config) -> int:
    value = getattr(config.option, "numprocesses", None)
    return value if isinstance(value, int) else 0


def run_values(config: pytest.Config) -> dict[str, str]:
    """The run's test URLs: ``.env.test`` + ``SANDLOOM_TEST_*`` (else the env)."""
    path = find_env_file(env_starts(config))
    values = workers.worker_env_values(path) if path is not None else {}
    for var in workers.OVERRIDABLE_VARS:
        if not values.get(var) and os.environ.get(var):
            values[var] = os.environ[var]
    return values


def worker_targets(values: dict[str, str], count: int) -> list[dict[str, str]]:
    """``values`` as each of ``count`` xdist workers rewrites it."""
    if count <= 0:
        return [values]
    stride = os.environ.get(workers.REDIS_STRIDE_ENV, "")
    mapping = os.environ.get(workers.REDIS_MAP_ENV, "")
    out = []
    for n in range(count):
        environ = {
            workers.WORKER_ENV: f"gw{n}",
            workers.REDIS_STRIDE_ENV: stride,
            workers.REDIS_MAP_ENV: mapping,
        }
        out.append({**values, **workers.rewrite_for_worker(values, environ)})
    return out


def allocate_worker_redis(values: dict[str, str], count: int) -> None:
    """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``: claim free Redis indexes for the
    ``count`` workers and publish them in ``SANDLOOM_TEST_REDIS_WORKER_DBS``.

    Runs in the controller's ``pytest_sessionstart`` (``tryfirst``), before
    xdist spawns its workers, which inherit the environment.
    """
    urls = _redis_urls(values)
    if not urls:
        return
    mapping = resources.allocate_worker_redis(
        urls,
        count,
        ownership.current_owner(),
        spares=resources.REDIS_SPARES_PER_BASE,  # replacements of crashed workers
    )
    os.environ[workers.REDIS_MAP_ENV] = workers.format_redis_map(mapping)


def self_connecting_urls(values: Mapping[str, str]) -> list[str]:
    """URLs naming ``localhost`` with a port in the OS's ephemeral range.

    A service published on one loopback address leaves the other with no listener.
    ``localhost`` tries both, and a connection to a dead port in the ephemeral range
    can be given that same port as its source: TCP then connects the socket to
    itself, and a client reads its own request back as the reply. It takes thousands
    of connections, which a test run makes, so it looks like a random failure.
    """
    found = []
    for var in ("DATABASE_URL", "CACHE_URL", "BROKER_URL"):
        parts = urlsplit(values.get(var) or "")
        if "host" in parse_qs(parts.query):  # a Unix socket: the authority is unused
            continue
        if parts.hostname == "localhost" and (parts.port or 0) >= EPHEMERAL_PORTS_START:
            found.append(f"{var} (localhost:{parts.port})")
    return found


def check_run_resources(config: pytest.Config) -> None:
    """Ownership + connection budget of the whole run (controller only)."""
    if hasattr(config, "workerinput"):
        return  # an xdist worker: the controller checked for all of them
    # Both: the framework suite keeps a shell's values over the file's.
    risky = sorted(
        {*self_connecting_urls(run_values(config)), *self_connecting_urls(os.environ)}
    )
    if risky:
        pytest.exit(
            f"{', '.join(risky)} can connect to itself instead of the service, which "
            "shows up as random test failures. Use 127.0.0.1 in the URL, or a port "
            f"below {EPHEMERAL_PORTS_START}.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    # Pin the owner for this run and every process it starts (xdist workers,
    # e2e subprocesses): a test that chdirs must not change who "we" are.
    os.environ.setdefault(ownership.OWNER_ENV, ownership.current_owner())
    values = run_values(config)
    count = _numprocesses(config)
    # Budget first: a run that cannot start must not claim Redis indexes.
    database = values.get("DATABASE_URL") or ""
    if database:
        per_process = connections.process_connection_need()
        what = (
            f"this run ({count} xdist workers x {per_process})"
            if count
            else f"this run ({per_process} per process)"
        )
        try:
            connections.check_headroom(database, max(count, 1) * per_process, what)
        except connections.ConnectionBudgetError as exc:
            pytest.exit(str(exc), returncode=pytest.ExitCode.USAGE_ERROR)
    try:
        if count and workers.redis_auto():
            allocate_worker_redis(values, count)
        for target in worker_targets(values, count):
            ownership.claim_test_resources(target)
    except ownership.TestResourceOwnershipError as exc:
        pytest.exit(str(exc), returncode=pytest.ExitCode.USAGE_ERROR)
    if database and not count:
        _prepare_serial_database(database)


def _prepare_serial_database(database: str) -> None:
    """A serial run's database exists with the framework's extensions before
    the first test (an xdist worker's ``_gw<N>`` copy gets the same from
    ``workers.ensure_database``). Otherwise a test that creates the shared
    metadata itself (``metadata.create_all``: ``citext`` columns) passed only
    after a test whose ``db_engine`` had installed the extensions."""
    try:
        workers.ensure_database(database)
    except psycopg2.OperationalError:
        return  # server down: the database tests skip or fail on their own


def cap_auto_workers(config: pytest.Config, requested: Any) -> Any:
    """``-n auto``'s worker count capped to the server's connection room and,
    with ``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``, to the free Redis indexes.

    A shortage runs fewer workers (0: serially) with a warning instead of
    aborting the session: an explicit ``-n N`` still fails with the full
    diagnosis.
    """
    if not isinstance(requested, int) or requested <= 1:
        return requested
    values = run_values(config)
    database = values.get("DATABASE_URL") or ""
    count = connections.affordable_workers(requested, database)
    if count > 1 and workers.redis_auto():
        urls = _redis_urls(values)
        if urls:
            try:
                owner = os.environ.get(ownership.OWNER_ENV)
                room = resources.affordable_redis_workers(
                    urls, owner or ownership.current_owner()
                )
            except (redis.RedisError, OSError):  # reported again at session start
                return count
            if room < count:
                fallback = room if room > 1 else 0
                sys.stderr.write(
                    f"sandloom: only {room} xdist workers' worth of Redis database "
                    f"indexes are free (wanted {count}); running "
                    + (f"{fallback} workers" if fallback else "serially")
                    + ". Free stale ones: `uv run python "
                    "tools/checks/test_resources.py "
                    "prune --apply`.\n"
                )
                return fallback
    return count


def _redis_urls(values: dict[str, str]) -> list[str]:
    return [
        values[var]
        for var in workers.REDIS_VARS
        if (values.get(var) or "").startswith(("redis://", "rediss://"))
    ]


__all__ = [
    "cap_auto_workers",
    "check_run_resources",
    "run_values",
    "worker_targets",
]
