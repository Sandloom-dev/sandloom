"""PostgreSQL connection budget of a test run.

One PostgreSQL server (``max_connections`` 100 by default) serves every test
run on a machine. A run that opens more connections than the server has left
fails mid-run with ``too many clients already`` — in a random test, possibly
in ANOTHER run. The harness therefore:

* sizes the pools of a test process small (:data:`TEST_POOL_PROFILE`, applied
  as defaults: an explicit environment value wins) — tests run one request at
  a time, they do not need the production defaults (10 + 20 request, 5 + 5
  contract connections per process);
* computes what a process can hold at most (:func:`process_connection_need`:
  request pool + contract pool + the harness's own test engine + a reserve
  for ad-hoc engines tests open) and what a run needs (x xdist workers);
* checks the server's headroom (``max_connections`` minus the reserved slots
  minus the connections open right now) BEFORE the run and fails fast with a
  clear message instead (:func:`check_headroom`);
* caps ``pytest -n auto`` to the workers the headroom affords
  (:func:`affordable_workers`), keeping :data:`SUBPROCESS_RESERVE` free for
  the multi-process suites (split conformance boots up to that many).

``SANDLOOM_TEST_SKIP_CONNECTION_CHECK=1`` disables the check (not the profile).

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

SKIP_ENV = "SANDLOOM_TEST_SKIP_CONNECTION_CHECK"

#: The production defaults of the pool settings (``sandloom.core.config.settings``;
#: mirrored here so this module never imports the settings — a test pins them).
SETTINGS_DEFAULTS: dict[str, int] = {
    "DB_POOL_SIZE": 10,
    "DB_MAX_OVERFLOW": 20,
    "CONTRACT_DB_POOL_SIZE": 5,
    "CONTRACT_DB_MAX_OVERFLOW": 5,
}
#: Pool sizing of a test process (defaults; explicit environment wins).
#: ``DB_POOL_OVERFLOW_LINGER_SECONDS=0``: an overflow connection is closed as
#: soon as it is returned instead of idling for 30 s.
TEST_POOL_PROFILE: dict[str, str] = {
    "DB_POOL_SIZE": "2",
    "DB_MAX_OVERFLOW": "3",
    "CONTRACT_DB_POOL_SIZE": "1",
    "CONTRACT_DB_MAX_OVERFLOW": "1",
    "DB_POOL_OVERFLOW_LINGER_SECONDS": "0",
}
#: The harness's own test engine (``db_engine`` / ``open_test_engine``):
#: ``pool_size + max_overflow`` of ``build_disposable_engine`` in tests.
TEST_ENGINE_POOL_SIZE = 2
TEST_ENGINE_MAX_OVERFLOW = 2
#: Ad-hoc engines tests open themselves (scratch databases, admin work).
AD_HOC_RESERVE = 2
#: Kept free for the multi-process suites when sizing ``-n auto``.
SUBPROCESS_RESERVE = 40
#: What ONE multi-process suite (split conformance) may hold at once.
TARGET_RUN_CONNECTIONS = 40


class ConnectionBudgetError(RuntimeError):
    """The PostgreSQL server cannot take the connections a run needs."""


def apply_test_pool_profile(
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Set the :data:`TEST_POOL_PROFILE` values that are not set yet."""
    env = os.environ if environ is None else environ
    applied = {}
    for key, value in TEST_POOL_PROFILE.items():
        if not env.get(key):
            env[key] = value
            applied[key] = value
    return applied


def pool_connections(env: Mapping[str, str]) -> int:
    """Request pool + contract pool of one app process, at their maximum."""
    total = 0
    for key, default in SETTINGS_DEFAULTS.items():
        raw = (env.get(key) or "").strip()
        total += int(raw) if raw else default
    return total


def process_connection_need(env: Mapping[str, str] | None = None) -> int:
    """Connections one test process can hold at most."""
    values = os.environ if env is None else env
    return (
        pool_connections(values)
        + TEST_ENGINE_POOL_SIZE
        + TEST_ENGINE_MAX_OVERFLOW
        + AD_HOC_RESERVE
    )


@dataclass(frozen=True)
class Headroom:
    max_connections: int
    reserved: int
    in_use: int

    @property
    def free(self) -> int:
        return self.max_connections - self.reserved - self.in_use


def server_headroom(database_url: str) -> Headroom | None:
    """The server's connection headroom (``None``: not PostgreSQL / down)."""
    parts = urlsplit(database_url)
    if not parts.scheme.startswith("postgresql"):
        return None
    import psycopg2  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the hook / call that needs it

    dsn = urlunsplit(parts._replace(scheme="postgresql", path="/postgres", query=""))
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_setting('max_connections')::int, "
                "current_setting('superuser_reserved_connections')::int "
                "+ coalesce(current_setting('reserved_connections', true), '0')"
                "::int, (SELECT count(*) FROM pg_stat_activity "
                "WHERE backend_type = 'client backend')"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    # The probe's own connection is gone again: do not count it.
    return Headroom(int(row[0]), int(row[1]), max(0, int(row[2]) - 1))


def check_headroom(
    database_url: str,
    need: int,
    what: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> Headroom | None:
    """Raise :class:`ConnectionBudgetError` unless ``need`` connections fit."""
    env = os.environ if environ is None else environ
    if (env.get(SKIP_ENV) or "").strip().lower() in {"1", "true", "yes"}:
        return None
    room = server_headroom(database_url)
    if room is None or need <= room.free:
        return room
    host = urlsplit(database_url).hostname
    raise ConnectionBudgetError(
        f"{what} needs up to {need} PostgreSQL connections, but the server at "
        f"{host} has only {room.free} free (max_connections "
        f"{room.max_connections}, {room.reserved} reserved, {room.in_use} in "
        f"use — another test run?). Wait for the other run, use fewer xdist "
        f"workers (-n), or set {SKIP_ENV}=1 to run anyway."
    )


def affordable_workers(
    requested: int, database_url: str, env: Mapping[str, str] | None = None
) -> int:
    """``requested`` xdist workers capped to what the server can take."""
    room = server_headroom(database_url) if database_url else None
    if room is None:
        return requested
    per_worker = process_connection_need(env)
    fits = (room.free - SUBPROCESS_RESERVE) // max(1, per_worker)
    return max(1, min(requested, fits))


__all__ = [
    "AD_HOC_RESERVE",
    "ConnectionBudgetError",
    "Headroom",
    "SETTINGS_DEFAULTS",
    "SKIP_ENV",
    "SUBPROCESS_RESERVE",
    "TARGET_RUN_CONNECTIONS",
    "TEST_ENGINE_MAX_OVERFLOW",
    "TEST_ENGINE_POOL_SIZE",
    "TEST_POOL_PROFILE",
    "affordable_workers",
    "apply_test_pool_profile",
    "check_headroom",
    "pool_connections",
    "process_connection_need",
    "server_headroom",
]
