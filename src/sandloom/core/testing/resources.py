"""Inventory, pruning and allocation of shared test resources.

Every run on a machine shares one test PostgreSQL server and one test Redis
(the docker test services). Each run marks what it uses with its owner
(:mod:`sandloom.core.testing.ownership`), so a run never resets another run's
database / Redis index. Markers of finished runs stay behind, though: Redis
has a fixed number of database indexes (256 with ``--databases 256``) and
once they are all marked, a parallel run finds no room for its workers.

This module

* lists every marked (or non-empty) test resource with its owner, when it was
  last used and whether a client is connected to it right now
  (:func:`scan_redis`, :func:`scan_databases`);
* decides which ones are stale (:func:`verdict`): a resource is pruned only
  when NOTHING is connected to it AND its last use is known and older than
  ``max_age_days`` (at least one day — no test run lasts that long, so an
  active run is never stale: it re-stamps its resources when it starts);
* prunes them on request (:func:`prune`): a Redis index is emptied with an
  atomic check-and-``FLUSHDB`` (refused when its marker or stamp changed in
  between), a database is dropped with a plain ``DROP DATABASE`` (refused by
  PostgreSQL while anything is connected);
* hands an xdist run free Redis indexes for its workers
  (:func:`allocate_worker_redis`): indexes this owner already holds first, then
  unmarked EMPTY ones, each claimed atomically — instead of the fixed
  ``index + stride * (N + 1)`` layout that collides with other runs' markers.

"Last used" is, in order: the explicit stamp (Redis ``sandloom:test:last_used``,
database setting ``sandloom_test.last_used``); else, for a legacy marker, the
youngest key idle time (``OBJECT IDLETIME`` under ``CLIENT NO-TOUCH``, so the
scan itself never refreshes it) or the database directory's modification time
(needs a superuser role); else unknown — and unknown is never pruned.

The CLI is ``tools/checks/test_resources.py``. Imports nothing from the
framework.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg2  # type: ignore[import-untyped]
import redis
from psycopg2 import sql

from sandloom.core.testing.ownership import (
    DB_LAST_USED_SETTING,
    DB_MARKER_PREFIX,
    REDIS_LAST_USED_KEY,
    REDIS_OWNER_KEY,
    TestResourceOwnershipError,
)

#: No resource younger than this is ever stale (a run re-stamps on start).
MIN_AGE_DAYS = 1.0
DEFAULT_MAX_AGE_DAYS = 7.0
#: Keys sampled for a legacy index's idle time.
_IDLE_SAMPLE = 200

# Atomic check-and-flush: only if the marker and the stamp are still the ones
# the scan saw and nothing was added since.
_FLUSH_IF_UNCHANGED = """
if (redis.call('GET', KEYS[1]) or '') ~= ARGV[1] then return 0 end
if (redis.call('GET', KEYS[2]) or '') ~= ARGV[2] then return 0 end
if redis.call('DBSIZE') ~= tonumber(ARGV[3]) then return 0 end
redis.call('FLUSHDB')
return 1
"""

# Atomic claim of a worker index: ours already, or unmarked and empty.
_CLAIM_INDEX = """
local holder = redis.call('GET', KEYS[1])
if holder == ARGV[1] or (not holder and redis.call('DBSIZE') == 0) then
  redis.call('SET', KEYS[1], ARGV[1])
  redis.call('SET', KEYS[2], ARGV[2])
  return 1
