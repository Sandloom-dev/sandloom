from pathlib import Path

import pytest

from sandloom.core.archcheck import analyzers


@pytest.mark.parametrize(
    "expression",
    [
        "text('select * from notes')",
        "Note.__table__",
        "db.exec_driver_sql('delete from notes')",
    ],
)
def test_raw_tenant_sql_requires_explicit_system_scope(
    tmp_path: Path, expression: str
) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "from sqlalchemy import text\ndef query(db):\n    return " + expression + "\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source)
    source.write_text(
        "from sqlalchemy import text\nfrom sandloom.core.di import unscoped as system_scope\ndef query(db):\n    with system_scope():\n        return "
        + expression
        + "\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source) == []


def test_raw_sql_declaration_defaults_do_not_need_a_runtime_scope(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.py"
    source.write_text(
        "from sqlalchemy import text\nclass Note:\n    default = text('0')\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source) == []


@pytest.mark.parametrize(
    "statement",
    [
        'return lambda: db.execute(text("select * from notes"))',
        'return (db.execute(text("select * from notes")) for _ in range(1))',
    ],
)
def test_deferred_sql_does_not_inherit_creation_scope(
    tmp_path: Path,
    statement: str,
) -> None:
    source = tmp_path / "deferred.py"
    source.write_text(
        "from sqlalchemy import text\nfrom sandloom.core.di import unscoped\n"
        "def deferred(db):\n    with unscoped():\n        " + statement + "\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source)


@pytest.mark.parametrize(
    "expression",
    [
        'lambda db: db.execute(text("select * from notes"))',
        '(db.execute(text("select * from notes")) for db in databases)',
    ],
)
def test_module_level_deferred_sql_is_runtime_code(
    tmp_path: Path,
    expression: str,
) -> None:
    source = tmp_path / "deferred.py"
    source.write_text("from sqlalchemy import text\nquery = " + expression + "\n")
    assert analyzers.find_unsafe_tenant_sql(source)


def test_with_context_expressions_run_in_sequential_scopes(tmp_path: Path) -> None:
    source = tmp_path / "contexts.py"
    source.write_text(
        "from sqlalchemy import text\nfrom sandloom.core.di import unscoped\n"
        "async def query(db, context):\n"
        "    with context(await db.execute(text('select * from notes'))), unscoped():\n"
        "        pass\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source)
    source.write_text(
        "from sqlalchemy import text\nfrom sandloom.core.di import unscoped\n"
        "async def query(db, context):\n"
        "    with unscoped(), context(await db.execute(text('select * from notes'))):\n"
        "        pass\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source) == []


def test_deferred_sql_uses_its_own_runtime_scope(tmp_path: Path) -> None:
    source = tmp_path / "deferred.py"
    source.write_text(
        "from sqlalchemy import text\nfrom sandloom.core.di import unscoped\n"
        "def deferred(db):\n"
        "    def run():\n"
        "        with unscoped():\n"
        "            return db.execute(text('select * from notes'))\n"
        "    return run\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source) == []
