"""The standard modules' schema builds on a second database (SQLite; moved
from ``tests/architecture/test_schema_on_a_second_database.py``, KB-05c).

Identity, tenancy and billing declare their tables through the portable
forms of ``sandloom.sdk.data`` (SANDLOOM-C028); their models are built on
SQLite in one process, as the architecture suite builds each example's.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: The architecture suite's builder (the same script).
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

BUNDLED = ["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]


def test_the_schema_builds_on_sqlite() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _BUILD, *BUNDLED],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert int(result.stdout.strip().splitlines()[-1]) > 0