end
return 0
"""


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
# Redis
# --------------------------------------------------------------------------- #


def _server_url(url: str, index: int = 0) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{index}"))


def _display(url: str, index: int) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return f"{parts.scheme}://{host}/{index}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


def _connect_redis(url: str) -> Any:
    client = redis.Redis.from_url(
        _server_url(url),
        socket_connect_timeout=5,
        socket_timeout=10,
        single_connection_client=True,  # SELECT / NO-TOUCH apply to one link
    )
    try:
        # Reading the marker must not refresh the idle time a later scan uses.
        client.execute_command("CLIENT", "NO-TOUCH", "ON")
    except redis.exceptions.ResponseError:
        pass  # Redis < 7.2: idle times of legacy markers are best-effort
    return client


def _database_count(client: Any) -> int:
    try:
        reply = client.config_get("databases")
        return int(reply.get("databases") or reply.get(b"databases") or 16)
    except redis.exceptions.ResponseError:  # CONFIG disabled: probe instead
        count = 0
        while count < 4096:
            try:
                client.execute_command("SELECT", count)
            except redis.exceptions.ResponseError:
                break
            count += 1
        client.execute_command("SELECT", 0)
        return max(count, 1)


def _clients_per_db(client: Any) -> dict[int, int]:
    own = int(client.execute_command("CLIENT", "ID"))
    counts: dict[int, int] = {}
    for entry in client.client_list():
        if int(entry.get("id", -1)) == own:
            continue
        db = int(entry.get("db", 0))
        counts[db] = counts.get(db, 0) + 1
    return counts


def _headers(client: Any, indexes: Sequence[int]) -> list[tuple[int, str, str]]:
    """``(keys, owner, stamp)`` of every index in ONE pipelined round trip."""
    pipe = client.pipeline(transaction=False)
    pipe.execute_command("CLIENT", "NO-TOUCH", "ON")
    for index in indexes:
        pipe.execute_command("SELECT", index)
        pipe.dbsize()
        pipe.get(REDIS_OWNER_KEY)
        pipe.get(REDIS_LAST_USED_KEY)
    try:
        replies = pipe.execute(raise_on_error=False)[1:]
    finally:
        pipe.reset()
    out = []
    for n in range(len(indexes)):
        _, keys, owner, stamp = replies[4 * n : 4 * n + 4]
        out.append((int(keys), _text(owner), _text(stamp)))
    return out


def _min_idle(client: Any, index: int) -> int | None:
    """The youngest idle time (s) of up to ``_IDLE_SAMPLE`` keys of ``index``."""
    client.execute_command("SELECT", index)
    _, keys = client.scan(0, count=_IDLE_SAMPLE)
    if not keys:
        return None
    pipe = client.pipeline(transaction=False)
    pipe.execute_command("CLIENT", "NO-TOUCH", "ON")
    pipe.execute_command("SELECT", index)
    for key in keys[:_IDLE_SAMPLE]:
        pipe.object("idletime", key)
    try:
        idles = pipe.execute(raise_on_error=False)[2:]
    finally:
        pipe.reset()
    values = [int(i) for i in idles if isinstance(i, int)]
    return min(values) if values else None


def _resource(
    url: str,
    index: int,
    header: tuple[int, str, str],
    live: int,
    idle: int | None,
    now: float,
) -> Resource:
    keys, raw_owner, raw_stamp = header
    last_used: float | None = None
    source = "unknown"
    if raw_stamp.isdigit():
        last_used, source = float(raw_stamp), "stamp"
    elif idle is not None:
        last_used, source = now - idle, "idle"
    return Resource(
        kind="redis",
        name=_display(url, index),
        owner=raw_owner or None,
        last_used=last_used,
        source=source,
        live=live,
        size=keys,
        fingerprint=(raw_owner, raw_stamp, keys),
    )


def scan_redis(url: str, *, include_empty: bool = False) -> list[Resource]:
    """Every index of the Redis server ``url`` names (marked or non-empty)."""
    now = time.time()
    client = _connect_redis(url)
    try:
        live = _clients_per_db(client)
        indexes = list(range(_database_count(client)))
        out = []
        for index, header in zip(indexes, _headers(client, indexes), strict=True):
            keys, _, stamp = header
            if not (include_empty or keys or live.get(index)):
                continue
            idle = _min_idle(client, index) if keys and not stamp.isdigit() else None
            out.append(_resource(url, index, header, live.get(index, 0), idle, now))
        return out
    finally:
        client.close()


def _redis_index(resource: Resource) -> int:
    return int(urlsplit(resource.name).path.lstrip("/"))


def prune_redis(url: str, resource: Resource) -> bool:
    """Empty ``resource``'s index iff it is unchanged since the scan."""
    client = _connect_redis(url)
    try:
        index = _redis_index(resource)
        if _clients_per_db(client).get(index, 0):
            return False
        client.execute_command("SELECT", index)
        owner, stamp, keys = resource.fingerprint
        flushed = client.eval(
            _FLUSH_IF_UNCHANGED,
            2,
            REDIS_OWNER_KEY,
            REDIS_LAST_USED_KEY,
            owner,
            stamp,
            keys,
        )
        return bool(flushed)
    finally:
        client.close()


#: Spare indexes claimed per base index (best effort) for the workers xdist
#: starts to REPLACE crashed ones (``gw<count>``, ``gw<count+1>``, ...).
REDIS_SPARES_PER_BASE = 2


