"""Architecture rule: infrastructure backends are constructed ONLY in drivers.

The whole point of the infra plugin layer is that infrastructure is reached
through capability contracts and constructed by a single owner — the driver.
No other code (a business module, a core helper, a "legacy global") may call a
concrete backend constructor directly; if it does, that backend is no longer
swappable by configuration and the "infra-agnostic" promise is a fiction.

This test fails if a backend constructor is called outside ``drivers/``.
Every call is resolved through the file's import-alias table before it is
matched (``import X as Y``, ``import a.b``, ``from X import Z as W``, an
attribute chain, a plain ``name = <sdk attribute>`` rebinding), so a renamed
import does not hide a construction. Caught, whatever the spelling:

* a call that resolves into a backend SDK (``redis``, ``aioredis``,
  ``psycopg2``, ``psycopg``, ``asyncpg``, ``boto3``, ``botocore``,
  ``aiobotocore``, ``celery``, ``kombu``) and ends in ``from_url``,
  ``create_pool``, ``create_client``, ``get_session``, ``client``,
  ``resource``, ``Session``, ``Redis``, ``StrictRedis``, ``Celery``,
  ``*ConnectionPool``, or ``connect`` on ``psycopg2`` / ``psycopg`` /
  ``asyncpg`` (``celery.signals.x.connect`` is a callback registration, not a
  connection) (``redis.asyncio.from_url``,
  ``redis.asyncio.ConnectionPool.from_url``, ``BlockingConnectionPool(...)``,
  ``pg.connect`` after ``import psycopg2 as pg``, ``connect()`` after
  ``from psycopg2 import connect``, ``boto3.client``, ``asyncpg.create_pool``);
* ``<anything>.Redis(...)`` / ``StrictRedis(...)`` / ``Redis.from_url(...)`` on
  a receiver the file did not import (``self.redis_module.Redis(...)``);
* ``create_async_engine`` (SQLAlchemy, any alias or receiver), ``Storage(...)``
  (filesystem object store) and ``Celery(...)`` by bare name or alias.

NOT caught: a construction behind a variable (``f = make_factory(); f()``), a
call reached through ``getattr`` or ``importlib.import_module(...).connect``,
and an alias of a value that is not an SDK attribute. The SDK import itself is
the other ratchet (``test_kernel_backend_imports.py``).

The legacy ``sandloom.core.{db,cache,queue,storage}`` modules are deliberately NOT
exempt: after the inversion they are thin shims that forward to the driver and
must contain no construction. There are NO exceptions: the Celery WORKER
entrypoint (``sandloom.core.worker``) resolves its app through the taskqueue
capability (the driver builds it), and modules declare worker tasks with the
app-agnostic ``@shared_task`` in ``<module>/tasks.py`` — nobody else calls
``Celery(...)``.

Ratchet (ADR-0016, slices ``idk-ratchets`` and ``idk-ratchets-close``): the
PostgreSQL ops tools and the test harness connect with ``psycopg2.connect`` and
the dev services with ``redis.Redis``; ``SEEDED`` lists the sites that stay by
file and constructor (never by line), each with why. It is exact: a new site or
a higher count fails, and so does a stale entry (fewer sites than listed, or
none), so the change that removes a site drops its entry. The ops tools and the
harness name ``psycopg2`` through ``sandloom.core.migrations.pg_sync`` (the
lazy modules ``psycopg2``, ``errors``, ``extras``, ``sql``); the alias table
resolves those names to the SDK, so ``psycopg2.connect`` through it counts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"
DRIVERS_DIR = SANDLOOM_ROOT / "core" / "drivers"

# Bare-name (or aliased-name) constructor calls, e.g. ``create_async_engine(...)``.
FORBIDDEN_NAME_CALLS = {
    "create_async_engine",
    "Celery",
    "Storage",
    "Redis",
    "StrictRedis",
}
# Backend SDK roots: a call that resolves into one of them is checked below.
BACKEND_SDKS = frozenset(
    {
        "redis",
        "aioredis",
        "psycopg2",
        "psycopg",
        "asyncpg",
        "boto3",
        "botocore",
        "aiobotocore",
        "celery",
        "kombu",
    }
)
# ``connect`` opens a database connection only on these SDKs (on ``celery`` it
# is ``signals.<signal>.connect``, a callback registration).
CONNECT_SDKS = frozenset({"psycopg2", "psycopg", "asyncpg"})
# Last component of a call that resolves into an SDK and builds a connection,
# pool, client or session (``*ConnectionPool`` is matched by suffix).
SDK_CONSTRUCTOR_ATTRS = frozenset(
    {
        "from_url",
        "create_pool",
        "create_client",
        "get_session",
        "client",
        "resource",
        "Session",
    }
)
# ``<anything>.Redis(...)``: ``redis.Redis``, ``redis.asyncio.Redis``,
# ``aioredis.Redis`` on a receiver the file did not import.
FORBIDDEN_ANY_RECEIVER_ATTRS = {"Redis", "StrictRedis"}
# ``<anything>.create_async_engine(...)``.
FORBIDDEN_ANY_RECEIVER_CALLS = {"create_async_engine"}

#: The sync PostgreSQL driver's lazy names: ``from <PG_SYNC> import psycopg2``
#: is the SDK ``psycopg2``, ``errors`` is ``psycopg2.errors``, and so on.
PG_SYNC = "sandloom.core.migrations.pg_sync"
PG_SYNC_NAMES: dict[str, list[str]] = {
    "psycopg2": ["psycopg2"],
    "errors": ["psycopg2", "errors"],
    "extras": ["psycopg2", "extras"],
    "sql": ["psycopg2", "sql"],
}

# Exact: (file relative to src/sandloom, constructor) -> (sites, reason). Every
# entry is a hole in the invariant that stays, with why; a site that is not
# listed, a higher count and a stale entry (fewer sites, or none) all fail.
_OPS = "stays: PostgreSQL ops tool (design.md section 4: psycopg2 stays where the ops tools need it)"
_HARNESS = "stays: PostgreSQL test harness (design.md section 4)"
_DEV_REDIS = (
    "stays: `sandloom dev` pings and inspects the developer's own Redis server "
    "(the optional `redis` extra, imported by name; no client: a TCP check)"
)
SEEDED: dict[tuple[str, str], tuple[int, str]] = {
    ("core/cli/dev/services.py", "Redis"): (1, _DEV_REDIS),
    ("core/cli/dev/services.py", "psycopg2.connect"): (1, _OPS),
    ("core/migrations/backup.py", "psycopg2.connect"): (1, _OPS),
    ("core/testing/connections.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/migrations.py", "psycopg2.connect"): (2, _HARNESS),
    ("core/testing/ownership.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/resources.py", "psycopg2.connect"): (2, _HARNESS),
    ("core/testing/session_checks.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/slots.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/workers.py", "psycopg2.connect"): (2, _HARNESS),
}


def _dotted(node: ast.expr) -> list[str] | None:
    """``a.b.c`` as ``["a", "b", "c"]``; None when the root is not a name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def _resolve(node: ast.expr, table: dict[str, list[str]]) -> list[str] | None:
    parts = _dotted(node)
    if parts is None:
        return None
    return [*table.get(parts[0], parts[:1]), *parts[1:]]


