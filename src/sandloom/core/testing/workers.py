"""pytest-xdist isolation: one database and one set of Redis DBs per worker.

Run a suite in parallel with ``pytest -n auto --dist loadfile``. Each xdist
worker (``PYTEST_XDIST_WORKER=gw<N>``) gets its own backends, derived from the
test configuration (``.env.test`` / the shell):

* ``DATABASE_URL`` -> the same server, database ``<name>_gw<N>`` (created on
  first use, with the ``citext`` / ``uuid-ossp`` extensions the schemas use);
* ``CACHE_URL`` / ``BROKER_URL`` -> the same Redis, database index
  ``<index> + SANDLOOM_TEST_REDIS_DB_STRIDE * (N + 1)`` (stride default 16). URLs
  that differ keep differing, so two indexes never collapse into one. The
  Redis server needs enough databases: ``redis-server --databases 256``
  supports 15 workers at the default stride.
  ``SANDLOOM_TEST_REDIS_DB_STRIDE=auto`` replaces the fixed layout: the controller
  claims free indexes for every worker before they start
  (:func:`sandloom.core.testing.resources.allocate_worker_redis` — this owner's
  previous indexes first, then unmarked empty ones) and hands the workers the
  mapping in ``SANDLOOM_TEST_REDIS_WORKER_DBS`` (``<base>=<i0>,<i1>,…;…``). Use it
  on a Redis shared with other runs, whose markers break any fixed layout.

Without xdist (no ``PYTEST_XDIST_WORKER``) nothing changes.

Reloading ``.env.test`` (``load_dotenv(override=True)``) puts the BASE values
back; :func:`reload_test_env` reloads and re-applies the worker rewrite, and
:func:`apply_worker_env` is idempotent, so every reload path stays on the
worker's own backends. :func:`worker_env_values` is the same for code that
reads the file into a dict for a subprocess (``dotenv_values``).

Explicit overrides: ``SANDLOOM_TEST_DATABASE_URL`` / ``SANDLOOM_TEST_CACHE_URL`` /
``SANDLOOM_TEST_BROKER_URL`` in the environment WIN over ``.env.test`` on every
load path (:func:`load_test_env`, :func:`reload_test_env`,
:func:`worker_env_values`). ``.env.test`` is loaded with ``override=True`` so a
stray dev ``DATABASE_URL`` in the shell never masks it; these prefixed names are
the deliberate way to point one test run at a different database / Redis (e.g.
several suites in parallel on one machine).

This module imports nothing from the framework (no ``sandloom.core.config.settings``),
so
it is safe to use before the settings snapshot is taken.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values, load_dotenv

WORKER_ENV = "PYTEST_XDIST_WORKER"
REDIS_STRIDE_ENV = "SANDLOOM_TEST_REDIS_DB_STRIDE"
DEFAULT_REDIS_STRIDE = 16
#: ``SANDLOOM_TEST_REDIS_DB_STRIDE`` value selecting allocated indexes.
REDIS_STRIDE_AUTO = "auto"
#: The controller's allocation for ``auto``: ``<base>=<i0>,<i1>,…;…``.
REDIS_MAP_ENV = "SANDLOOM_TEST_REDIS_WORKER_DBS"
#: The settings each worker gets its own instance of.
DATABASE_VARS = ("DATABASE_URL",)
REDIS_VARS = ("CACHE_URL", "BROKER_URL")
#: ``SANDLOOM_TEST_<VAR>`` wins over ``.env.test`` for these settings.
OVERRIDE_PREFIX = "SANDLOOM_TEST_"
OVERRIDABLE_VARS = (*DATABASE_VARS, *REDIS_VARS)

_WORKER_RE = re.compile(r"^gw(\d+)$")
# What this process last wrote per variable: a value equal to it is already
# rewritten (idempotence); anything else is a base value to rewrite.
_applied: dict[str, str] = {}
_created_databases: set[str] = set()


def worker_id(environ: Mapping[str, str] | None = None) -> str | None:
    """``gw<N>`` on an xdist worker, else ``None``."""
    value = (os.environ if environ is None else environ).get(WORKER_ENV, "")
    return value if _WORKER_RE.match(value) else None


def worker_number(environ: Mapping[str, str] | None = None) -> int | None:
    wid = worker_id(environ)
    if wid is None:
        return None
    match = _WORKER_RE.match(wid)
    assert match is not None  # noqa: S101 - type narrowing of an invariant
    return int(match.group(1))


def _redis_stride(environ: Mapping[str, str]) -> int:
    raw = (environ.get(REDIS_STRIDE_ENV) or "").strip()
    try:
        stride = int(raw) if raw else DEFAULT_REDIS_STRIDE
    except ValueError:
        return DEFAULT_REDIS_STRIDE
    return stride if stride > 0 else DEFAULT_REDIS_STRIDE


def redis_auto(environ: Mapping[str, str] | None = None) -> bool:
    """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``: workers get allocated indexes."""
    env = os.environ if environ is None else environ
    return (env.get(REDIS_STRIDE_ENV) or "").strip().lower() == REDIS_STRIDE_AUTO


def format_redis_map(mapping: Mapping[int, list[int]]) -> str:
    return ";".join(
        f"{base}=" + ",".join(str(i) for i in indexes)
        for base, indexes in sorted(mapping.items())
    )


def parse_redis_map(raw: str) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for part in filter(None, (p.strip() for p in raw.split(";"))):
        base, _, indexes = part.partition("=")
        out[int(base)] = [int(i) for i in indexes.split(",") if i.strip()]
    return out


def mapped_redis_url(url: str, number: int, mapping: Mapping[int, list[int]]) -> str:
    """``url`` moved to worker ``number``'s allocated index (``auto``)."""
    parts = urlsplit(url)
    raw = parts.path.lstrip("/")
    try:
        index = int(raw) if raw else 0
    except ValueError:
        return url  # not a plain index (unix socket path, ...): leave it
    indexes = mapping.get(index)
    if not indexes:
        raise RuntimeError(
            f"{REDIS_STRIDE_ENV}=auto: no Redis index allocated for base index "
            f"{index}, worker gw{number} "
            f"({REDIS_MAP_ENV}={format_redis_map(mapping)!r})"
        )
    if number >= len(indexes):
        # A replacement for crashed workers beyond the allocated spares:
        # deterministic (never a conftest import failure cascade), but it
        # shares an index with another worker — say so on stderr.
        slot = number % len(indexes)
        print(
            f"sandloom testing: worker gw{number} has no Redis index of its own for "
            f"base {index}; sharing gw{slot}'s index {indexes[slot]} (a crashed "
            "worker was replaced more often than the spares allow)",
            file=sys.stderr,
        )
        return urlunsplit(parts._replace(path=f"/{indexes[slot]}"))
    return urlunsplit(parts._replace(path=f"/{indexes[number]}"))


