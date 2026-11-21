"""SANDLOOM-C036 yield-inside-unscoped and SANDLOOM-C037 engine-in-module.

C036: a generator that yields inside ``with unscoped():`` hands the tenant
bypass to its caller until it closes (PR #338 red-team, surface B #1). A
``@contextmanager`` hands it over on purpose and is exempt.

C037: an engine a module builds itself skips ``install_write_admission`` (the
compiled-statement check, write admission) and the tenant_rls session setup.
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
    "contextmanager": (
        "from contextlib import contextmanager\n"
        "@contextmanager\ndef scope():\n    with unscoped():\n        yield\n"
    ),
    "asynccontextmanager call form": (
        "import contextlib\n"
        "@contextlib.asynccontextmanager\nasync def scope():\n"
        "    with unscoped():\n        yield\n"
    ),
    "yield in a nested function": (
        "def rows(db):\n    with unscoped():\n"
        "        def later():\n            yield 1\n        return later\n"
    ),
    "yield in a lambda-free comprehension": (
        "def rows(db):\n    with unscoped():\n        data = [1 for _ in db]\n"
        "    return data\n"
    ),
    "another context manager": (
        "def rows(db, lock):\n    with lock:\n        yield 1\n"
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
def test_a_yield_outside_unscoped_or_in_a_context_manager_passes(
    tmp_path: Path, case: str
) -> None:
    assert find_yield_inside_unscoped(_write(tmp_path, _YIELD_OK[case])) == []


def test_an_aliased_unscoped_import_is_followed(tmp_path: Path) -> None:
    path = tmp_path / "service.py"
    path.write_text(
        "from sandloom.core.di import unscoped as system_scope\n"
        "def rows():\n    with system_scope():\n        yield 1\n"
    )
    assert find_yield_inside_unscoped(path)


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
}


@pytest.mark.parametrize("case", list(_ENGINE_BAD))
def test_an_engine_built_in_a_module_is_reported(tmp_path: Path, case: str) -> None:
    path = tmp_path / "service.py"
    path.write_text(_ENGINE_BAD[case])
    found = find_engine_constructors(path)
    assert found and all("engine" in line for line in found), found


@pytest.mark.parametrize("case", list(_ENGINE_OK))
def test_a_module_without_an_engine_factory_passes(tmp_path: Path, case: str) -> None:
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
        "public_api:\n  - sandloom.core.di\n"
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
            "safe.py": _IMPORT + _YIELD_OK["contextmanager"],
        },
    )
    violations = run_checks(load_rules(manifest), only=["yield-inside-unscoped"])
    assert [(v.rule, v.file.name) for v in violations] == [
        ("yield-inside-unscoped", "service.py")
    ]
    assert violations[0].code == "SANDLOOM-C036"


def test_engine_in_module_runs_in_sandloom_check_including_migrations(
    tmp_path: Path,
) -> None:
    manifest = _app(
        tmp_path,
        {
            "service.py": "from sqlalchemy.ext.asyncio import create_async_engine\n",
            "migrations/versions/0001.py": "from sqlalchemy import create_engine\n",
        },
    )
    violations = run_checks(load_rules(manifest), only=["engine-in-module"])
    assert sorted(v.file.name for v in violations) == ["0001.py", "service.py"]
    assert {v.code for v in violations} == {"SANDLOOM-C037"}


@pytest.mark.parametrize(
    ("code", "slug"),
    [("SANDLOOM-C036", "yield-inside-unscoped"), ("SANDLOOM-C037", "engine-in-module")],
)
def test_the_codes_are_in_the_catalogue_with_their_rules(code: str, slug: str) -> None:
    entry = next(e for e in CATALOGUE if e.code == code)
    assert entry.slug == slug and entry.kind == "check"
