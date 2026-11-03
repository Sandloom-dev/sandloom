"""Test-run slots: two test runs in one checkout never share a database.

``sandloom dev up`` writes ``.env.test.local`` next to ``.env.test`` with the
repository's test servers (``SANDLOOM_TEST_DATABASE_URL`` and the Redis URLs).
Before anything touches a database, the process that starts a run (the
``sandloom test`` parent, or the pytest controller of a plain ``pytest``)
takes a *slot*:

1. it locks slot 1, 2, 3 ... with ``pg_try_advisory_lock(<key>, <slot>)`` on
   the test server's ``postgres`` database, ``<key>`` derived from the machine
   id and the checkout, on a connection it keeps open for the whole run (a
   crashed run releases its slot with its connection);
2. slot N points the run at its own database (``sandloom_test_<checkout
   hash>`` for slot 1, ``…_<N>`` after), its own owner
   (``<machine id>:<checkout>#<N>``) and its own Redis base indexes (claimed
   for that owner), through ``SANDLOOM_TEST_*`` in ``os.environ``, so the
   ownership markers, ``assert_disposable_test_database`` and the xdist
   ``_gw<N>`` copies all see this run's names;
3. ``SANDLOOM_TEST_SLOT_HELD=<pid>`` tells the processes the run starts (the
   pytest steps of ``sandloom test``, xdist workers) that the slot is held.

Nothing changes for CI (``CI`` set), for an exported
``SANDLOOM_TEST_DATABASE_URL`` / ``SANDLOOM_TEST_OWNER`` (the caller manages
its resources), or for a checkout without ``.env.test.local``.

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

import contextlib
import os
import struct
import sys
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg2  # type: ignore[import-untyped]
from dotenv import dotenv_values

from sandloom.core.testing.env import LOCAL_ENV_FILE, find_env_file
from sandloom.core.testing.machine import (
    canonical,
    checkout_root,
    machine_id,
    short_hash,
)

SLOT_HELD_ENV = "SANDLOOM_TEST_SLOT_HELD"
OWNER_ENV = "SANDLOOM_TEST_OWNER"
DATABASE_ENV = "SANDLOOM_TEST_DATABASE_URL"
REDIS_ENVS = ("SANDLOOM_TEST_CACHE_URL", "SANDLOOM_TEST_BROKER_URL")
STRIDE_ENV = "SANDLOOM_TEST_REDIS_DB_STRIDE"
MAX_SLOTS = 64


@dataclass
class Slot:
    """A held slot: its number, the run's names and the lock's connection."""

    number: int
    database_url: str
    owner: str
    redis_urls: dict[str, str] = field(default_factory=dict)
    connection: Any = None

    def release(self) -> None:
        if self.connection is not None:
            with contextlib.suppress(Exception):
                self.connection.close()
            self.connection = None


_held: Slot | None = None


def held_slot() -> Slot | None:
    """The slot this process holds (``None``: none)."""
    return _held


def slot_key(machine: str, checkout: Path) -> int:
    """The first advisory-lock key of a checkout (a signed 32-bit int)."""
    digest = bytes.fromhex(short_hash(machine, canonical(checkout), length=8))
    return int(struct.unpack(">i", digest)[0])


def slot_database_url(base_url: str, number: int) -> str:
    """``base_url`` for slot ``number``: the name as is for slot 1, else ``_N``."""
    if number <= 1:
        return base_url
    parts = urlsplit(base_url)
    name = parts.path.lstrip("/")
    return urlunsplit(parts._replace(path=f"/{name}_{number}"))


def slot_owner(machine: str, checkout: Path, number: int) -> str:
    return f"{machine}:{checkout}#{number}"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _held_by_parent(env: MutableMapping[str, str]) -> bool:
    raw = (env.get(SLOT_HELD_ENV) or "").strip()
    if not raw:
        return False
    try:
        return _pid_alive(int(raw))
    except ValueError:
        return False


def local_env_file(starts: Iterable[Path]) -> Path | None:
    """``.env.test.local`` next to the nearest ``.env.test`` (never elsewhere)."""
    env_test = find_env_file(starts)
    if env_test is None:
        return None
    candidate = env_test.parent / LOCAL_ENV_FILE
    return candidate if candidate.is_file() else None


def _admin_dsn(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql", path="/postgres", query=""))


def _claim_redis_bases(urls: dict[str, str], owner: str) -> dict[str, str]:
    """New base indexes for this owner, one per distinct base index."""
    import redis  # noqa: PLC0415 - optional: the redis extra; only a slot with Redis URLs needs it

    from sandloom.core.testing import resources  # noqa: PLC0415 - optional: imports the redis extra at import; only a slot with Redis URLs needs it

    plain = [u for u in urls.values() if u.startswith(("redis://", "rediss://"))]
    if not plain:
        return {}
    try:
        mapping = resources.allocate_worker_redis(plain, 1, owner)
    except redis.RedisError as exc:
        raise RuntimeError(str(exc)) from exc
    out: dict[str, str] = {}
    for var, url in urls.items():
        parts = urlsplit(url)
        raw = parts.path.lstrip("/") or "0"
        if not raw.isdigit() or int(raw) not in mapping:
            continue
        out[var] = urlunsplit(parts._replace(path=f"/{mapping[int(raw)][0]}"))
    return out


def _slot_source(
    starts: Iterable[Path], env: MutableMapping[str, str]
) -> tuple[Path, dict[str, str]] | None:
    """``(.env.test.local, its values)`` when this run takes a slot."""
    if _truthy(env.get("CI")) or env.get("PYTEST_XDIST_WORKER"):
        return None
    if _held_by_parent(env) or env.get(DATABASE_ENV) or env.get(OWNER_ENV):
        return None
    local = local_env_file(starts)
    if local is None:
        return None
    values = {k: v for k, v in dotenv_values(local).items() if v}
    if not (values.get(DATABASE_ENV) or "").startswith("postgresql"):
        return None
    return local, values


def _lock_first_free(conn: Any, key: int) -> int:
    with conn.cursor() as cur:
        for candidate in range(1, MAX_SLOTS + 1):
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (key, candidate))
            row = cur.fetchone()
            if row and row[0]:
                return candidate
    return 0


def _point_at(
    slot: Slot, values: dict[str, str], env: MutableMapping[str, str], announce: bool
) -> None:
    env[DATABASE_ENV] = slot.database_url
    env[OWNER_ENV] = slot.owner
    env.setdefault(STRIDE_ENV, "auto")
    redis = {var: values[var] for var in REDIS_ENVS if values.get(var)}
    try:
        claimed = _claim_redis_bases(redis, slot.owner)
    except (OSError, RuntimeError) as exc:  # Redis full: the run reports it itself
        claimed = {}
        if announce:
            print(f"sandloom testing: no own Redis indexes ({exc})", file=sys.stderr)
    slot.redis_urls = {**redis, **claimed}
    env.update(slot.redis_urls)
    env[SLOT_HELD_ENV] = str(os.getpid())


def acquire_test_slot(
    starts: Iterable[Path],
    environ: MutableMapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
    announce: bool = True,
) -> Slot | None:
    """Take this run's slot and point ``environ`` at it (see the module doc).

    ``None`` when no slot applies (CI, explicit overrides, no
    ``.env.test.local``, a parent holds one) or the test server is down (the
    run then fails on its own, with the usual message).
    """
    global _held
    env = os.environ if environ is None else environ
    if _held is not None and environ is None:
        return _held
    source = _slot_source(starts, env)
    if source is None:
        return None
    local, values = source
    base = values[DATABASE_ENV]
    checkout = checkout_root(cwd or local.parent)
    machine = machine_id(env)
    try:
        conn = psycopg2.connect(_admin_dsn(base), connect_timeout=5)
    except psycopg2.OperationalError as exc:
        if announce:
            print(
                f"sandloom testing: no test-run slot (the test server of "
                f"{local.name} is not reachable: {str(exc).strip().splitlines()[0]})",
                file=sys.stderr,
            )
        return None
    conn.autocommit = True
    number = _lock_first_free(conn, slot_key(machine, checkout))
    if not number:
        conn.close()
        raise RuntimeError(
            f"sandloom testing: all {MAX_SLOTS} test-run slots of {checkout} are "
            "held by running test runs"
        )
    owner = slot_owner(machine, checkout, number)
    slot = Slot(number, slot_database_url(base, number), owner, connection=conn)
    _point_at(slot, values, env, announce)
    if announce and number > 1:
        name = urlsplit(slot.database_url).path.lstrip("/")
        print(
            f"sandloom testing: another test run holds slot 1 of this checkout; "
            f"this run uses slot {number} (database {name})",
            file=sys.stderr,
        )
    if environ is None:
        _held = slot
    return slot


def release_test_slot() -> None:
    global _held
    if _held is not None:
        _held.release()
        _held = None


__all__ = [
    "LOCAL_ENV_FILE",
    "MAX_SLOTS",
    "SLOT_HELD_ENV",
    "Slot",
    "acquire_test_slot",
    "held_slot",
    "local_env_file",
    "release_test_slot",
    "slot_database_url",
    "slot_key",
    "slot_owner",
]