def _alias_table(tree: ast.AST) -> dict[str, list[str]]:
    """Local name -> the dotted path it stands for (imports, then rebindings)."""
    table: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name.split(".")
                else:  # ``import a.b`` binds ``a``
                    root = alias.name.split(".")[0]
                    table[root] = [root]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                table[alias.asname or alias.name] = (
                    PG_SYNC_NAMES[alias.name]
                    if node.module == PG_SYNC and alias.name in PG_SYNC_NAMES
                    else [*node.module.split("."), alias.name]
                )
    for node in ast.walk(tree):  # ``connect = psycopg2.connect``
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Attribute | ast.Name)
        ):
            resolved = _resolve(node.value, table)
            if resolved and resolved[0] in BACKEND_SDKS:
                table[node.targets[0].id] = resolved
    return table


def _construction_name(func: ast.expr, table: dict[str, list[str]]) -> str | None:
    """The constructor a call expression names (the seeded key), or None."""
    resolved = _resolve(func, table)
    if resolved is not None:
        last = resolved[-1]
        in_sdk = resolved[0] in BACKEND_SDKS
        if isinstance(func, ast.Name) and last in FORBIDDEN_NAME_CALLS:
            return last  # ``Celery(...)``, ``cae(...)`` for create_async_engine
        if in_sdk and last in FORBIDDEN_NAME_CALLS:
            return last  # ``redis.asyncio.Redis``, ``celery.Celery``
        if (
            in_sdk
            and last == "from_url"
            and len(resolved) > 1
            and resolved[-2] in FORBIDDEN_ANY_RECEIVER_ATTRS
        ):
            return "Redis"  # ``redis.asyncio.Redis.from_url``
        if in_sdk and (
            last in SDK_CONSTRUCTOR_ATTRS
            or last.endswith("ConnectionPool")
            or (last == "connect" and resolved[0] in CONNECT_SDKS)
        ):
            return f"{resolved[0]}.{last}"
    if isinstance(func, ast.Attribute):
        if func.attr in FORBIDDEN_ANY_RECEIVER_ATTRS | FORBIDDEN_ANY_RECEIVER_CALLS:
            return "Redis" if func.attr in FORBIDDEN_ANY_RECEIVER_ATTRS else func.attr
        if (
            func.attr == "from_url"
            and isinstance(func.value, ast.Attribute | ast.Name)
            and (
                func.value.attr
                if isinstance(func.value, ast.Attribute)
                else func.value.id
            )
            in FORBIDDEN_ANY_RECEIVER_ATTRS
        ):
            return "Redis"  # ``Redis.from_url`` on an unresolved receiver
    return None


