"""Run-level guards, checked once before any test runs.

* **Ownership** (:mod:`sandloom.core.testing.ownership`): every database / Redis
  index the run will reset — under pytest-xdist each worker's own — belongs to
  this run (claimed if unmarked); another run's resource aborts the session.
  Redis is a harness family (:mod:`sandloom.core.testing.families`): its hooks
  allocate, cap and claim; without one the run is DB-only.
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
from urllib.parse import parse_qs, urlunsplit

import pytest

from sandloom.core.migrations.pg_sync import psycopg2
from sandloom.core.testing import connections, families, ownership, workers
from sandloom.core.testing.env import dev_up_hint
from sandloom.core.testing.families import INFRA_VAR, INSTANCE_URL_OPTIONS
from sandloom.core.testing.fixtures import env_starts, find_env_file
from sandloom.core.url_split import split_url

#: Where Linux's default ephemeral range starts; macOS and Windows start higher.
EPHEMERAL_PORTS_START = 32768


def _numprocesses(config: pytest.Config) -> int:
    value = getattr(config.option, "numprocesses", None)
    return value if isinstance(value, int) else 0


def run_values(config: pytest.Config) -> dict[str, str]:
    """The run's test URLs: ``.env.test`` + ``SANDLOOM_TEST_*`` (else the env)."""
    path = find_env_file(env_starts(config))
    values = workers.worker_env_values(path) if path is not None else {}
    # The settings a shell value may stand in for: DATABASE_URL. The cache and
    # broker overrides are merged into INFRA (``worker_env_values``), not copied.
    for var in workers.DATABASE_VARS:
        if not values.get(var) and os.environ.get(var):
            values[var] = os.environ[var]
    if not values.get(INFRA_VAR) and os.environ.get(INFRA_VAR):
        values[INFRA_VAR] = os.environ[INFRA_VAR]
    return values


def worker_targets(values: dict[str, str], count: int) -> list[dict[str, str]]:
    """``values`` as each of ``count`` xdist workers rewrites it."""
    if count <= 0:
        return [values]
    return [
        {
            **values,
            **workers.rewrite_for_worker(
                values, {**os.environ, workers.WORKER_ENV: f"gw{n}"}
            ),
        }
        for n in range(count)
    ]


def allocate_worker_resources(values: dict[str, str], count: int) -> None:
    """Every harness family claims its resources for the ``count`` workers
    (the Redis family, under ``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``: free
    indexes, published in ``SANDLOOM_TEST_REDIS_WORKER_DBS``).

    Runs in the controller's ``pytest_sessionstart`` (``tryfirst``), before
    xdist spawns its workers, which inherit the environment.
    """
    for family in families.harness_families():
        family.allocate_workers(values, count)


def self_connecting_urls(values: Mapping[str, str]) -> list[str]:
    """URLs naming ``localhost`` with a port in the OS's ephemeral range.

    A service published on one loopback address leaves the other with no listener.
    ``localhost`` tries both, and a connection to a dead port in the ephemeral range
    can be given that same port as its source: TCP then connects the socket to
    itself, and a client reads its own request back as the reply. It takes thousands
    of connections, which a test run makes, so it looks like a random failure.
    """
    found = []
    for var, url in _run_urls(values):
        parts = split_url(url)
        hosts = parse_qs(parts.query).get("host", [])
        if hosts and all(host.startswith("/") for host in hosts):
            continue  # a Unix socket: the authority is unused
        if parts.hostname == "localhost" and (parts.port or 0) >= EPHEMERAL_PORTS_START:
            found.append(f"{var} (localhost:{parts.port})")
    return found


def _run_urls(values: Mapping[str, str]) -> list[tuple[str, str]]:
    """Every service URL of the run as ``(name, url)``: ``DATABASE_URL`` and
    each ``url`` / ``broker`` / ``backend`` option of an ``INFRA`` instance
    (named ``capability.instance.option``)."""
    urls = [("DATABASE_URL", values.get("DATABASE_URL") or "")]
    for capability, instances in families.parse_infra(values.get(INFRA_VAR)).items():
        for name, spec in instances.items():
            urls += [
                (f"{capability}.{name}.{option}", spec[option])
                for option in INSTANCE_URL_OPTIONS
                if isinstance(spec.get(option), str)
            ]
    return urls


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
    _warn_unreachable(config, database)
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
        if count:
            allocate_worker_resources(values, count)
        for target in worker_targets(values, count):
            ownership.claim_test_resources(target)
    except ownership.TestResourceOwnershipError as exc:
        hint = dev_up_hint(env_starts(config))
        message = f"{exc} Or: {hint}." if hint else str(exc)
        pytest.exit(message, returncode=pytest.ExitCode.USAGE_ERROR)
    if database and not count:
        _prepare_serial_database(database)


def _server_reachable(database_url: str) -> bool:
    """Whether the PostgreSQL server of ``database_url`` accepts a connection
    to its ``postgres`` database (a refused login counts as reachable)."""
    parts = split_url(database_url)
    dsn = urlunsplit(parts._replace(scheme="postgresql", path="/postgres", query=""))
    try:
        psycopg2.connect(dsn, connect_timeout=5).close()
    except psycopg2.OperationalError as exc:
        text = str(exc)
        return "authentication" in text or "does not exist" in text
    return True


def _warn_unreachable(config: pytest.Config, database: str) -> None:
    """The test server does not answer and the checkout has no
    ``.env.test.local``: say how to get this checkout's own servers (the
    database tests then skip or fail on their own)."""
    if not database.startswith("postgresql"):
        return
    hint = dev_up_hint(env_starts(config))
    if hint is None:
        return
    if _server_reachable(database):
        return
    parts = split_url(database)
    print(
        f"sandloom testing: the test database server {parts.hostname}:"
        f"{parts.port or 5432} is not reachable; {hint}",
        file=sys.stderr,
    )


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
    by each harness family, to what its backend has room for (the Redis family,
    with ``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``: the free Redis indexes).

    A shortage runs fewer workers (0: serially) with a warning instead of
    aborting the session: an explicit ``-n N`` still fails with the full
    diagnosis.
    """
    if not isinstance(requested, int) or requested <= 1:
        return requested
    values = run_values(config)
    database = values.get("DATABASE_URL") or ""
    count = connections.affordable_workers(requested, database)
    for family in families.harness_families():
        if count <= 1:
            break
        count = family.cap_workers(values, count)
    return count


__all__ = [
    "cap_auto_workers",
    "check_run_resources",
    "run_values",
    "worker_targets",
]
