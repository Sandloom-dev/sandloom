"""Hard ownership of test resources: never reset another run's database / Redis.

The harness empties its test database (``TRUNCATE`` / ``drop_all``) and its
Redis indexes (``FLUSHDB``). On a machine where several checkouts (agents, a
developer, benchmarks) share one PostgreSQL / Redis server, a run pointed at
someone else's database — a stale env file, a missing override — would wipe
that run's state mid-flight. So every test resource carries an OWNER marker:

* a PostgreSQL database: its comment, ``sandloom-test-owner:<owner>``
  (``COMMENT ON DATABASE``; no table, so schema / autogenerate tests never see
  it, and it survives ``drop_all``);
* a Redis database index: the key ``sandloom:test:owner`` (re-written after every
  harness ``FLUSHDB``);
* a SQLite file: the file beside it, ``<file>.owner`` (its text the owner,
  its modification time when a run last used it), kept when the harness
  empties or rebuilds the database.

Every claim also records WHEN the resource was last used — the Redis key
``sandloom:test:last_used`` and the per-database setting ``sandloom_test.last_used``
(``ALTER DATABASE … SET``; epoch seconds) — kept apart from the owner marker,
whose format older checkouts still parse. ``tools/checks/test_resources.py``
(:mod:`sandloom.core.testing.resources`) lists the markers and prunes the ones no
run has used for days.

A resource without a marker is CLAIMED by the first run that uses it (a
database the harness creates is stamped on creation). A resource marked by a
DIFFERENT owner is refused before anything is reset, with
:class:`TestResourceOwnershipError` — unless ``SANDLOOM_TEST_TAKE_OWNERSHIP=1``,
which re-stamps it for this run (an explicit, deliberate takeover).

The owner is ``SANDLOOM_TEST_OWNER`` when set, else ``<machine id>:<checkout>``:
the machine id (:mod:`sandloom.core.testing.machine`) and the checkout the run
started in (the nearest directory holding ``.git`` above the working directory
— one per git worktree), else the working directory. Cloud sandboxes share
paths like ``/workspace/repo``, hence the machine id. A marker written before
the machine id existed (the bare checkout path) is still this checkout's own.
A test run started by ``sandloom test`` or the pytest plugin on a checkout
``sandloom dev up`` prepared holds a slot (:mod:`sandloom.core.testing.slots`)
whose owner ends in ``#<slot>``. CI runners start from fresh containers, so
their resources are always unmarked and simply claimed.

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

import contextlib
import os
import time
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg2  # type: ignore[import-untyped]
from psycopg2 import errors, sql

from sandloom.core.testing.machine import checkout_root, machine_id

OWNER_ENV = "SANDLOOM_TEST_OWNER"
TAKEOVER_ENV = "SANDLOOM_TEST_TAKE_OWNERSHIP"
#: Prefix of the database comment carrying the owner.
DB_MARKER_PREFIX = "sandloom-test-owner:"
#: Redis key carrying the owner of a database index.
REDIS_OWNER_KEY = "sandloom:test:owner"
#: Redis key carrying when a run last claimed / flushed the index (epoch s).
REDIS_LAST_USED_KEY = "sandloom:test:last_used"
#: Per-database setting carrying when a run last claimed it (epoch seconds).
DB_LAST_USED_SETTING = "sandloom_test.last_used"

# Resources this process already verified (kind, identity) -> owner.
_claimed: dict[tuple[str, str], str] = {}


class TestResourceOwnershipError(RuntimeError):
    """The test database / Redis index belongs to another run."""

    __test__ = False  # not a pytest test class


def current_owner(
    environ: Mapping[str, str] | None = None, cwd: Path | None = None
) -> str:
    """This run's owner id: ``SANDLOOM_TEST_OWNER``, else
    ``<machine id>:<checkout root>``."""
    env = os.environ if environ is None else environ
    explicit = (env.get(OWNER_ENV) or "").strip()
    if explicit:
        return explicit
    return f"{machine_id(env)}:{checkout_root(cwd)}"


def same_owner(holder: str | None, owner: str) -> bool:
    """``holder`` is ``owner``, or the legacy form of it: a marker written
    before owners carried the machine id (the bare checkout path)."""
    if holder is None:
        return False
    if holder == owner:
        return True
    _, sep, path = owner.partition(":")
    return bool(sep) and holder == path and Path(holder).is_absolute()


def takeover_requested(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(TAKEOVER_ENV) or "").strip().lower() in {"1", "true", "yes"}


def _refusal(kind: str, what: str, holder: str, owner: str, hint: str) -> str:
    return (
        f"Refusing to reset {kind} {what}: it belongs to another test run "
        f"(owner {holder!r}; this run is {owner!r}). Point this run at its "
        f"own resources — {hint} — or, if that run is gone and the {kind} "
        f"is really yours now, set {TAKEOVER_ENV}=1 once to take it over "
        f"(the owner id is {OWNER_ENV} when set, else the checkout root)."
    )


_DB_HINT = (
    "export SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://…/sandloom_test_<you> "
    "(created on first use)"
)
_SQLITE_HINT = (
    "export SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///<your dir>/<name>_test.db"
)
_REDIS_HINT = (
    "export SANDLOOM_TEST_CACHE_URL / SANDLOOM_TEST_BROKER_URL=redis://…/<your index>"
)


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #


def _plain_dsn(url: str, database: str | None = None) -> tuple[str, str]:
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    path = f"/{database if database is not None else name}"
    plain = parts._replace(scheme="postgresql", path=path, query="")
    return urlunsplit(plain), name


def database_owner(cursor: Any) -> str | None:
    """The owner marker of the connected database (``None``: unmarked)."""
    cursor.execute(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
        "WHERE datname = current_database()"
    )
    row = cursor.fetchone()
    comment = row[0] if row else None
    if not comment:
        return None
    if not comment.startswith(DB_MARKER_PREFIX):
        return f"<unrecognised comment {comment!r}>"
    return str(comment[len(DB_MARKER_PREFIX) :])


def stamp_database(cursor: Any, name: str, owner: str) -> None:
    cursor.execute(
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(name), sql.Literal(DB_MARKER_PREFIX + owner)
        )
    )


def claim_database(
    url: str, *, owner: str | None = None, environ: Mapping[str, str] | None = None
) -> None:
    """Verify (or claim) that this run owns the PostgreSQL database ``url``.

    Raises :class:`TestResourceOwnershipError` when another owner marked it.
    A missing database or an unreachable server is not this check's business
    (the harness creates / the tests skip): it returns quietly.
    """
    if url.startswith("sqlite"):
        _claim_sqlite(url, owner or current_owner(environ), environ)
        return
    if not url.startswith("postgresql"):
        return
    owner = owner or current_owner(environ)
    dsn, name = _plain_dsn(url)
    if not name or _claimed.get(("db", dsn)) == owner:
        return

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError:
        return  # missing database / server down: nothing of anyone's to reset
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            holder = database_owner(cur)
            if holder is not None and not same_owner(holder, owner):
                if not takeover_requested(environ):
                    raise TestResourceOwnershipError(
                        _refusal("database", repr(name), holder, owner, _DB_HINT)
                    )
                warnings.warn(
                    f"{TAKEOVER_ENV}: taking over test database {name!r} "
                    f"from {holder!r}",
                    stacklevel=2,
                )
            if holder != owner:
                try:
                    stamp_database(cur, name, owner)
                except errors.InsufficientPrivilege as exc:
                    raise TestResourceOwnershipError(
                        f"cannot mark test database {name!r} with its owner "
                        "(the role does not own it); refusing destructive test "
                        "setup until the role can persist the ownership marker"
                    ) from exc
            # When it was last used (``ALTER DATABASE … SET``; best-effort):
            # a setting, not the comment, which older checkouts parse whole.
            # (A role that cannot alter it leaves no stamp: never pruned by one.)
            with contextlib.suppress(psycopg2.Error):
                cur.execute(
                    sql.SQL("ALTER DATABASE {} SET {} = {}").format(
                        sql.Identifier(name),
                        sql.SQL(DB_LAST_USED_SETTING),
                        sql.Literal(str(int(time.time()))),
                    )
                )
    finally:
        conn.close()
    _claimed[("db", dsn)] = owner


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #

#: Suffix of the file beside a SQLite test database carrying its owner.
SQLITE_MARKER_SUFFIX = ".owner"


def _sqlite_marker(file: Path) -> Path:
    return file.with_name(file.name + SQLITE_MARKER_SUFFIX)


def stamp_sqlite_file(file: Path, owner: str) -> None:
    """Mark the SQLite test database ``file`` as ``owner``'s (now used)."""
    file.parent.mkdir(parents=True, exist_ok=True)
    _sqlite_marker(file).write_text(owner, encoding="utf-8")


