"""Inventory and pruning of shared test resources.

Every run on a machine shares one test PostgreSQL server (the docker test
services). Each run marks what it uses with its owner
(:mod:`sandloom.core.testing.ownership`), so a run never resets another run's
database. Markers of finished runs stay behind, though.

This module

* lists every marked test database with its owner, when it was last used and
  whether a client is connected to it right now (:func:`scan_databases`);
* decides which ones are stale (:func:`verdict`): a resource is pruned only
  when NOTHING is connected to it AND its last use is known and older than
  ``max_age_days`` (at least one day — no test run lasts that long, so an
  active run is never stale: it re-stamps its resources when it starts);
* prunes them on request (:func:`prune`): a database is dropped with a plain
  ``DROP DATABASE`` (refused by PostgreSQL while anything is connected).

A backend the kernel does not ship lists and prunes its own resources the same
way: it returns :class:`Resource` rows and judges them with :func:`verdict`
(the Redis family of ``sandloom-redis`` does, for the Redis database indexes).

"Last used" is, in order: the explicit stamp (the database setting
``sandloom_test.last_used``); else, for a legacy marker, the database
directory's modification time (needs a superuser role); else unknown — and
unknown is never pruned.

The CLI is ``tools/checks/test_resources.py``. Imports nothing from the
framework.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlunsplit

from sandloom.core.infra.url_split import split_url
from sandloom.core.migrations.pg_sync import psycopg2, sql
from sandloom.core.testing.ownership import (
    DB_LAST_USED_SETTING,
    DB_MARKER_PREFIX,
)

#: No resource younger than this is ever stale (a run re-stamps on start).
MIN_AGE_DAYS = 1.0
DEFAULT_MAX_AGE_DAYS = 7.0


@dataclass(frozen=True)
class Resource:
    """One test database / Redis index as a scan saw it."""

    kind: str  # "redis" | "database"
    name: str  # "redis://host:port/<index>" | database name
    owner: str | None
    last_used: float | None  # epoch seconds
    source: str  # "stamp" | "idle" | "files" | "unknown"
    live: int  # connected clients right now
    size: int  # keys | bytes
    #: What an atomic prune re-checks (the raw marker / stamp / key count).
    fingerprint: tuple[str, str, int] = ("", "", 0)

    def age_days(self, now: float) -> float | None:
        return None if self.last_used is None else (now - self.last_used) / 86400


def verdict(
    resource: Resource,
    *,
    now: float,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    include_unmarked: bool = False,
    protect_owners: Iterable[str] = (),
) -> tuple[bool, str]:
    """``(prune?, why)`` for ``resource``. Conservative: any doubt keeps it."""
    max_age_days = max(max_age_days, MIN_AGE_DAYS)
    if resource.live:
        return False, f"in use ({resource.live} connected)"
    if resource.owner is None and not include_unmarked:
        return False, "unmarked (pass --include-unmarked)"
    if resource.owner is not None and resource.owner in set(protect_owners):
        return False, "protected owner"
    age = resource.age_days(now)
    if age is None:
        return False, "last use unknown"
    if age < max_age_days:
        return False, f"used {age:.1f}d ago"
    return True, f"unused for {age:.1f}d ({resource.source})"


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #


def _admin_dsn(url: str) -> str:
    parts = split_url(url)
    return urlunsplit(parts._replace(scheme="postgresql", path="/postgres", query=""))


_DATABASES_SQL = """
SELECT d.datname,
       shobj_description(d.oid, 'pg_database'),
       pg_database_size(d.oid),
       (SELECT count(*) FROM pg_stat_activity a WHERE a.datid = d.oid),
       (SELECT split_part(c, '=', 2)
          FROM pg_db_role_setting s, unnest(s.setconfig) AS c
         WHERE s.setdatabase = d.oid AND s.setrole = 0
           AND c LIKE '<setting>=%'
         LIMIT 1),
       d.oid
  FROM pg_database d
 WHERE NOT d.datistemplate AND d.datname LIKE '%test%'
 ORDER BY d.datname
""".replace("<setting>", DB_LAST_USED_SETTING)


def _directory_mtimes(cursor: Any) -> dict[int, float]:
    """``base/<oid>`` modification times (superuser / pg_read_server_files)."""
    try:
        cursor.execute(
            "SELECT oid, extract(epoch FROM"
            " (pg_stat_file('base/' || oid)).modification)"
            " FROM pg_database WHERE NOT datistemplate"
        )
        return {int(oid): float(mtime) for oid, mtime in cursor.fetchall()}
    except psycopg2.Error:
        cursor.connection.rollback()
        return {}


def scan_databases(url: str) -> list[Resource]:
    """Every database whose name contains ``test`` on ``url``'s server."""
    conn = psycopg2.connect(_admin_dsn(url), connect_timeout=5)
    try:
        with conn.cursor() as cur:
            mtimes = _directory_mtimes(cur)
            cur.execute(_DATABASES_SQL)
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for name, comment, size, live, stamp, oid in rows:
        if size is None:
            # Dropped by another run between the catalog read and the size
            # lookup (pg_database_size() is NULL then): it is gone, skip it.
            continue
        owner: str | None = None
        if comment and comment.startswith(DB_MARKER_PREFIX):
            owner = comment[len(DB_MARKER_PREFIX) :]
        elif comment:
            owner = f"<comment {comment!r}>"
        last_used: float | None = None
        source = "unknown"
        if stamp and stamp.isdigit():
            last_used, source = float(stamp), "stamp"
        elif int(oid) in mtimes:
            last_used, source = mtimes[int(oid)], "files"
        out.append(
            Resource(
                kind="database",
                name=str(name),
                owner=owner,
                last_used=last_used,
                source=source,
                live=int(live or 0),
                size=int(size),
                fingerprint=(comment or "", stamp or "", 0),
            )
        )
    return out


def prune_database(url: str, resource: Resource) -> bool:
    """Drop ``resource``'s database iff its marker / stamp are unchanged.

    A plain ``DROP DATABASE`` (never ``WITH (FORCE)``): PostgreSQL refuses it
    while anything is connected, so an active run can never lose its database.
    """
    current = {r.name: r for r in scan_databases(url)}.get(resource.name)
    if current is None or current.fingerprint != resource.fingerprint or current.live:
        return False
    conn = psycopg2.connect(_admin_dsn(url), connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(resource.name))
                )
            except psycopg2.Error:
                return False
    finally:
        conn.close()
    return True


def prune(url: str, resource: Resource) -> bool:
    """Prune one stale database (``url``: its server). ``False``: left alone.

    Raises ``ValueError`` for a resource of another kind: its backend's family
    prunes it (a Redis index: ``sandloom-redis``), never a silent no-op here.
    """
    if resource.kind != "database":
        raise ValueError(
            f"cannot prune a {resource.kind!r} resource ({resource.name}) here: "
            "only databases; its backend's package prunes the rest"
        )
    return prune_database(url, resource)


__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "MIN_AGE_DAYS",
    "Resource",
    "prune",
    "prune_database",
    "scan_databases",
    "verdict",
]
