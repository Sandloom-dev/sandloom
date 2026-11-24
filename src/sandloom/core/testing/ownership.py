"""Hard ownership of test resources: never reset another run's database or index.

The harness empties its test database (``TRUNCATE`` / ``drop_all``) and its
keyed indexes (``FLUSHDB``). On a machine where several checkouts (agents, a
developer, benchmarks) share one PostgreSQL or keyed server, a run pointed at
someone else's database — a stale env file, a missing override — would wipe
that run's state mid-flight. So every test resource carries an OWNER marker:

* a PostgreSQL database: its comment, ``sandloom-test-owner:<owner>``
  (``COMMENT ON DATABASE``; no table, so schema / autogenerate tests never see
  it, and it survives ``drop_all``);
* a keyed database index: the key ``sandloom:test:owner`` (re-written after every
  harness ``FLUSHDB``); the keyed family of the harness (``sandloom-redis``,
  :mod:`sandloom.core.testing.families`) writes and checks it with
  :func:`check_holder`, :func:`already_claimed` and :func:`record_claim`;
* a SQLite file: the file beside it, ``<file>.owner`` (its text the owner,
  its modification time when a run last used it), kept when the harness
  empties or rebuilds the database.

Every claim also records WHEN the resource was last used — the key
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
whose owner ends in ``#<slot>``; a database the same checkout marked without a
slot, and no run has claimed since (``sandloom migrate new`` creates the
checkout's test database before any run holds one), is that run's own too
(:func:`same_owner`, :func:`database_claimed`). A slot-less database a run
claimed is that run's: a slot run is refused it. CI runners start from fresh
containers, so their resources are always unmarked and simply claimed.

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

import contextlib
import os
import re
import time
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

from sandloom.core.migrations.pg_sync import errors, psycopg2, sql
from sandloom.core.testing import families
from sandloom.core.testing.machine import checkout_root, machine_id
from sandloom.core.url_split import split_url

OWNER_ENV = "SANDLOOM_TEST_OWNER"
TAKEOVER_ENV = "SANDLOOM_TEST_TAKE_OWNERSHIP"
#: Prefix of the database comment carrying the owner.
DB_MARKER_PREFIX = "sandloom-test-owner:"
#: Per-database setting carrying when a run last claimed it (epoch seconds).
DB_LAST_USED_SETTING = "sandloom_test.last_used"

# Resources this process already verified (kind, identity) -> owner.
_claimed: dict[tuple[str, str], str] = {}


class TestResourceOwnershipError(RuntimeError):
    """The test database / keyed index belongs to another run."""

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


#: ``<machine id>:<checkout root>#<slot>``: the owner of a test-run slot
#: (:func:`slot_owner`); the part before ``#`` is the checkout's slot-less owner.
#: The machine id is exactly 12 hex (:func:`sandloom.core.testing.machine.machine_id`)
#: and the checkout an absolute path, so an explicit ``SANDLOOM_TEST_OWNER`` such
#: as ``ci:shard#2`` or ``agent#7`` is no slot's owner.
_SLOT_OWNER = re.compile(r"(?P<checkout>[0-9a-f]{12}:(?P<path>.+))#(?P<slot>[0-9]+)")


def slot_owner(machine: str, checkout: Path, number: int) -> str:
    """The owner of slot ``number`` of ``checkout`` on ``machine``
    (:mod:`sandloom.core.testing.slots`)."""
    return f"{machine}:{checkout}#{number}"


def slotless_owner(owner: str) -> str:
    """The checkout's own owner of a slot's ``owner``
    (``<machine id>:<checkout>#2`` is ``<machine id>:<checkout>``); any other
    owner, an explicit ``SANDLOOM_TEST_OWNER`` included, is returned as it is."""
    match = _SLOT_OWNER.fullmatch(owner)
    if match is None or not Path(match["path"]).is_absolute():
        return owner
    return match["checkout"]


def same_owner(holder: str | None, owner: str, *, unclaimed: bool = False) -> bool:
    """``holder`` is ``owner``, or a form of it the same checkout wrote:

    * for an owner that is a slot's, the checkout's slot-less owner
      (``sandloom migrate new`` creates the checkout's test database before
      any run holds a slot, so it is marked ``<machine id>:<checkout>``) and
      the legacy form below, but ONLY with ``unclaimed``: the caller found that
      no run has claimed the resource since (:func:`database_claimed`). A
      slot-less holder that claimed it is a run that may still be using it:
      another run of the checkout never takes it. The other way round never
      holds either: a slot-less run does not take what a slot's run marked;
    * the legacy form: a marker written before owners carried the machine id
      (the bare checkout path).
    """
    if holder is None:
        return False
    if holder == owner:
        return True
    checkout_owner = slotless_owner(owner)
    if checkout_owner != owner and not unclaimed:
        return False
    if holder == checkout_owner:
        return True
    _, sep, path = checkout_owner.partition(":")
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


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #


def _plain_dsn(url: str, database: str | None = None) -> tuple[str, str]:
    parts = split_url(url)
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


def database_claimed(cursor: Any) -> bool:
    """Whether a run has claimed the connected database since it was created.

    :func:`claim_database` records when the database was last used
    (``sandloom_test.last_used``); the harness stamps a database it creates
    (``workers.ensure_database``, what ``sandloom migrate new`` runs) with an
    owner alone. A marked database without that setting was never used.
    """
    cursor.execute(
        "SELECT 1 FROM pg_db_role_setting s, unnest(s.setconfig) AS c "
        "WHERE s.setdatabase = (SELECT oid FROM pg_database "
        "WHERE datname = current_database()) "
        "AND s.setrole = 0 AND split_part(c, '=', 1) = %s",
        (DB_LAST_USED_SETTING,),
    )
    return cursor.fetchone() is not None


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
            # Asked only of a foreign marker: a slot's run takes its checkout's
            # slot-less database only while no run has claimed it.
            unclaimed = (
                holder is not None and holder != owner and not database_claimed(cur)
            )
            if holder is not None and not same_owner(
                holder, owner, unclaimed=unclaimed
            ):
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
    parts = split_url(url)
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
# A backend's own resources (harness families)
# --------------------------------------------------------------------------- #


def check_holder(
    kind: str,
    what: str,
    holder: str | None,
    owner: str,
    hint: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Refuse a resource another run marked: what a harness family calls before
    it resets one of its own (a keyed index, an object-store bucket).

    ``kind`` and ``what`` name the resource in the message (``"cache database"``,
    its redacted URL); ``holder`` is the marker found (``None``: unmarked);
    ``hint`` says how this run points at its own. Raises
    :class:`TestResourceOwnershipError`, unless ``SANDLOOM_TEST_TAKE_OWNERSHIP=1``
    (a warning then, and the caller re-stamps the resource).

    A slot's run never takes the slot-less marker of its checkout here
    (:func:`same_owner` without ``unclaimed``): a backend's marker is written
    by a claim alone (``claim_redis`` stamps its owner together with when it was
    last used), so a marked resource is one a run may still be using. Only
    ``migrate new`` marks a resource it never claimed, and it creates a database.
    """
    if holder is None or same_owner(holder, owner):
        return
    if not takeover_requested(environ):
        raise TestResourceOwnershipError(_refusal(kind, what, holder, owner, hint))
    warnings.warn(
        f"{TAKEOVER_ENV}: taking over {kind} {what} from {holder!r}",
        stacklevel=3,
    )


def already_claimed(kind: str, identity: str, owner: str) -> bool:
    """Whether this process verified resource ``identity`` of ``kind`` for
    ``owner`` already (:func:`record_claim`)."""
    return _claimed.get((kind, identity)) == owner


def record_claim(kind: str, identity: str, owner: str) -> None:
    """Remember that this process verified resource ``identity`` of ``kind`` for
    ``owner``, so the next claim of it costs nothing."""
    _claimed[(kind, identity)] = owner


# --------------------------------------------------------------------------- #
# A whole run
# --------------------------------------------------------------------------- #


def claim_test_resources(values: Mapping[str, str]) -> None:
    """Claim ``DATABASE_URL`` of ``values``, then what every harness family
    resets (:mod:`sandloom.core.testing.families`: the keyed family claims the
    URL of every named instance ``INFRA`` declares)."""
    database = values.get("DATABASE_URL") or ""
    if database:
        claim_database(database)
    for family in families.harness_families():
        family.claim(values)


__all__ = [
    "DB_LAST_USED_SETTING",
    "DB_MARKER_PREFIX",
    "OWNER_ENV",
    "TAKEOVER_ENV",
    "TestResourceOwnershipError",
    "already_claimed",
    "check_holder",
    "claim_database",
    "claim_test_resources",
    "current_owner",
    "record_claim",
    "same_owner",
    "slot_owner",
    "slotless_owner",
    "sqlite_file_owner",
    "stamp_sqlite_file",
    "takeover_requested",
]