def sqlite_file_owner(file: Path) -> str | None:
    """The owner marker of the SQLite test database ``file`` (``None``:
    unmarked)."""
    marker = _sqlite_marker(file)
    if not marker.exists():
        return None
    return marker.read_text(encoding="utf-8").strip() or None


def _sqlite_path(url: str) -> Path | None:
    """The file of a SQLite URL (``None``: in memory); as
    :func:`sandloom.core.testing.workers.sqlite_file`, which imports this
    module."""
    parts = urlsplit(url)
    path = parts.path[1:] if parts.path.startswith("/") else parts.path
    if not path or path == ":memory:" or "mode=memory" in parts.query:
        return None
    return Path(path)


def _claim_sqlite(url: str, owner: str, environ: Mapping[str, str] | None) -> None:
    file = _sqlite_path(url)
    if file is None or _claimed.get(("sqlite", str(file))) == owner:
        return  # in memory: one process's own
    holder = sqlite_file_owner(file)
    if holder is not None and not same_owner(holder, owner):
        if not takeover_requested(environ):
            raise TestResourceOwnershipError(
                _refusal("SQLite database", str(file), holder, owner, _SQLITE_HINT)
            )
        warnings.warn(
            f"{TAKEOVER_ENV}: taking over SQLite test database {file} from {holder!r}",
            stacklevel=3,
        )
    stamp_sqlite_file(file, owner)  # its mtime: when a run last used it
    _claimed[("sqlite", str(file))] = owner


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #


