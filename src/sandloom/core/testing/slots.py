"""Test-run slots: two test runs in one checkout never share a database.

``sandloom dev up`` writes ``.env.test.local`` next to ``.env.test`` with the
repository's test servers (``SANDLOOM_TEST_DATABASE_URL`` and the cache and broker URLs).
Before anything touches a database, the process that starts a run (the
``sandloom test`` parent, or the pytest controller of a plain ``pytest``)
takes a *slot*:

1. it locks slot 1, 2, 3 ... with ``pg_try_advisory_lock(<key>, <slot>)`` on
   the test server's ``postgres`` database, ``<key>`` derived from the machine
   id and the checkout, on a connection it keeps open for the whole run (a
   crashed run releases its slot with its connection);
2. slot N points the run at its own database (``sandloom_test_<checkout
   hash>`` for slot 1, ``…_<N>`` after), its own owner
   (``<machine id>:<checkout>#<N>``) and its own backend resources (claimed
   for that owner), through ``SANDLOOM_TEST_*`` in ``os.environ``, so the
   ownership markers, ``assert_disposable_test_database`` and the xdist
   ``_gw<N>`` copies all see this run's names;
3. ``SANDLOOM_TEST_SLOT_HELD=<pid>`` tells the processes the run starts (the
   pytest steps of ``sandloom test``, xdist workers) that the slot is held.

Nothing changes for an exported ``SANDLOOM_TEST_DATABASE_URL`` /
``SANDLOOM_TEST_OWNER`` (the caller manages its resources: a CI job sets
them), or for a checkout without ``.env.test.local``. ``CI`` alone changes
nothing: agent harnesses export ``CI=1`` on a developer machine, and a slot is
cheap.

When every slot is held, the run stops with one line naming the holders (each
slot's connection carries ``application_name`` ``sandloom-test-slot pid=<pid>
<host>``). The slot's connection is checked again when the run ends: a run
that lost it (the server restarted) may have shared its database with
another run, and fails (:func:`slot_lost`).

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

import atexit
import contextlib
import os
import socket
import struct
import sys
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

from sandloom.core.migrations.pg_sync import psycopg2
from sandloom.core.testing import families
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
URL_ENVS = ("SANDLOOM_TEST_CACHE_URL", "SANDLOOM_TEST_BROKER_URL")
MAX_SLOTS = 64
#: ``application_name`` prefix of a slot's connection (names the holder).
APPLICATION_PREFIX = "sandloom-test-slot"


class SlotsExhausted(RuntimeError):
    """Every test-run slot of the checkout is held (a one-line message)."""


@dataclass
class Slot:
    """A held slot: its number, the run's names and the lock's connection."""

    number: int
    database_url: str
    owner: str
    family_urls: dict[str, str] = field(default_factory=dict)
    #: The URLs the harness families claimed for this slot (handed back on release).
    claimed_urls: tuple[str, ...] = ()
    connection: Any = None

    def release(self) -> None:
        # Only while this run still holds the slot's lock: once it is lost,
        # another run may hold the same slot (same owner, same indexes).
        claimed = () if slot_lost(self) else self.claimed_urls
        if claimed:
            for family in families.harness_families():
                with contextlib.suppress(Exception):  # gone or taken over: prune's job
                    family.release_slot_bases(claimed, self.owner)
        self.claimed_urls = ()
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


def _claim_family_bases(
    urls: dict[str, str], owner: str, env: MutableMapping[str, str]
) -> dict[str, str]:
    """New own base resources of every harness family for this owner."""
    claimed: dict[str, str] = {}
    for family in families.harness_families():
        claimed.update(family.slot_bases(urls, owner, env))
    return claimed


def _slot_source(
    starts: Iterable[Path], env: MutableMapping[str, str]
) -> tuple[Path, dict[str, str]] | None:
    """``(.env.test.local, its values)`` when this run takes a slot."""
    if env.get("PYTEST_XDIST_WORKER"):
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


def _application_name() -> str:
    return f"{APPLICATION_PREFIX} pid={os.getpid()} {socket.gethostname()}"[:63]


def _holders(conn: Any, key: int) -> str:
    """Who holds the slots of ``key``: ``"slot 1: pid=… host, …"``."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT l.objid::bigint, a.application_name, a.pid "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.granted "
                "AND l.objsubid = 2 AND l.classid = %s::bigint::oid "
                "ORDER BY 1",
                (key & 0xFFFFFFFF,),
            )
            rows = cur.fetchall()
    except psycopg2.Error:
        return "unknown holders"
    named = []
    for number, app, backend in rows:
        who = str(app or "")
        who = who.removeprefix(APPLICATION_PREFIX).strip() or f"backend {backend}"
        named.append(f"slot {number}: {who}")
    if not named:
        return "unknown holders"
    shown = ", ".join(named[:5])
    return shown + (f" and {len(named) - 5} more" if len(named) > 5 else "")


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
    urls = {var: values[var] for var in URL_ENVS if values.get(var)}
    try:
        claimed = _claim_family_bases(urls, slot.owner, env)
    except families.HarnessFamilyError:
        raise  # a broken family is not "backend full": refuse it as what it is
    except (OSError, RuntimeError) as exc:  # backend full: the run reports it itself
        claimed = {}
        if announce:
            print(
                f"sandloom testing: no own backend resources ({exc})", file=sys.stderr
            )
    slot.family_urls = {**urls, **claimed}
    slot.claimed_urls = tuple(dict.fromkeys(claimed.values()))
    env.update(slot.family_urls)
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
        conn = psycopg2.connect(
            _admin_dsn(base), connect_timeout=5, application_name=_application_name()
        )
    except psycopg2.OperationalError as exc:
        if announce:
            print(
                f"sandloom testing: no test-run slot (the test server of "
                f"{local.name} is not reachable: {str(exc).strip().splitlines()[0]})",
                file=sys.stderr,
            )
        return None
    conn.autocommit = True
    key = slot_key(machine, checkout)
    number = _lock_first_free(conn, key)
    if not number:
        holders = _holders(conn, key)
        conn.close()
        raise SlotsExhausted(
            f"sandloom testing: all {MAX_SLOTS} test-run slots of {checkout} are "
            f"held ({holders}): wait for a run to end or stop one, or export "
            "SANDLOOM_TEST_DATABASE_URL to give this run its own database"
        )
    owner = slot_owner(machine, checkout, number)
    slot = Slot(number, slot_database_url(base, number), owner, connection=conn)
    try:
        _point_at(slot, values, env, announce)
    except BaseException:
        conn.close()  # frees the advisory lock of a slot this run will not use
        raise
    if announce and number > 1:
        name = urlsplit(slot.database_url).path.lstrip("/")
        print(
            f"sandloom testing: another test run holds slot 1 of this checkout; "
            f"this run uses slot {number} (database {name})",
            file=sys.stderr,
        )
    if environ is None:
        _held = slot
        # `sandloom test` and a plain pytest controller hold the slot until
        # they exit; then it hands its resources back (also after a failure).
        atexit.register(release_test_slot)
    return slot


def slot_lost(slot: Slot | None = None) -> bool:
    """Whether the held slot's connection is gone (so is its lock: another
    run may have taken the slot and its database meanwhile)."""
    slot = _held if slot is None else slot
    if slot is None or slot.connection is None:
        return False
    try:
        with slot.connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except psycopg2.Error:
        return True
    return False


#: What a run that lost its slot prints (and why it fails).
SLOT_LOST_MESSAGE = (
    "sandloom testing: this run lost its test-run slot (the connection holding "
    "it closed: the test server restarted?); another run may have used the same "
    "database meanwhile, so the results are not trustworthy. Run the tests again"
)


def release_test_slot() -> None:
    global _held
    if _held is not None:
        _held.release()
        _held = None


__all__ = [
    "LOCAL_ENV_FILE",
    "MAX_SLOTS",
    "SLOT_HELD_ENV",
    "SLOT_LOST_MESSAGE",
    "Slot",
    "SlotsExhausted",
    "acquire_test_slot",
    "held_slot",
    "local_env_file",
    "release_test_slot",
    "slot_database_url",
    "slot_key",
    "slot_lost",
    "slot_owner",
]