def allocate_worker_redis(
    base_urls: Sequence[str], workers: int, owner: str, *, spares: int = 0
) -> dict[int, list[int]]:
    """Claim ``workers`` Redis indexes per distinct base index of ``base_urls``.

    Returns ``{base index: [index of worker 0, worker 1, ...]}``. Indexes this
    ``owner`` already marked come first (a rerun reuses them), then unmarked
    EMPTY ones; each is claimed with an atomic check-and-set, so a concurrent
    run can never be handed the same index. Raises
    :class:`TestResourceOwnershipError` when the server has too few left.
    """
    bases = sorted({_index_of(u) for u in base_urls})
    if not bases or workers <= 0:
        return {}
    if len({urlsplit(u).netloc for u in base_urls}) > 1:
        raise TestResourceOwnershipError(
            "SANDLOOM_TEST_REDIS_DB_STRIDE=auto needs CACHE_URL and BROKER_URL on "
            "one Redis server; use a numeric stride instead"
        )
    needed = workers * len(bases)
    client = _connect_redis(base_urls[0])
    try:
        mine, free = _claimable_indexes(client, bases, owner)
        claimed: list[int] = []
        stamp = str(int(time.time()))
        # spares: best effort, after the required indexes
        wanted = needed + max(0, spares) * len(bases)
        for index in (*mine, *free):
            if len(claimed) == wanted:
                break
            client.execute_command("SELECT", index)
            if client.eval(
                _CLAIM_INDEX, 2, REDIS_OWNER_KEY, REDIS_LAST_USED_KEY, owner, stamp
            ):
                claimed.append(index)
    finally:
        client.close()
    if len(claimed) < needed:
        raise TestResourceOwnershipError(
            f"only {len(claimed)} of the {needed} Redis database indexes this "
            f"run needs ({workers} workers x {len(bases)}) are free on "
            f"{_display(base_urls[0], 0)[:-2]}: the rest belong to other runs. "
            "List them with `uv run python tools/checks/test_resources.py list` and "
            "prune the stale ones (`prune --apply`), or run with fewer workers."
        )
    extra = claimed[needed:]
    per_base = len(extra) // len(bases)
    return {
        base: claimed[n * workers : (n + 1) * workers]
        + extra[n * per_base : (n + 1) * per_base]
        for n, base in enumerate(bases)
    }


def _claimable_indexes(
    client: Any, bases: Sequence[int], owner: str
) -> tuple[list[int], list[int]]:
    """``(indexes owner already marked, unmarked empty unconnected ones)``."""
    indexes = [i for i in range(_database_count(client)) if i not in bases]
    # An empty, unmarked index may still be in use: a test that FLUSHDBs its
    # own index also drops the owner marker. Never hand out an index someone
    # is connected to.
    connected = _clients_per_db(client)
    mine: list[int] = []
    free: list[int] = []
    for index, (keys, holder, _) in zip(
        indexes, _headers(client, indexes), strict=True
    ):
        if holder == owner:
            mine.append(index)
        elif not holder and not keys and not connected.get(index):
            free.append(index)
    return mine, free


def affordable_redis_workers(base_urls: Sequence[str], owner: str) -> int:
    """How many xdist workers :func:`allocate_worker_redis` could serve now.

    Read-only (claims nothing): the indexes ``owner`` holds plus the free ones,
    divided by the distinct base indexes. ``sandloom test``'s ``-n auto`` is capped
    to it, so a Redis short of free indexes runs fewer workers (or serially)
    instead of aborting the session.
    """
    bases = sorted({_index_of(u) for u in base_urls})
    if not bases:
        return 0
    client = _connect_redis(base_urls[0])
    try:
        mine, free = _claimable_indexes(client, bases, owner)
    finally:
        client.close()
    return (len(mine) + len(free)) // len(bases)


def _index_of(url: str) -> int:
    raw = urlsplit(url).path.lstrip("/")
    return int(raw) if raw else 0


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #


def _admin_dsn(url: str) -> str:
    parts = urlsplit(url)
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
    """Prune one stale resource (``url``: its server). ``False``: left alone."""
    if resource.kind == "redis":
        return prune_redis(url, resource)
    return prune_database(url, resource)


__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "MIN_AGE_DAYS",
    "Resource",
    "affordable_redis_workers",
    "allocate_worker_redis",
    "prune",
    "prune_database",
    "prune_redis",
    "scan_databases",
    "scan_redis",
    "verdict",
]
