"""Every module's schema builds on a second database (SQLite).

Modules declare tables through the portable forms of ``sandloom.sdk.data``
(SANDLOOM-C028), so their DDL must build on any database that has a SQL
dialect renderer. This builds each example's models on SQLite, one process
per application (examples share table names); the standard modules' schema
is built the same way in
``tests/integration/with_batteries/test_schema_on_a_second_database_bundled_modules.py``.
It catches a column type or index the second database cannot write (an
``ARRAY``, an unrestricted partial index); a dialect type that subclasses a
generic one (``JSONB`` is a ``JSON``) builds anyway, which C028 covers by
refusing the import. It proves the schemas, not the runtime: the kernel itself still runs on
PostgreSQL (row-level security, the outbox stores, ``clock_timestamp()``), so
running the modules' tests on a second database waits for that to move behind
the driver (tm: infra-pg-features).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_BUILD = """
import importlib, pkgutil, sys
import sqlalchemy as sa
from sandloom.core.orm import Base
for name in sys.argv[1:]:
    package = importlib.import_module(name)
    for found in pkgutil.walk_packages(package.__path__, name + "."):
        if found.name.endswith(".models") or ".models." in found.name:
            importlib.import_module(found.name)
engine = sa.create_engine("sqlite://")
Base.metadata.create_all(engine)
built = set(sa.inspect(engine).get_table_names())
missing = set(Base.metadata.tables) - built
assert not missing, missing
print(len(built))
"""

#: (directory to run in, packages whose models make up the application)
APPLICATIONS = [
    (example, [package.name])
    for example in sorted((REPO / "examples").iterdir())
    if (example / "pyproject.toml").exists()
    for package in example.iterdir()
    if (package / "modules").is_dir()
]


@pytest.mark.parametrize(
    ("root", "packages"),
    APPLICATIONS,
    ids=[root.name for root, _ in APPLICATIONS],
)
def test_the_schema_builds_on_sqlite(root: Path, packages: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _BUILD, *packages],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert int(result.stdout.strip().splitlines()[-1]) > 0


def test_every_example_is_built() -> None:
    examples = {r.name for r, _ in APPLICATIONS}
    assert {"academy", "helpdesk", "linkly", "projecthub", "tutorial"} <= examples