def _find_backend_constructions(file_path: Path) -> list[str]:
    tree = ast.parse(file_path.read_text())
    table = _alias_table(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _construction_name(node.func, table)
            if name:
                hits.append(f"Line {node.lineno}: {name}(...)")
    return hits


def _constructor_sites(file_path: Path) -> dict[str, int]:
    """Constructor -> number of call sites (line-free, for the seeded keys)."""
    sites: dict[str, int] = {}
    for hit in _find_backend_constructions(file_path):
        ctor = hit.split(": ", 1)[1].removesuffix("(...)")
        sites[ctor] = sites.get(ctor, 0) + 1
    return sites


def _new_sites(rel: str, sites: dict[str, int]) -> list[str]:
    return [
        f"{ctor}(...) x{count} (allowed {SEEDED.get((rel, ctor), (0, ''))[0]})"
        for ctor, count in sorted(sites.items())
        if count > SEEDED.get((rel, ctor), (0, ""))[0]
    ]


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for py in SANDLOOM_ROOT.glob("**/*.py"):
        if "tests" in py.parts:
            continue
        if DRIVERS_DIR in py.parents:  # drivers ARE the construction site
            continue
        files.append(py)
    return files


@pytest.mark.parametrize(
    "file_path",
    _iter_production_files(),
    ids=lambda p: str(p.relative_to(SANDLOOM_ROOT)),
)
def test_no_backend_construction_outside_drivers(file_path: Path) -> None:
    rel = str(file_path.relative_to(SANDLOOM_ROOT))
    hits = _new_sites(rel, _constructor_sites(file_path))
    if hits:
        pytest.fail(
            f"Backend constructed outside drivers/ in '{rel}':\n"
            + "\n".join(hits)
            + "\n\nInfrastructure must be constructed by its DRIVER (the single "
            "owner), then resolved by logical name through the capability "
            "registry. Move the construction into sandloom/core/drivers/ and "
            "resolve it via the facade / di / a driver-owned factory.",
            pytrace=False,
        )


def _stale_sites(rel: str, sites: dict[str, int]) -> list[str]:
    """Entries of ``rel`` that list more sites than the scan finds."""
    return [
        f"{ctor}(...) listed x{limit}, found x{sites.get(ctor, 0)}"
        for (listed, ctor), (limit, _reason) in sorted(SEEDED.items())
        if listed == rel and sites.get(ctor, 0) < limit
    ]


def _stale_entries(root: Path) -> list[str]:
    """Every stale problem of ``SEEDED``, one line each: a file that is gone (its
    entries all go) or a site count the scan no longer finds."""
    stale: list[str] = []
    for rel in sorted({rel for rel, _ in SEEDED}):
        path = root / rel
        if not path.is_file():
            stale.append(f"{rel}: file is gone, drop its entries")
            continue
        stale.extend(
            f"{rel}: {problem}"
            for problem in _stale_sites(rel, _constructor_sites(path))
        )
    return stale


def test_no_seeded_entry_is_stale() -> None:
    """Every entry matches the scan exactly: the commit that removes a site
    drops or lowers its entry."""
    stale = _stale_entries(SANDLOOM_ROOT)
    assert stale == [], "stale SEEDED entries (lower or drop them):\n" + "\n".join(
        stale
    )


def test_a_seeded_file_that_is_gone_is_reported_as_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deleted file used to raise a bare ``FileNotFoundError`` from the scan."""
    monkeypatch.setitem(SEEDED, ("core/gone.py", "psycopg2.connect"), (1, "stays: x"))
    assert "core/gone.py: file is gone, drop its entries" in _stale_entries(tmp_path)
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "gone.py").write_text("x = 1\n")  # present, no site left
    assert [
        line for line in _stale_entries(tmp_path) if line.startswith("core/gone.py")
    ] == ["core/gone.py: psycopg2.connect(...) listed x1, found x0"]


def test_every_seeded_entry_says_why_it_stays() -> None:
    """Each entry: an existing file, a positive count and ``stays: <reason>``;
    drivers never listed."""
    for (rel, ctor), (sites, reason) in SEEDED.items():
        assert (SANDLOOM_ROOT / rel).is_file(), rel
        assert sites > 0 and ctor.strip(), (rel, ctor)
        assert reason.startswith("stays:") and reason[len("stays:") :].strip(), (
            rel,
            ctor,
            reason,
        )
        assert not rel.startswith("core/drivers/"), rel
    assert list(SEEDED) == sorted(SEEDED), (
        "keep SEEDED sorted by file, then constructor"
    )


def test_analyzer_detects_construction(tmp_path: Path) -> None:
    """Self-test: the analyzer flags each forbidden constructor shape."""
    sample = tmp_path / "leaky.py"
    sample.write_text(
        "def f():\n"
        "    e = create_async_engine('x')\n"
        "    c = redis.from_url('y')\n"
        "    s = Storage()\n"
        "    a = Celery('z')\n"
        "    r = redis.Redis(host='h')\n"
        "    r2 = aioredis.Redis(host='h')\n"
        "    r3 = redis.asyncio.Redis.from_url('u')\n"
        "    r4 = Redis()\n"
        "    q = psycopg2.connect('dsn')\n"
    )
    assert len(_find_backend_constructions(sample)) == 9
    assert _constructor_sites(sample)["Redis"] == 4


# Every spelling the first version of this ratchet missed (security review of
# PR #191), plus the alias forms the kernel itself uses.
_EVASIONS = [
    ("import redis.asyncio\nredis.asyncio.from_url('u')\n", "redis.from_url"),
    (
        "import redis.asyncio as redis_asyncio\nredis_asyncio.from_url('u')\n",
        "redis.from_url",
    ),
    (
        "import redis.asyncio\nredis.asyncio.ConnectionPool.from_url('u')\n",
        "redis.from_url",
    ),
    (
        "import redis.asyncio\nredis.asyncio.BlockingConnectionPool(host='h')\n",
        "redis.BlockingConnectionPool",
    ),
    (
        "from redis.asyncio import BlockingConnectionPool as P\nP.from_url('u')\n",
        "redis.from_url",
    ),
    (
        "from redis.asyncio import ConnectionPool\nConnectionPool(host='h')\n",
        "redis.ConnectionPool",
    ),
    ("import psycopg2 as pg\npg.connect('d')\n", "psycopg2.connect"),
    ("from psycopg2 import connect\nconnect('d')\n", "psycopg2.connect"),
    ("from psycopg2 import connect as c\nc('d')\n", "psycopg2.connect"),
    ("import psycopg2\nconnect = psycopg2.connect\nconnect('d')\n", "psycopg2.connect"),
    ("from redis import from_url\nfrom_url('u')\n", "redis.from_url"),
    ("import psycopg\npsycopg.connect('d')\n", "psycopg.connect"),
    ("import aioredis\naioredis.from_url('u')\n", "aioredis.from_url"),
    ("import asyncpg\nasyncpg.create_pool('d')\n", "asyncpg.create_pool"),
    ("import asyncpg\nasyncpg.connect('d')\n", "asyncpg.connect"),
    ("import boto3\nboto3.client('s3')\n", "boto3.client"),
    ("import boto3\nboto3.resource('s3')\n", "boto3.resource"),
    ("import boto3\nboto3.session.Session()\n", "boto3.Session"),
    ("from boto3 import session as s\ns.Session()\n", "boto3.Session"),
    (
        "import botocore.session\nbotocore.session.get_session()\n",
        "botocore.get_session",
    ),
    (
        "import aiobotocore.session as ab\nab.get_session().create_client('s3')\n",
        "aiobotocore.get_session",
    ),
    ("from redis.asyncio import Redis as R\nR(host='h')\n", "Redis"),
    ("from celery import Celery as C\nC('x')\n", "Celery"),
    (
        "from sqlalchemy.ext.asyncio import create_async_engine as cae\ncae('u')\n",
        "create_async_engine",
    ),
    (
        "from sqlalchemy.ext import asyncio as sa\nsa.create_async_engine('u')\n",
        "create_async_engine",
    ),
]


@pytest.mark.parametrize(("source", "ctor"), _EVASIONS)
def test_the_analyzer_resolves_import_aliases(
    tmp_path: Path, source: str, ctor: str
) -> None:
    sample = tmp_path / "evade.py"
    sample.write_text(source)
    assert _constructor_sites(sample).get(ctor, 0) >= 1, source


def test_the_analyzer_spares_a_name_that_is_not_a_backend(tmp_path: Path) -> None:
    sample = tmp_path / "fine.py"
    sample.write_text(
        "import sqlite3\nimport psycopg2.sql\nfrom redis.exceptions import RedisError\n"
        "from celery import signals\nsignals.beat_init.connect(print)\n"
        "from mylib import connect, client\n"
        "sqlite3.connect('x')\nconnect('d')\nclient('s3')\npsycopg2.sql.SQL('x')\n"
        "e = RedisError('x')\n"
    )
    assert _find_backend_constructions(sample) == []


def test_a_seeded_site_passes_a_new_one_fails_and_so_does_a_stale_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(SEEDED, ("core/x.py", "psycopg2.connect"), (2, "stays: x"))
    assert _new_sites("core/x.py", {"psycopg2.connect": 2}) == []
    assert _stale_sites("core/x.py", {"psycopg2.connect": 2}) == []
    assert len(_stale_sites("core/x.py", {"psycopg2.connect": 1})) == 1
    assert len(_stale_sites("core/x.py", {})) == 1
    assert len(_new_sites("core/x.py", {"psycopg2.connect": 3})) == 1
    assert len(_new_sites("core/y.py", {"psycopg2.connect": 1})) == 1
    assert len(_new_sites("core/x.py", {"Redis": 1})) == 1


def test_a_connection_through_pg_sync_counts_as_the_sdk(tmp_path: Path) -> None:
    """The ops tools and the harness name ``psycopg2`` through its lazy module:
    the scan resolves that name to the SDK, so a new connection through it fails."""
    sample = tmp_path / "ops.py"
    sample.write_text(
        "from sandloom.core.migrations.pg_sync import errors, psycopg2 as pg, sql\n"
        "def f(dsn):\n"
        "    pg.connect(dsn)\n"
        "    psycopg2_alias = pg\n"
        "    return errors.DuplicateDatabase, sql.SQL('x')\n"
    )
    assert _constructor_sites(sample) == {"psycopg2.connect": 1}
    other = tmp_path / "other.py"
    other.write_text(
        "from sandloom.core.migrations.urls import psycopg2\n"
        "def f(dsn):\n    psycopg2.connect(dsn)\n"
    )
    assert _constructor_sites(other) == {}
