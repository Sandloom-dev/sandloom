"""pytest-xdist isolation: one database, and one set of backend resources, per worker.

Run a suite in parallel with ``pytest -n auto --dist loadfile``. Each xdist
worker (``PYTEST_XDIST_WORKER=gw<N>``) gets its own backends, derived from the
test configuration (``.env.test`` / the shell):

* ``DATABASE_URL`` -> the same server, database ``<name>_gw<N>`` (created on
  first use, with the ``citext`` / ``uuid-ossp`` extensions the schemas use);
* every ``INFRA`` instance's ``url`` / ``broker`` -> what the harness family of
  that URL's backend says a worker uses (the layout, its environment names and
  its limits are the family's: the package that ships the backend documents
  them).

Every backend but the database is a harness family
(:mod:`sandloom.core.testing.families`) that its package registers; this module
calls its ``rewrite_worker`` hook, and a process with no family is DB-only. A
family loads its isolation code only for a process that can reach its backend (the
client is installed and a setting names it): an application on memory drivers
never loads it and needs no client to run its tests. It asks the driver of each
``INFRA`` instance with a ``url`` or a ``broker`` through
``InfraProvider.test_isolation(options, worker_id)``.

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
the deliberate way to point one test run at a different database / backend (e.g.
several suites in parallel on one machine). ``SANDLOOM_TEST_CACHE_URL`` /
``SANDLOOM_TEST_BROKER_URL`` keep their names but name instances: they are
merged into ``INFRA`` (:func:`infra_with_overrides`: the declared ``default``
cache / task queue and every declared instance on the same database as it;
instances on other databases keep theirs, a declared ``backend`` follows its
``broker``). One that finds no declared default raises, it never does nothing.

This module imports nothing from the framework (no ``sandloom.core.config.settings``),
so
it is safe to use before the settings snapshot is taken.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

from dotenv import dotenv_values, load_dotenv

from sandloom.core.migrations.pg_sync import errors, psycopg2, sql
from sandloom.core.testing import families
from sandloom.core.testing.families import INFRA_VAR
from sandloom.core.url_split import split_url

WORKER_ENV = "PYTEST_XDIST_WORKER"
#: The settings each worker gets its own instance of.
DATABASE_VARS = ("DATABASE_URL",)
#: The suffixes of the harness's URL override names (``SANDLOOM_TEST_CACHE_URL``,
#: ``SANDLOOM_TEST_BROKER_URL``). They are not settings: each is merged into
#: ``INFRA`` (:func:`infra_with_overrides`).
URL_VARS = ("CACHE_URL", "BROKER_URL")
#: ``INFRA`` (JSON): its instances with a ``url`` / ``broker`` are isolated.
ISOLATED_VARS = (*DATABASE_VARS, INFRA_VAR)
#: ``SANDLOOM_TEST_<VAR>`` wins over ``.env.test`` for these names.
OVERRIDE_PREFIX = "SANDLOOM_TEST_"
OVERRIDABLE_VARS = (*DATABASE_VARS, *URL_VARS)
#: The capability whose ``default`` instance each URL override replaces the
#: URL of in ``INFRA``; the option that holds it is whichever of
#: :data:`_URL_OPTIONS` the instance states. The one map of the overrides:
#: ``sandloom dev up`` reads it to decide which names it may write.
INFRA_URL_CAPABILITIES = {
    "CACHE_URL": "cache",
    "BROKER_URL": "taskqueue",
}
#: The options of an instance that hold a URL the overrides may replace:
#: the harness's one list of URL options, less ``backend``, which mirrors its
#: instance's ``broker`` (:func:`_set_url`) and is never replaced on its own.
_URL_OPTIONS = tuple(o for o in families.INSTANCE_URL_OPTIONS if o != "backend")

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


def worker_database_url(url: str, worker: str) -> str:
    """``url`` with the database name suffixed ``_<worker>`` (idempotent).

    A SQLite file gets the suffix on its stem, in the same directory:
    ``…/run_test.db`` -> ``…/run_test_gw0.db``."""
    parts = split_url(url)
    if parts.scheme.startswith("sqlite"):
        return _worker_sqlite_url(url, worker)
    name = parts.path.lstrip("/")
    if not name or name.endswith(f"_{worker}"):
        return url
    return urlunsplit(parts._replace(path=f"/{name}_{worker}"))


def sqlite_file(url: str) -> Path | None:
    """The file a SQLite URL names (``None``: not SQLite, or in memory)."""
    parts = split_url(url)
    if not parts.scheme.startswith("sqlite"):
        return None
    # ``sqlite:///rel.db`` is relative, ``sqlite:////abs.db`` absolute: the
    # path after the authority's ``/``.
    path = parts.path[1:] if parts.path.startswith("/") else parts.path
    if not path or path == ":memory:" or "mode=memory" in parts.query:
        return None
    return Path(path)


def _worker_sqlite_url(url: str, worker: str) -> str:
    file = sqlite_file(url)
    if file is None or file.stem.endswith(f"_{worker}"):
        return url
    parts = split_url(url)
    renamed = file.with_name(f"{file.stem}_{worker}{file.suffix}")
    # spelled out: urlunsplit drops the empty authority of ``sqlite:///rel``
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}/{renamed}{query}"


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
    out: dict[str, str] = {}
    for var in DATABASE_VARS:
        url = values.get(var)
        if url:
            out[var] = worker_database_url(url, wid)
    for family in families.harness_families():
        out.update(family.rewrite_worker(values, env, wid, number))
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
        for var in ISOLATED_VARS
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


class UnmatchedOverrideError(ValueError):
    """A ``SANDLOOM_TEST_CACHE_URL`` / ``SANDLOOM_TEST_BROKER_URL`` the run sets
    names no instance to replace: ``INFRA`` declares no URL of an installed
    backend for the default cache (or task queue). The override would do
    nothing, silently; it refuses instead."""


def infra_with_overrides(
    infra_raw: str | None, overrides: Mapping[str, str]
) -> str | None:
    """``infra_raw`` (the ``INFRA`` JSON) with the run's own cache / broker URL
    merged into the instances that sit on the one it replaces.

    ``overrides`` maps ``CACHE_URL`` / ``BROKER_URL`` (the suffix of the
    ``SANDLOOM_TEST_`` name) to the URL this run uses. Nothing follows another
    instance's URL, so the URL is written into each instance that named the
    replaced one, never into the others:

    * the ``default`` instance of ``cache`` (``url``) and of ``taskqueue``
      (``broker``) must declare a URL of an installed backend in ``INFRA``
      (``.env.test``): the override replaces it. No instance is created: with none
      declared the override raises :class:`UnmatchedOverrideError`, never does nothing;
    * every other declared instance (any capability) whose ``url`` or ``broker``
      is the SAME server and database as the default's takes it too (in
      that option);
    * an instance on another database or server keeps its URL: instances meant
      to live apart stay apart, and the cache's ``FLUSHDB`` never reaches their
      keys. A parallel run that overrides only the cache therefore keeps the
      declared URLs of the instances on other databases: override those as well
      (the task queue's through ``SANDLOOM_TEST_BROKER_URL``) or two runs share
      them. The per-worker rewrite of a harness family moves databases
      only, on the same server, and does the same;
    * a ``backend`` a replaced ``broker`` instance declares mirrors the new
      broker URL (a run that points its broker elsewhere must not share the
      result database of another run).

    A malformed ``INFRA`` is returned unchanged (the settings refuse it when they
    load); without an override so is a missing one.
    """
    wanted = {
        var: url
        for var, url in overrides.items()
        if url and var in INFRA_URL_CAPABILITIES
    }
    if not wanted:
        return infra_raw
    try:
        parsed = json.loads(infra_raw) if infra_raw else {}
    except ValueError:
        return infra_raw
    if not isinstance(parsed, dict):
        return infra_raw
    for var, url in wanted.items():
        capability = INFRA_URL_CAPABILITIES[var]
        base = _declared_url(parsed, capability, "default")
        if not base:
            raise UnmatchedOverrideError(
                f"{OVERRIDE_PREFIX}{var} is set, but INFRA declares no URL of an "
                f"installed backend ({' / '.join(_URL_OPTIONS)}) on "
                f"{capability}.default for it to replace: declare it in .env.test "
                f"(and install the package of that backend), or unset "
                f"{OVERRIDE_PREFIX}{var} (an app that selects no {capability} has "
                "nothing to override)"
            )
        if not _owned(url):
            # Never the value: it is a URL a person set and may hold a password.
            raise UnmatchedOverrideError(
                f"{OVERRIDE_PREFIX}{var} is no URL of an installed backend: it "
                f"replaces the URL INFRA declares on {capability}.default, so it "
                "must name a server of that backend (install the package of that "
                f"backend), or unset {OVERRIDE_PREFIX}{var}"
            )
        _replace_on_database(parsed, base, url)
    return json.dumps(parsed)


def overrides_infra_can_take(infra_raw: str | None) -> tuple[str, ...]:
    """The override names (``CACHE_URL`` / ``BROKER_URL``) that
    :func:`infra_with_overrides` accepts for ``infra_raw``: those whose capability's
    ``default`` instance declares a URL of an installed backend. The harness raises
    :class:`UnmatchedOverrideError` for any other, so ``sandloom dev up`` writes
    only these. A malformed or missing ``INFRA`` takes none."""
    try:
        parsed = json.loads(infra_raw) if infra_raw else {}
    except ValueError:
        return ()
    if not isinstance(parsed, dict):
        return ()
    return tuple(
        var
        for var, capability in INFRA_URL_CAPABILITIES.items()
        if _declared_url(parsed, capability, "default")
    )


def _replace_on_database(parsed: dict[str, Any], base: str, url: str) -> None:
    """Every ``url`` / ``broker`` of ``parsed`` on the database of ``base`` becomes
    ``url``."""
    for instances in parsed.values():
        for spec in instances.values() if isinstance(instances, dict) else ():
            if not isinstance(spec, dict):
                continue
            for held in _URL_OPTIONS:
                if _same_database(spec.get(held), base):
                    _set_url(spec, held, url)


def _declared_url(parsed: dict[str, Any], capability: str, name: str) -> str | None:
    """The URL of a harness family ``parsed`` declares on instance ``name`` of
    ``capability`` (its first stated option of :data:`_URL_OPTIONS`), if any."""
    instances = parsed.get(capability)
    spec = instances.get(name) if isinstance(instances, dict) else None
    if not isinstance(spec, dict):
        return None
    for option in _URL_OPTIONS:
        stated = spec.get(option)
        if isinstance(stated, str) and _owned(stated):
            return stated
    return None


def _owned(url: str) -> bool:
    """A harness family isolates ``url`` (it is a URL of that family's backend)."""
    return any(family.owns_url(url) for family in families.harness_families())


def _same_database(stated: object, base: str) -> bool:
    """``stated`` is a URL a family owns, on the same server and database as
    ``base`` (the family decides what leaving the database out means)."""
    if not isinstance(stated, str):
        return False
    return any(
        family.owns_url(stated)
        and family.owns_url(base)
        and family.same_database(stated, base)
        for family in families.harness_families()
    )


def _set_url(spec: dict[str, Any], option: str, url: str) -> None:
    """Set ``option`` of ``spec`` to ``url``; a declared ``backend`` of a family's
    URL follows a replaced ``broker``."""
    spec[option] = url
    backend = spec.get("backend")
    if option == "broker" and isinstance(backend, str) and _owned(backend):
        spec["backend"] = url


def explicit_test_overrides(
    environ: Mapping[str, str] | None = None,
    *,
    current: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    """The explicit ``SANDLOOM_TEST_<VAR>`` overrides set in ``environ``.

    ``current`` is the configuration they apply to (default ``environ``: the
    process). ``DATABASE_URL`` is always named. ``SANDLOOM_TEST_CACHE_URL`` /
    ``SANDLOOM_TEST_BROKER_URL`` are merged into ``INFRA`` (the ``INFRA`` entry
    of the result, see :func:`infra_with_overrides`); they set no other
    variable, and raise :class:`UnmatchedOverrideError` where ``INFRA`` has no
    instance for them.
    """
    env = os.environ if environ is None else environ
    cur = env if current is None else current
    out = {
        var: env[OVERRIDE_PREFIX + var]
        for var in DATABASE_VARS
        if env.get(OVERRIDE_PREFIX + var)
    }
    urls = {
        var: env[OVERRIDE_PREFIX + var]
        for var in URL_VARS
        if env.get(OVERRIDE_PREFIX + var)
    }
    if urls:
        infra = cur.get(INFRA_VAR)
        merged = infra_with_overrides(infra, urls)
        if merged is not None and merged != infra:
            out[INFRA_VAR] = merged
    return out


def seed_test_environ(
    file_values: Mapping[str, str | None],
    *,
    skip: frozenset[str] = frozenset(),
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Seed ``environ`` before the settings snapshot: ``.env.test``'s
    ``file_values`` fill only the keys the shell left unset, then each explicit
    ``SANDLOOM_TEST_<VAR>`` override is assigned, beating a shell-exported
    ``<VAR>`` too. Keys in ``skip`` are left alone.

    The override must win: a shell that still exports ``DATABASE_URL`` (a
    developer's PostgreSQL) while ``SANDLOOM_TEST_DATABASE_URL`` names a SQLite
    file would otherwise snapshot settings on PostgreSQL while the fixtures
    use the file, so a feature check (``skip_unless_db_feature``) reads the
    wrong database.
    """
    env = os.environ if environ is None else environ
    for key, value in file_values.items():
        if value is not None and key not in skip:
            env.setdefault(key, value)
    for key, value in explicit_test_overrides(env).items():
        if key not in skip:
            env[key] = value


def apply_test_overrides(
    environ: MutableMapping[str, str] | None = None,
    *,
    create_database: bool = True,
) -> dict[str, str]:
    """Copy ``SANDLOOM_TEST_<VAR>`` onto ``<VAR>`` in ``environ``; returns them.

    The cache / broker overrides are merged into ``INFRA``: see
    :func:`explicit_test_overrides`.

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
    database / backend.
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
    values.update(explicit_test_overrides(current=values))
    changed = rewrite_for_worker(values)
    for var in DATABASE_VARS:
        if var in changed:
            ensure_database(changed[var])
    return {**values, **changed}


#: Seconds ``ensure_database`` waits for the server (an unreachable host
#: fails the run instead of hanging it; risk fix 1).
ENSURE_DATABASE_CONNECT_TIMEOUT = 5


def ensure_database(url: str) -> None:
    """Create the (PostgreSQL) database ``url`` names if missing (once/process).

    Adds the extensions the framework's schemas rely on and marks a database it
    creates as owned by this run (:mod:`sandloom.core.testing.ownership`).
    A SQLite file's directory is created (the harness claims and builds the
    file); other URLs are ignored.
    """
    parts = split_url(url)
    if parts.scheme.startswith("sqlite"):
        _ensure_sqlite_file(url)
        return
    if not parts.scheme.startswith("postgresql"):
        return
    name = parts.path.lstrip("/")
    if not name or name in _created_databases:
        return

    # Sync driver (test-only): callable from any context, even inside a loop.
    def dsn(database: str) -> str:
        plain = parts._replace(scheme="postgresql", path=f"/{database}", query="")
        return urlunsplit(plain)

    created = False
    conn = psycopg2.connect(
        dsn("postgres"), connect_timeout=ENSURE_DATABASE_CONNECT_TIMEOUT
    )
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
    conn = psycopg2.connect(dsn(name), connect_timeout=ENSURE_DATABASE_CONNECT_TIMEOUT)
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


def _ensure_sqlite_file(url: str) -> None:
    file = sqlite_file(url)
    if file is None or str(file) in _created_databases:
        return
    # the file itself is created by its first connection, and claimed (its
    # owner marker) before the harness resets it (ownership.claim_database)
    file.parent.mkdir(parents=True, exist_ok=True)
    _created_databases.add(str(file))


__all__ = [
    "WORKER_ENV",
    "apply_worker_env",
    "ensure_database",
    "reload_test_env",
    "rewrite_for_worker",
    "sqlite_file",
    "worker_database_url",
    "worker_env_values",
    "worker_id",
    "worker_number",
]