def _decoded(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _check_redis_holder(
    holder: str | None, url: str, owner: str, environ: Mapping[str, str] | None
) -> None:
    if holder is None or same_owner(holder, owner):
        return
    if not takeover_requested(environ):
        raise TestResourceOwnershipError(
            _refusal("Redis database", url, holder, owner, _REDIS_HINT)
        )
    warnings.warn(
        f"{TAKEOVER_ENV}: taking over Redis database {url} from {holder!r}",
        stacklevel=3,
    )


def _redact(url: str) -> str:
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def claim_redis(
    url: str, *, owner: str | None = None, environ: Mapping[str, str] | None = None
) -> None:
    """Verify (or claim) that this run owns the Redis database ``url``."""
    if not url.startswith(("redis://", "rediss://")):
        return
    owner = owner or current_owner(environ)
    if _claimed.get(("redis", url)) == owner:
        return

    import redis  # noqa: PLC0415 - optional: the redis extra; only a redis URL needs it

    client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    try:
        try:
            holder = _decoded(client.get(REDIS_OWNER_KEY))
        except redis.exceptions.ConnectionError:
            return  # server down: the tests that need it skip / fail
        _check_redis_holder(holder, _redact(url), owner, environ)
        if holder != owner:
            client.set(REDIS_OWNER_KEY, owner)
        client.set(REDIS_LAST_USED_KEY, int(time.time()))
    finally:
        client.close()
    _claimed[("redis", url)] = owner


async def flush_owned_redis(client: Any, *, owner: str | None = None) -> None:
    """``FLUSHDB`` a test Redis database this run owns, keeping its marker.

    ``client`` is an asyncio Redis client (bytes or decoded responses).
    Raises :class:`TestResourceOwnershipError` when another run marked it.
    """
    owner = owner or current_owner()
    holder = _decoded(await client.get(REDIS_OWNER_KEY))
    kwargs = client.connection_pool.connection_kwargs
    where = f"redis://{kwargs.get('host', '?')}:{kwargs.get('port', '?')}/" + str(
        kwargs.get("db", 0)
    )
    _check_redis_holder(holder, where, owner, None)
    await client.flushdb()
    await client.set(REDIS_OWNER_KEY, owner)
    await client.set(REDIS_LAST_USED_KEY, int(time.time()))


# --------------------------------------------------------------------------- #
# A whole run
# --------------------------------------------------------------------------- #


def claim_test_resources(values: Mapping[str, str]) -> None:
    """Claim ``DATABASE_URL`` / ``CACHE_URL`` / ``BROKER_URL`` of ``values``."""
    database = values.get("DATABASE_URL") or ""
    if database:
        claim_database(database)
    for var in ("CACHE_URL", "BROKER_URL"):
        url = values.get(var) or ""
        if url:
            claim_redis(url)


__all__ = [
    "DB_LAST_USED_SETTING",
    "DB_MARKER_PREFIX",
    "OWNER_ENV",
    "REDIS_LAST_USED_KEY",
    "REDIS_OWNER_KEY",
    "TAKEOVER_ENV",
    "TestResourceOwnershipError",
    "claim_database",
    "claim_redis",
    "claim_test_resources",
    "current_owner",
    "flush_owned_redis",
    "same_owner",
    "sqlite_file_owner",
    "stamp_sqlite_file",
    "takeover_requested",
]
