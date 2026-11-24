"""SANDLOOM-C036 unscoped-bypass-leaves-its-block and SANDLOOM-C037
engine-in-module, as ADR-0018 states them.

C036: in a business module ``unscoped`` appears only as the direct context
expression of a ``with`` / ``async with`` item, and nothing inside that block
yields. Every other spelling (a variable, ``ExitStack.enter_context``, an
alias, an attribute, a string, a decorator, a ``@contextmanager`` wrapper)
lets the bypass outlive its block (PR #342 red-team, S1-S3, FP1).

C037: a module reaches the database only through the framework's engine, so
the rule is a list of capabilities, not of spellings: engine / pool factories
and classes, ``sqlalchemy.engine`` beyond result and URL types,
``sqlalchemy.pool``, star imports, DBAPI drivers, ``.raw_connection()``, an
engine's ``.pool``, ``getattr`` on sqlalchemy (S4, S5, FP2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.archcheck import load_rules, run_checks
from sandloom.core.archcheck.analyzers import (
    find_engine_constructors,
    find_yield_inside_unscoped,
)
from sandloom.core.error_codes import CATALOGUE

_IMPORT = "from sandloom.core.di import unscoped\n"

_YIELD_BAD = {
    "sync generator": (
        "def rows(db):\n    with unscoped():\n        yield db.execute()\n"
    ),
    "async generator": ("async def rows(db):\n    with unscoped():\n        yield 1\n"),
    "async with": (
        "async def rows(db):\n    async with unscoped():\n        yield 1\n"
    ),
    "yield from": (
        "def rows(db):\n    with unscoped():\n        yield from db.rows()\n"
    ),
    "nested in try": (
        "def rows(db):\n    with unscoped():\n"
        "        try:\n            yield 1\n        finally:\n            pass\n"
    ),
    "second item of one with": (
        "def rows(db, lock):\n    with lock, unscoped():\n        yield 1\n"
    ),
    "attribute spelling": (
        "def rows(db, tc):\n    with tc.unscoped():\n        yield 1\n"
    ),
    "method of a class": (
        "class Repo:\n    def rows(self):\n        with unscoped():\n"
        "            yield 1\n"
    ),
    "unrelated decorator": (
        "@cached\ndef rows(db):\n    with unscoped():\n        yield 1\n"
    ),
    "context manager nested in a generator": (
        "from contextlib import contextmanager\n"
        "def rows(db):\n"
        "    @contextmanager\n"
        "    def inner():\n        yield\n"
        "    with unscoped():\n        yield 1\n"
    ),
    # S2/S3: a module's own wrapper around the bypass hands it to its caller
    "contextmanager decorator": (
        "from contextlib import contextmanager\n"
        "@contextmanager\ndef scope():\n    with unscoped():\n        yield\n"
    ),
    "asynccontextmanager call form": (
        "import contextlib\n"
        "@contextlib.asynccontextmanager\nasync def scope():\n"
        "    with unscoped():\n        yield\n"
    ),
    "a local look-alike decorator": (
        "def contextmanager(f):\n    return f\n"
        "@contextmanager\ndef scope():\n    with unscoped():\n        yield\n"
    ),
    "an aliased decorator": (
        "from contextlib import contextmanager as cm\n"
        "@cm\ndef scope():\n    with unscoped():\n        yield\n"
    ),
    # the earlier item is entered before a later item's expression runs
    "a later item of the same with": (
        "def rows(db, lock):\n    with unscoped(), lock((yield 1)):\n        pass\n"
    ),
    # a default runs when the ``def`` is evaluated, inside the block: it suspends
    # the ENCLOSING generator
    "a default of a nested function": (
        "def rows(db):\n    with unscoped():\n"
        "        def later(x=(yield 1)):\n            return x\n"
        "        return later\n"
    ),
    "a default of a lambda": (
        "def rows(db):\n    with unscoped():\n"
        "        later = lambda x=(yield 1): x\n"
        "        return later\n"
    ),
    # the ``as`` target is evaluated AFTER the item's context manager is entered
    "an as target of the unscoped item": (
        "def rows(db, sinks):\n    with unscoped() as sinks[(yield 1)]:\n        pass\n"
    ),
    "an as target of the unscoped item, second item": (
        "def rows(db, lock, sinks):\n"
        "    with lock, unscoped() as sinks[(yield 1)]:\n        pass\n"
    ),
}

_YIELD_OK = {
    "yield after the block": (
        "def rows(db):\n    with unscoped():\n        data = db.rows()\n"
        "    yield from data\n"
    ),
    "yield before the block": (
        "def rows(db):\n    yield 1\n    with unscoped():\n        db.rows()\n"
    ),
    "no generator": ("def rows(db):\n    with unscoped():\n        return db.rows()\n"),
    "yield in a nested function": (
        "def rows(db):\n    with unscoped():\n"
        "        def later():\n            yield 1\n        return later\n"
    ),
    "yield in a method of a class defined inside the block": (
        "def rows(db):\n    with unscoped():\n"
        "        class Later:\n            def items(self):\n                yield 1\n"
        "        return Later\n"
    ),
    "yield in a lambda defined inside the block": (
        "def rows(db):\n    with unscoped():\n        later = lambda: (yield 1)\n"
        "    return later\n"
    ),
    "yield in a lambda-free comprehension": (
        "def rows(db):\n    with unscoped():\n        data = [1 for _ in db]\n"
        "    return data\n"
    ),
    "another context manager": (
        "def rows(db, lock):\n    with lock:\n        yield 1\n"
    ),
    "a contextmanager that does not touch the bypass": (
        "from contextlib import contextmanager\n"
        "@contextmanager\ndef scope():\n    yield\n"
    ),
    "a keyword argument named unscoped": (
        "def f(run):\n    return run(unscoped=True)\n"
    ),
    "the word in a docstring": (
        'def f():\n    """Read it unscoped, then close the block."""\n'
    ),
    "async with, then a plain with": (
        "async def f(db, lock):\n    async with unscoped():\n"
        "        with lock:\n            return await db.rows()\n"
    ),
    # an earlier item's expression runs BEFORE ``unscoped()`` is entered
    "an earlier item of the same with": (
        "def rows(db, lock):\n    with lock((yield 1)), unscoped():\n        pass\n"
    ),
    "a default evaluated outside the block": (
        "def rows(db):\n    def later(x=(yield 1)):\n        return x\n"
        "    with unscoped():\n        return later\n"
    ),
    # an earlier item's ``as`` target is evaluated BEFORE ``unscoped()`` is entered
    "an as target of an earlier item": (
        "def rows(db, lock, sinks):\n"
        "    with lock as sinks[(yield 1)], unscoped():\n        pass\n"
    ),
    "an as target after the block": (
        "def rows(db, sinks):\n    with unscoped():\n        pass\n"
        "    with db as sinks[(yield 1)]:\n        pass\n"
    ),
}

#: S1: every spelling that lets the bypass leave the ``with`` it was made for.
_USE_BAD = {
    "a variable": ("def f():\n    cm = unscoped()\n    with cm:\n        pass\n"),
    "ExitStack.enter_context": (
        "from contextlib import ExitStack\n"
        "def f():\n    with ExitStack() as stack:\n"
        "        stack.enter_context(unscoped())\n"
    ),
    "AsyncExitStack.enter_async_context": (
        "from contextlib import AsyncExitStack\n"
        "async def f():\n    async with AsyncExitStack() as stack:\n"
        "        await stack.enter_async_context(unscoped())\n"
    ),
    "manual __enter__": ("def f():\n    u = unscoped()\n    u.__enter__()\n"),
    "a module-level alias": "system = unscoped\n",
    "an attribute of the sdk": ("import sandloom.sdk as sdk\nsu = sdk.unscoped\n"),
    "functools.partial": ("import functools\nrun = functools.partial(unscoped)\n"),
    "getattr with the name": (
        "import sandloom.sdk as sdk\ndef f():\n    return getattr(sdk, 'unscoped')()\n"
    ),
    "the name in a namespace lookup": (
        "import sandloom.sdk as sdk\nsu = sdk.__dict__['unscoped']\n"
    ),
    "a decorator": ("@unscoped()\ndef f():\n    return 1\n"),
    "a call outside a with": "def f():\n    unscoped()\n",
    "a call through an attribute outside a with": (
        "def f(tc):\n    return tc.unscoped()\n"
    ),
    "passed as an argument": "def f(run):\n    return run(unscoped)\n",
    "returned": "def f():\n    return unscoped\n",
    "used in a with item and elsewhere": (
        "def f():\n    with unscoped():\n        pass\n    return unscoped()\n"
    ),
}

_USE_OK = {
    "with": "def f(db):\n    with unscoped():\n        return db.rows()\n",
    "async with": (
        "async def f(db):\n    async with unscoped():\n        return await db.rows()\n"
    ),
    "with and a name": (
        "def f(db):\n    with unscoped() as scope:\n        return db.rows(scope)\n"
    ),
    "attribute spelling": ("def f(tc):\n    with tc.unscoped():\n        return 1\n"),
    "second item of one with": (
        "def f(lock):\n    with lock, unscoped():\n        return 1\n"
    ),
    "inside a method": (
        "class Repo:\n    def rows(self):\n        with unscoped():\n"
        "            return 1\n"
    ),
}


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "service.py"
    path.write_text(_IMPORT + source)
    return path


@pytest.mark.parametrize("case", list(_YIELD_BAD))
def test_a_yield_inside_unscoped_is_reported(tmp_path: Path, case: str) -> None:
    found = find_yield_inside_unscoped(_write(tmp_path, _YIELD_BAD[case]))
    assert len(found) == 1 and "yield inside `with unscoped()`" in found[0], found


@pytest.mark.parametrize("case", list(_YIELD_OK))
def test_a_yield_outside_unscoped_passes(tmp_path: Path, case: str) -> None:
    assert find_yield_inside_unscoped(_write(tmp_path, _YIELD_OK[case])) == []


@pytest.mark.parametrize("case", list(_USE_BAD))
def test_unscoped_used_other_than_as_a_with_item_is_reported(
    tmp_path: Path, case: str
) -> None:
    found = find_yield_inside_unscoped(_write(tmp_path, _USE_BAD[case]))
    assert found and all("`unscoped` may appear only" in line for line in found), found


@pytest.mark.parametrize("case", list(_USE_OK))
def test_unscoped_as_a_direct_with_item_passes(tmp_path: Path, case: str) -> None:
    assert find_yield_inside_unscoped(_write(tmp_path, _USE_OK[case])) == []


def test_an_aliased_unscoped_import_is_followed(tmp_path: Path) -> None:
    path = tmp_path / "service.py"
    path.write_text(
        "from sandloom.core.di import unscoped as system_scope\n"
        "def rows():\n    with system_scope():\n        yield 1\n"
    )
    found = find_yield_inside_unscoped(path)
    # one finding, the yield (line 4): `with system_scope():` itself is not one
    assert len(found) == 1 and found[0].startswith("Line 4: yield inside"), found


def test_an_aliased_unscoped_is_reported_outside_a_with_item(tmp_path: Path) -> None:
    path = tmp_path / "service.py"
    path.write_text(
        "from sandloom.core.di import unscoped as system_scope\n"
        "def rows():\n    return system_scope\n"
    )
    found = find_yield_inside_unscoped(path)
    # one finding, the returned reference (line 3), not the import
    assert len(found) == 1 and found[0].startswith("Line 3: `unscoped` may appear")


_ENGINE_BAD = {
    "create_engine": "from sqlalchemy import create_engine\n",
    "create_async_engine": "from sqlalchemy.ext.asyncio import create_async_engine\n",
    "engine_from_config": "from sqlalchemy import engine_from_config\n",
    "async_engine_from_config": (
        "from sqlalchemy.ext.asyncio import async_engine_from_config\n"
    ),
    "aliased import": "from sqlalchemy import create_engine as make\n",
    "module attribute": "import sqlalchemy as sa\nengine = sa.create_engine('sqlite://')\n",
    "dotted attribute": (
        "import sqlalchemy.ext.asyncio\n"
        "def build():\n    return sqlalchemy.ext.asyncio.create_async_engine('x')\n"
    ),
    "submodule alias": (
        "from sqlalchemy.ext import asyncio as aio\n"
        "def build():\n    return aio.create_async_engine('x')\n"
    ),
    # S4: the other ways to an engine or a pool
    "a star import": "from sqlalchemy import *\n",
    "a star import from a submodule": "from sqlalchemy.ext.asyncio import *\n",
    "getattr on sqlalchemy": (
        "import sqlalchemy as sa\nmake = getattr(sa, 'create_engine')\n"
    ),
    "vars of sqlalchemy": "import sqlalchemy as sa\nnames = vars(sa)\n",
    "the namespace of sqlalchemy": (
        "import sqlalchemy as sa\nmake = sa.__dict__['create_engine']\n"
    ),
    "the Engine class": "from sqlalchemy import Engine\n",
    "the Engine class of the engine module": "from sqlalchemy.engine import Engine\n",
    "the Engine class by attribute": (
        "import sqlalchemy as sa\nengine = sa.engine.Engine(None, None, None)\n"
    ),
    "the pool factory": "from sqlalchemy import create_pool_from_url\n",
    "a pool class": "from sqlalchemy.pool import QueuePool\n",
    "the pool module": "import sqlalchemy.pool as pool\n",
    "the pool module as a name": "from sqlalchemy import pool\n",
    "a pool by attribute": (
        "import sqlalchemy as sa\nqueue = sa.pool.QueuePool(lambda: None)\n"
    ),
    "the pool module held in a name": "import sqlalchemy as sa\nlayer = sa.pool\n",
    "a pool helper by attribute": (
        "import sqlalchemy as sa\nmanaged = sa.pool.manage(object())\n"
    ),
    "a dialect by attribute of the engine module": (
        "import sqlalchemy as sa\ndialect = sa.engine.default.DefaultDialect()\n"
    ),
    "the engine module as a name": "from sqlalchemy import engine\n",
    "an engine module import": "import sqlalchemy.engine\n",
    "a non-value name of the engine module": "from sqlalchemy.engine import Dialect\n",
    "a submodule of the engine module": "from sqlalchemy.engine import default\n",
    "a mock engine": "from sqlalchemy import create_mock_engine\n",
    # S5: the paths that skip even an engine
    "sqlite3": "import sqlite3\nconn = sqlite3.connect(':memory:')\n",
    "psycopg": "import psycopg\n",
    "psycopg from-import": "from psycopg import connect\n",
    "psycopg2 submodule": "import psycopg2.extras\n",
    "asyncpg": "import asyncpg\n",
    "aiosqlite": "import aiosqlite\n",
    "a driver by a constant import_module": (
        "import importlib\nmod = importlib.import_module('psycopg')\n"
    ),
    "the sqlalchemy pool by a constant import_module": (
        "import importlib\nmod = importlib.import_module('sqlalchemy.pool')\n"
    ),
    # a constant dynamic import of sqlalchemy is sqlalchemy
    "create_engine on a constant import_module": (
        "import importlib\n"
        "engine = importlib.import_module('sqlalchemy').create_engine('sqlite://')\n"
    ),
    "create_engine on a constant __import__": (
        "engine = __import__('sqlalchemy').create_engine('sqlite://')\n"
    ),
    "create_async_engine on a name bound to an import_module": (
        "import importlib\n"
        "sa = importlib.import_module('sqlalchemy.ext.asyncio')\n"
        "engine = sa.create_async_engine('sqlite+aiosqlite://')\n"
    ),
    "getattr on a constant import_module of sqlalchemy": (
        "import importlib\n"
        "build = getattr(importlib.import_module('sqlalchemy'), 'create_engine')\n"
    ),
    "raw_connection": "def f(session):\n    return session.get_bind().raw_connection()\n",
    "get_raw_connection": (
        "async def f(conn):\n    return await conn.get_raw_connection()\n"
    ),
    "driver_connection": "def f(conn):\n    return conn.driver_connection\n",
    # the DBAPI connection under a SQLAlchemy connection is the driver's own
    "the DBAPI cursor of a connection": (
        "def f(conn):\n    return conn.connection.cursor()\n"
    ),
    "the DBAPI cursor of a session's connection": (
        "def f(session):\n"
        "    return session.connection().connection.cursor().execute('select 1')\n"
    ),
    "the DBAPI cursor of an awaited connection": (
        "async def f(session):\n"
        "    return (await session.connection()).connection.cursor()\n"
    ),
    # the DBAPI connection of a connection call, with no cursor taken from it
    "the DBAPI connection of a session's connection": (
        "def f(session):\n    return session.connection().connection\n"
    ),
    "the DBAPI connection of an awaited connection": (
        "async def f(session):\n    return (await session.connection()).connection\n"
    ),
    "the DBAPI connection of a connect call": (
        "def f(engine):\n    return engine.connect().connection\n"
    ),
    # SQL run on the DBAPI connection itself
    "execute on the DBAPI connection of a connection": (
        "def f(conn):\n    return conn.connection.execute('select 1')\n"
    ),
    "executemany on the DBAPI connection of a connection": (
        "def f(conn):\n    return conn.connection.executemany('select ?', [(1,)])\n"
    ),
    "executescript on the DBAPI connection of a connection": (
        "def f(conn):\n    return conn.connection.executescript('select 1')\n"
    ),
    # a name bound to the DBAPI connection, in the function that uses it
    "a cursor of a name bound to a connection's DBAPI connection": (
        "def f(sync_conn):\n"
        "    raw = sync_conn.connection\n"
        "    return raw.cursor().execute('x')\n"
    ),
    "execute on a name bound to a connection's DBAPI connection": (
        "def f(sync_conn):\n    raw = sync_conn.connection\n    return raw.execute('x')\n"
    ),
    "an annotated name bound to a connection's DBAPI connection": (
        "def f(conn):\n    raw: object = conn.connection\n    return raw.cursor()\n"
    ),
    "an alias of a name bound to a connection's DBAPI connection": (
        "def f(conn):\n    raw = conn.connection\n    alias = raw\n"
        "    return alias.cursor()\n"
    ),
    "a name bound in an enclosing function": (
        "def f(conn):\n    raw = conn.connection\n"
        "    def g():\n        return raw.cursor()\n    return g\n"
    ),
    "a name bound to a connection's DBAPI connection at module level": (
        "raw = conn.connection\n\ndef f():\n    return raw.execute('x')\n"
    ),
    "an engine's pool": "def f(engine):\n    return engine.pool.connect()\n",
    "the pool of a bind": (
        "def f(session):\n    return session.get_bind().pool.connect()\n"
    ),
    "the pool of a connection's engine": (
        "def f(conn):\n    return conn.engine.pool.connect()\n"
    ),
    # a name assigned from a name that holds sqlalchemy holds it too
    "an alias of sqlalchemy": (
        "import sqlalchemy as sa\nalias = sa\nengine = alias.create_engine('sqlite://')\n"
    ),
    "an annotated alias of sqlalchemy": (
        "import sqlalchemy as sa\nalias: object = sa\n"
        "engine = alias.create_engine('sqlite://')\n"
    ),
    "an alias chain written in reverse order": (
        "import sqlalchemy as sa\nthird = second\nsecond = first\nfirst = sa\n"
        "engine = third.create_engine('sqlite://')\n"
    ),
    "an alias of a constant import_module of sqlalchemy": (
        "import importlib\nsa = importlib.import_module('sqlalchemy')\nalias = sa\n"
        "engine = alias.create_engine('sqlite://')\n"
    ),
    "getattr on an alias of sqlalchemy": (
        "import sqlalchemy as sa\nalias = sa\nmake = getattr(alias, 'create_engine')\n"
    ),
    "the pool of an alias of sqlalchemy": (
        "import sqlalchemy as sa\nalias = sa\nqueue = alias.pool.QueuePool(lambda: None)\n"
    ),
    # scope precision: a spelling the static resolver follows
    "a variadic annotation": (
        "import sqlalchemy as sa\n"
        "def f(*args: sa.create_engine('sqlite://')):\n    return args\n"
    ),
    "a keyword variadic annotation": (
        "import sqlalchemy as sa\n"
        "def f(**kwargs: sa.create_engine('sqlite://')):\n    return kwargs\n"
    ),
    "a comprehension target named like the import": (
        "import sqlalchemy as sa\n"
        "def f(items):\n    names = [sa for sa in items]\n"
        "    return sa.create_engine('sqlite://'), names\n"
    ),
    "a global declaration under a parameter named like the import": (
        "import sqlalchemy as sa\n"
        "def outer(sa):\n    def inner():\n        global sa\n"
        "        return sa.create_engine('sqlite://')\n    return inner\n"
    ),
}

_ENGINE_OK = {
    "an unrelated sqlalchemy import": "from sqlalchemy import select, text\n",
    "a session factory": (
        "from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker\n"
    ),
    "a function named like the factory in another package": (
        "from acme.tools import create_engine_report\n"
    ),
    "an attribute of another name": "def f(db):\n    return db.engine_config()\n",
    # FP2: a method of that name on an object that is not sqlalchemy
    "a create_engine method of a user class": (
        "def f(factory):\n    return factory.create_engine()\n"
    ),
    "a create_engine method on self": (
        "class Registry:\n    def build(self):\n        return self.create_engine()\n"
    ),
    "result types of the engine module": (
        "from sqlalchemy.engine import CursorResult, Result, Row, RowMapping\n"
    ),
    "the URL helpers": (
        "from sqlalchemy.engine import URL, make_url\n"
        "from sqlalchemy.engine.url import make_url as parse\n"
    ),
    "a result type by attribute": (
        "import sqlalchemy as sa\nresult: sa.engine.CursorResult\n"
    ),
    "a pool attribute of something that is no engine": (
        "def f(worker):\n    return worker.pool\n"
    ),
    # a cursor taken from something that is no connection is not the DBAPI's
    "a cursor of a pagination object": ("def f(page):\n    return page.cursor()\n"),
    "a connection attribute of a client, then a cursor of the result": (
        "def f(client):\n    return client.cursor().connection\n"
    ),
    # a connection the module keeps is SQLAlchemy's: its execute is the guarded one
    "execute on a connection the module's own object keeps": (
        "class Repo:\n    def run(self, statement):\n"
        "        return self.connection.execute(statement)\n"
    ),
    "a name bound to the module's own connection, then execute": (
        "class Repo:\n    def run(self, statement):\n"
        "        conn = self.connection\n        return conn.execute(statement)\n"
    ),
    "execute on a connection of its own": (
        "def f(conn, statement):\n    return conn.execute(statement)\n"
    ),
    # a name is judged in the function that binds it
    "a name bound to a DBAPI connection in another function": (
        "def one(conn):\n    raw = conn.connection\n    return raw\n"
        "def two(raw):\n    return raw.execute('x')\n"
    ),
    "a name rebound to something else in the function that uses it": (
        "def one(conn):\n    raw = conn.connection\n    return raw\n"
        "def two(session):\n    raw = session\n    return raw.execute('x')\n"
    ),
    "the session's own execute": (
        "async def f(session, statement):\n"
        "    return await session.execute(statement)\n"
    ),
    "getattr on a model": "def f(model):\n    return getattr(model, 'name')\n",
    "a constant import_module of sqlalchemy used for a select": (
        "import importlib\n"
        "sa = importlib.import_module('sqlalchemy')\n"
        "statement = sa.select(1)\n"
    ),
    "a create_engine method of a constant import_module of another package": (
        "import importlib\n"
        "factory = importlib.import_module('acme.tools').create_engine()\n"
    ),
    "a module named like a driver prefix": "from acme import sqlite3_helpers\n",
    # a result / URL type is a value: reflecting on it is no factory lookup
    "getattr on an allowed result type": (
        "from sqlalchemy.engine import Row\n"
        "def f():\n    return getattr(Row, '__name__')\n"
    ),
    "vars of an aliased allowed result type": (
        "from sqlalchemy.engine import CursorResult as Cursor\n"
        "def f():\n    return vars(Cursor)\n"
    ),
    # an alias is followed only from a name that holds sqlalchemy
    "an alias of sqlalchemy used for a select": (
        "import sqlalchemy as sa\nalias = sa\nstatement = alias.select(1)\n"
    ),
    "an alias of another package's module": (
        "from acme import tools\nalias = tools\nfactory = alias.create_engine()\n"
    ),
    "an alias of a parameter": (
        "def f(factory):\n    alias = factory\n    return alias.create_engine()\n"
    ),
    "an alias chain that never reaches sqlalchemy": (
        "from acme import tools\nthird = second\nsecond = first\nfirst = tools\n"
        "factory = third.create_engine()\n"
    ),
    # an alias belongs to the scope that assigns it
    "a name another function binds to sqlalchemy": (
        "import sqlalchemy as sa\n"
        "def one():\n    alias = sa\n    return alias.select(1)\n"
        "def two(factory):\n    alias = factory\n    return alias.create_engine()\n"
    ),
    "a name the module binds to sqlalchemy, rebound locally": (
        "import sqlalchemy as sa\nalias = sa\n"
        "def two(factory):\n    alias = factory\n    return alias.create_engine()\n"
    ),
}

_ENGINE_BAD_BY_SCOPE = {
    "an alias made in the function that uses it": (
        "import sqlalchemy as sa\n"
        "def one(factory):\n    alias = factory\n    return alias.create_engine()\n"
        "def two():\n    alias = sa\n    return alias.create_engine('sqlite://')\n"
    ),
    "a module alias used in a function that does not rebind it": (
        "import sqlalchemy as sa\nalias = sa\n"
        "def one(factory):\n    other = factory\n    return other.create_engine()\n"
        "def two():\n    return alias.create_engine('sqlite://')\n"
    ),
}


@pytest.mark.parametrize("case", list(_ENGINE_BAD_BY_SCOPE))
def test_an_alias_resolves_in_the_scope_that_uses_it(tmp_path: Path, case: str) -> None:
    path = tmp_path / "service.py"
    path.write_text(_ENGINE_BAD_BY_SCOPE[case])
    found = find_engine_constructors(path)
    assert len(found) == 1 and "Line 7" in found[0], found


@pytest.mark.parametrize("case", list(_ENGINE_BAD))
def test_a_database_reached_around_the_engine_is_reported(
    tmp_path: Path, case: str
) -> None:
    path = tmp_path / "service.py"
    path.write_text(_ENGINE_BAD[case])
    found = find_engine_constructors(path)
    assert found and all("engine" in line for line in found), found


@pytest.mark.parametrize("case", list(_ENGINE_OK))
def test_a_module_that_reaches_the_database_through_its_session_passes(
    tmp_path: Path, case: str
) -> None:
    path = tmp_path / "service.py"
    path.write_text(_ENGINE_OK[case])
    assert find_engine_constructors(path) == []


def _app(tmp_path: Path, files: dict[str, str]) -> Path:
    modules = tmp_path / "acme" / "modules"
    (modules / "orders").mkdir(parents=True)
    (modules / "__init__.py").write_text("")
    (modules / "orders" / "__init__.py").write_text("")
    for name, body in files.items():
        target = modules / "orders" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: acme/modules\n"
        "modules_package: acme.modules\n"
        "public_api:\n  - sandloom.sdk\n"
    )
    return manifest


@pytest.mark.parametrize("rule", ["yield-inside-unscoped", "engine-in-module"])
def test_both_rules_run_in_sandloom_check_and_pass_a_clean_module(
    tmp_path: Path, rule: str
) -> None:
    manifest = _app(tmp_path, {"service.py": "def f():\n    return 1\n"})
    assert run_checks(load_rules(manifest), only=[rule]) == []


def test_yield_inside_unscoped_runs_in_sandloom_check(tmp_path: Path) -> None:
    manifest = _app(
        tmp_path,
        {
            "service.py": _IMPORT + _YIELD_BAD["sync generator"],
            "wrapper.py": _IMPORT + _YIELD_BAD["contextmanager decorator"],
            "safe.py": _IMPORT + _YIELD_OK["yield after the block"],
        },
    )
    violations = run_checks(load_rules(manifest), only=["yield-inside-unscoped"])
    assert sorted((v.rule, v.file.name) for v in violations) == [
        ("yield-inside-unscoped", "service.py"),
        ("yield-inside-unscoped", "wrapper.py"),
    ]
    assert {v.code for v in violations} == {"SANDLOOM-C036"}


def test_a_laundered_bypass_is_reported_where_it_is_made(tmp_path: Path) -> None:
    """S2: the wrapper is the finding, so a generator using it needs none."""
    manifest = _app(
        tmp_path,
        {
            "scope.py": _IMPORT + _YIELD_BAD["contextmanager decorator"],
            "service.py": "from .scope import scope\n"
            "def rows(db):\n    with scope():\n        yield 1\n",
        },
    )
    violations = run_checks(load_rules(manifest), only=["yield-inside-unscoped"])
    assert [v.file.name for v in violations] == ["scope.py"]


def test_engine_in_module_runs_in_sandloom_check_including_migrations(
    tmp_path: Path,
) -> None:
    manifest = _app(
        tmp_path,
        {
            "service.py": "from sqlalchemy.ext.asyncio import create_async_engine\n",
            "migrations/versions/0001.py": "from sqlalchemy import create_engine\n",
            "driver.py": "import sqlite3\n",
            "ok.py": "from sqlalchemy.engine import CursorResult\n",
        },
    )
    violations = run_checks(load_rules(manifest), only=["engine-in-module"])
    assert sorted(v.file.name for v in violations) == [
        "0001.py",
        "driver.py",
        "service.py",
    ]
    assert {v.code for v in violations} == {"SANDLOOM-C037"}


@pytest.mark.parametrize(
    ("code", "slug"),
    [("SANDLOOM-C036", "yield-inside-unscoped"), ("SANDLOOM-C037", "engine-in-module")],
)
def test_the_codes_are_in_the_catalogue_with_their_rules(code: str, slug: str) -> None:
    entry = next(e for e in CATALOGUE if e.code == code)
    assert entry.slug == slug and entry.kind == "check"