def worker_database_url(url: str, worker: str) -> str:
    """``url`` with the database name suffixed ``_<worker>`` (idempotent)."""
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    if not name or name.endswith(f"_{worker}"):
        return url
    return urlunsplit(parts._replace(path=f"/{name}_{worker}"))


def worker_redis_url(url: str, number: int, stride: int) -> str:
    """``url`` with its database index offset by ``stride * (number + 1)``."""
    parts = urlsplit(url)
    raw = parts.path.lstrip("/")
    try:
        index = int(raw) if raw else 0
    except ValueError:
        return url  # not a plain index (unix socket path, ...): leave it
    return urlunsplit(parts._replace(path=f"/{index + stride * (number + 1)}"))


def rewrite_for_worker(
    values: Mapping[str, str | None], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The worker's replacements for ``values`` (empty when not on a worker).

    Pure: no idempotence bookkeeping and no database creation.
    """
    env = os.environ if environ is None else environ
    wid, number = worker_id(env), worker_number(env)
    if wid is None or number is None:
        return {}
    mapping: dict[int, list[int]] | None = None
    if redis_auto(env):
        raw = env.get(REDIS_MAP_ENV) or ""
        if not raw:
            raise RuntimeError(
                f"{REDIS_STRIDE_ENV}=auto but {REDIS_MAP_ENV} is not set: the "
                "xdist controller allocates the worker indexes (the "
                "sandloom.core.testing plugin's pytest_sessionstart)"
            )
        mapping = parse_redis_map(raw)
    stride = _redis_stride(env)
    out: dict[str, str] = {}
    for var in DATABASE_VARS:
        url = values.get(var)
        if url:
            out[var] = worker_database_url(url, wid)
    for var in REDIS_VARS:
        url = values.get(var)
        if url and url.startswith(("redis://", "rediss://")):
            out[var] = (
                mapped_redis_url(url, number, mapping)
                if mapping is not None
                else worker_redis_url(url, number, stride)
            )
    return out


def apply_worker_env(
    environ: MutableMapping[str, str] | None = None, *, create_database: bool = True
) -> dict[str, str]:
    """Point ``environ`` (default ``os.environ``) at this worker's backends.

    Idempotent: a value this function already wrote is left alone, a value
    put back by a ``.env.test`` reload is rewritten again. Creates the worker
    database on first use (``create_database``). Returns what changed.
    """
    env = os.environ if environ is None else environ
    pending = {
        var: env[var]
        for var in (*DATABASE_VARS, *REDIS_VARS)
        if env.get(var) and _applied.get(var) != env[var]
    }
    changed = rewrite_for_worker(pending, env)
    for var, value in changed.items():
        env[var] = value
        _applied[var] = value
    if create_database:
        for var in DATABASE_VARS:
            if var in changed:
                ensure_database(changed[var])
    return changed


def explicit_test_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The explicit ``SANDLOOM_TEST_<VAR>`` overrides set in ``environ``."""
    env = os.environ if environ is None else environ
    return {
        var: env[OVERRIDE_PREFIX + var]
        for var in OVERRIDABLE_VARS
        if env.get(OVERRIDE_PREFIX + var)
    }


def apply_test_overrides(
    environ: MutableMapping[str, str] | None = None,
    *,
    create_database: bool = True,
) -> dict[str, str]:
    """Copy ``SANDLOOM_TEST_<VAR>`` onto ``<VAR>`` in ``environ``; returns them.

    ``create_database=False`` is for a command that connects to no database.
    """
    env = os.environ if environ is None else environ
    overrides = explicit_test_overrides(env)
    env.update(overrides)
    # An explicitly named test database is created on first use (an xdist
    # worker's own database is created by apply_worker_env instead).
    if create_database and environ is None and worker_id(env) is None:
        for var in DATABASE_VARS:
            if var in overrides:
                ensure_database(overrides[var])
    return overrides


def reload_test_env(path: str | os.PathLike[str]) -> None:
    """``load_dotenv(path, override=True)`` then re-apply the worker rewrite.

    Use instead of a bare ``load_dotenv(..., override=True)`` in tests: the
    reload would otherwise put a parallel worker back on the shared base
    database / Redis.
    """
    if Path(path).exists():
        load_dotenv(dotenv_path=path, override=True)
    apply_test_overrides()
    apply_worker_env()


def worker_env_values(path: str | os.PathLike[str]) -> dict[str, str]:
    """``dotenv_values(path)`` (``None`` dropped), the ``SANDLOOM_TEST_<VAR>``
    overrides applied, rewritten for this worker. Missing file -> ``{}`` base."""
    raw = dotenv_values(path) if Path(path).exists() else {}
    values = {k: v for k, v in raw.items() if v is not None}
    values.update(explicit_test_overrides())
    changed = rewrite_for_worker(values)
    for var in DATABASE_VARS:
        if var in changed:
            ensure_database(changed[var])
    return {**values, **changed}


def ensure_database(url: str) -> None:
    """Create the (PostgreSQL) database ``url`` names if missing (once/process).

    Adds the extensions the framework's schemas rely on and marks a database it
    creates as owned by this run (:mod:`sandloom.core.testing.ownership`).
    Non-PostgreSQL URLs are ignored.
    """
    parts = urlsplit(url)
    if not parts.scheme.startswith("postgresql"):
        return
    name = parts.path.lstrip("/")
    if not name or name in _created_databases:
        return
    # Sync driver (test-only): callable from any context, even inside a loop.
    import psycopg2  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the hook / call that needs it
    from psycopg2 import errors, sql  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the hook / call that needs it

    def dsn(database: str) -> str:
        plain = parts._replace(scheme="postgresql", path=f"/{database}", query="")
        return urlunsplit(plain)

    created = False
    conn = psycopg2.connect(dsn("postgres"))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone() is None:
                try:
                    cur.execute(
                        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                    )
                    created = True
                except errors.DuplicateDatabase:  # pragma: no cover - race
                    pass
    finally:
        conn.close()
    conn = psycopg2.connect(dsn(name))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            if created:
                # Owned by this run from birth (sandloom.core.testing.ownership).
                from sandloom.core.testing.ownership import (  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the hook / call that needs it
                    current_owner,
                    stamp_database,
                )

                stamp_database(cur, name, current_owner())
            for extension in ("citext", "uuid-ossp", "pg_trgm"):
                cur.execute(
                    sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                        sql.Identifier(extension)
                    )
                )
    finally:
        conn.close()
    _created_databases.add(name)


__all__ = [
    "DEFAULT_REDIS_STRIDE",
    "REDIS_MAP_ENV",
    "REDIS_STRIDE_AUTO",
    "REDIS_STRIDE_ENV",
    "WORKER_ENV",
    "apply_worker_env",
    "ensure_database",
    "format_redis_map",
    "mapped_redis_url",
    "parse_redis_map",
    "redis_auto",
    "reload_test_env",
    "rewrite_for_worker",
    "worker_database_url",
    "worker_env_values",
    "worker_id",
    "worker_number",
    "worker_redis_url",
]
