"""Architecture rule SANDLOOM-C028: no business module imports a database dialect.

The driver in configuration decides which database runs a module; a
``sqlalchemy.dialects`` import ties the module to one. The standard modules and
the examples use ``sqlalchemy.Uuid`` and the portable helpers of
``sandloom.sdk.data`` instead. Migrations are exempt: they are one database's
DDL history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import (
    find_bare_aware_datetimes,
    find_dialect_imports,
)

REPO = Path(__file__).resolve().parents[2]

#: Every business module the repository ships: the standard modules and the
#: examples' modules.
MODULE_ROOTS = sorted(
    [p for p in (REPO / "src").glob("sandloom_*") if (p / "__init__.py").exists()]
    + [p for p in (REPO / "examples").glob("*/*/modules/*") if p.is_dir()]
)


def test_the_module_list_is_not_empty() -> None:
    names = {p.name for p in MODULE_ROOTS}
    assert {"sandloom_identity", "sandloom_billing", "sandloom_tenancy"} <= names


@pytest.mark.parametrize("root", MODULE_ROOTS, ids=lambda p: p.name)
def test_module_imports_no_dialect(root: Path) -> None:
    offences = [
        f"{path.relative_to(REPO)}: {hit}"
        for path in sorted(root.rglob("*.py"))
        if "migrations" not in path.relative_to(root).parts
        for hit in find_dialect_imports(path)
    ]
    assert offences == [], "\n".join(offences)


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy.dialects.postgresql import UUID\n",
        "from sqlalchemy.dialects.postgresql import insert as pg_insert\n",
        "from sqlalchemy.dialects import postgresql\n",
        "from sqlalchemy import dialects\n",
        "import sqlalchemy.dialects.postgresql\n",
        "import importlib\nm = importlib.import_module('sqlalchemy.dialects.postgresql')\n",
        "import sqlalchemy as sa\nt = sa.dialects.postgresql.UUID\n",
        "from sqlalchemy import Index\nIndex('i', 'a', postgresql_where='a > 1')\n",
        "from sqlalchemy import Index\nIndex('i', 'a', sqlite_where='a > 1')\n",
        "from sqlalchemy import Index\nIndex('i', 'a', **{'postgresql_where': 'a'})\n",
        "import sqlalchemy as sa\nsa.Column('a', sa.Text, postgresql_using='gin')\n",
        "from sqlalchemy import text\ntext('SELECT id::text FROM t')\n",
    ],
)
def test_the_analyzer_catches_every_spelling(tmp_path: Path, source: str) -> None:
    sample = tmp_path / "models.py"
    sample.write_text(source)
    assert len(find_dialect_imports(sample)) == 1, source


def test_portable_forms_pass(tmp_path: Path) -> None:
    sample = tmp_path / "models.py"
    sample.write_text(
        "from sqlalchemy import Uuid, select\n"
        "from sandloom.sdk.data import CaseInsensitiveText, conflict_insert\n"
    )
    assert find_dialect_imports(sample) == []


def test_an_ordinary_argument_named_like_a_dialect_passes(tmp_path: Path) -> None:
    sample = tmp_path / "settings.py"
    sample.write_text(
        "connect(sqlite_path='a.db')\n"
        "Settings(postgresql_dsn='postgresql://h/d', mysql_charset='utf8')\n"
        "text('SELECT a FROM t WHERE b = :b')\n"
    )
    assert find_dialect_imports(sample) == []


def test_the_kernel_side_of_the_portable_forms_names_no_database() -> None:
    """Only the renderers (drivers) know dialects; the forms ask them."""
    portable = REPO / "src" / "sandloom" / "core" / "sql_alchemy" / "portable.py"
    assert find_dialect_imports(portable) == []


# -- SANDLOOM-C029: time columns read back in UTC on every database -----------


@pytest.mark.parametrize("root", MODULE_ROOTS, ids=lambda p: p.name)
def test_module_declares_no_bare_aware_datetime(root: Path) -> None:
    offences = [
        f"{path.relative_to(REPO)}: {hit}"
        for path in sorted(root.rglob("*.py"))
        if "migrations" not in path.relative_to(root).parts
        for hit in find_bare_aware_datetimes(path)
    ]
    assert offences == [], "\n".join(offences)


@pytest.mark.parametrize(
    ("source", "hits"),
    [
        ("from sqlalchemy import DateTime\nc = DateTime(timezone=True)\n", 1),
        ("import sqlalchemy as sa\nc = sa.DateTime(timezone=True)\n", 1),
        ("from sqlalchemy import DateTime\nc = DateTime(True)\n", 1),
        ("import sqlalchemy as sa\nc = sa.DateTime(True)\n", 1),
        ("from sqlalchemy import TIMESTAMP\nc = TIMESTAMP(timezone=True)\n", 1),
        ("import sqlalchemy as sa\nc = sa.TIMESTAMP(True)\n", 1),
        ("import sqlalchemy as sa\nc = sa.TIMESTAMP()\n", 0),
        ("from sqlalchemy import DateTime\nc = DateTime(False)\n", 0),
        ("from sqlalchemy import DateTime\nc = DateTime()\n", 0),
        ("from sqlalchemy import DateTime\nc = DateTime(timezone=False)\n", 0),
        ("from sandloom.sdk.data import UtcDateTime\nc = UtcDateTime()\n", 0),
    ],
)
def test_the_c029_analyzer(tmp_path: Path, source: str, hits: int) -> None:
    sample = tmp_path / "models.py"
    sample.write_text(source)
    assert len(find_bare_aware_datetimes(sample)) == hits, source
