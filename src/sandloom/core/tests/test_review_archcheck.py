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


@pytest.mark.parametrize(
    ("imports", "expression"),
    [
        ("from sqlalchemy import DDL", "DDL('drop table notes')"),
        ("from sqlalchemy.schema import DDL as Ddl", "Ddl('drop table notes')"),
        ("import sqlalchemy as sa", "sa.DDL('drop table notes')"),
    ],
)
def test_a_ddl_element_needs_explicit_system_scope_like_text(
    tmp_path: Path, imports: str, expression: str
) -> None:
    source = tmp_path / "service.py"
    source.write_text(f"{imports}\ndef query(db):\n    return {expression}\n")
    assert analyzers.find_unsafe_tenant_sql(source)
    source.write_text(
        f"{imports}\nfrom sandloom.core.di import unscoped\n"
        f"def query(db):\n    with unscoped():\n        return {expression}\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source) == []


def test_the_attribute_spelling_of_unscoped_admits_raw_sql_like_the_bare_name(
    tmp_path: Path,
) -> None:
    """C036 accepts ``with tc.unscoped():``; C015 must not report raw SQL in it."""
    source = tmp_path / "service.py"
    source.write_text(
        "import sandloom.sdk.tenant as tc\nfrom sqlalchemy import text\n"
        "def query(db):\n    with tc.unscoped():\n        return text('select 1')\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source) == []
    source.write_text(
        "import sandloom.sdk.tenant as tc\nfrom sqlalchemy import text\n"
        "def query(db):\n    with tc.scoped():\n        return text('select 1')\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source)


_FRAMEWORK_RECEIVERS = {
    "an aliased module import": (
        "import sandloom.sdk.tenant as tc\n",
        "tc.unscoped()",
    ),
    "a from-import of a submodule": (
        "from sandloom.sdk import tenant\n",
        "tenant.unscoped()",
    ),
    "a from-import aliased": (
        "from sandloom import sdk as framework\n",
        "framework.unscoped()",
    ),
    "a dotted receiver of a plain import": (
        "import sandloom.sdk\n",
        "sandloom.sdk.unscoped()",
    ),
}


@pytest.mark.parametrize("case", list(_FRAMEWORK_RECEIVERS))
def test_unscoped_on_a_framework_module_admits_raw_sql(
    tmp_path: Path, case: str
) -> None:
    imports, scope = _FRAMEWORK_RECEIVERS[case]
    source = tmp_path / "service.py"
    source.write_text(
        f"{imports}from sqlalchemy import text\n"
        f"def query(db):\n    with {scope}:\n        return text('select 1')\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source) == []


_LOOK_ALIKE_RECEIVERS = {
    "a parameter": "def query(db, tc):\n    with tc.unscoped():\n        return text('1')\n",
    "a local object": (
        "def query(db, make):\n    helper = make()\n"
        "    with helper.unscoped():\n        return text('1')\n"
    ),
    "another package's module": (
        "import acme.tenant as tc\n"
        "def query(db):\n    with tc.unscoped():\n        return text('1')\n"
    ),
    "a framework import the file binds again": (
        "import sandloom.sdk.tenant as tc\n"
        "def query(db, make):\n    tc = make()\n"
        "    with tc.unscoped():\n        return text('1')\n"
    ),
    "a framework import shadowed by a parameter": (
        "import sandloom.sdk.tenant as tc\n"
        "def query(db, tc):\n    with tc.unscoped():\n        return text('1')\n"
    ),
    "an attribute of a local object": (
        "def query(db, helper):\n    with helper.tenant.unscoped():\n"
        "        return text('1')\n"
    ),
    "a match capture named like the framework import": (
        "import sandloom.sdk.tenant as tc\n"
        "def query(db, obj):\n    match obj:\n        case object() as tc:\n"
        "            with tc.unscoped():\n                return text('1')\n"
    ),
    "a match star capture named like the framework import": (
        "import sandloom.sdk.tenant as tc\n"
        "def query(db, obj):\n    match obj:\n        case [*tc]:\n"
        "            with tc.unscoped():\n                return text('1')\n"
    ),
    "a match mapping rest named like the framework import": (
        "import sandloom.sdk.tenant as tc\n"
        "def query(db, obj):\n    match obj:\n        case {**tc}:\n"
        "            with tc.unscoped():\n                return text('1')\n"
    ),
    "a walrus in a comprehension rebinding the framework import": (
        "import sandloom.sdk.tenant as tc\n"
        "def query(db, make):\n    names = [(tc := make()) for _ in db]\n"
        "    with tc.unscoped():\n        return text('1')\n"
    ),
}


_TWO_FUNCTIONS = (
    "from sqlalchemy import text\n"
    "import sandloom.sdk.tenant as tc\n"
    "def unrelated(db, tc):\n    return tc\n"
    "def genuine(db):\n    with tc.unscoped():\n        return text('1')\n"
)


def test_a_parameter_named_like_the_import_in_another_function_keeps_the_import(
    tmp_path: Path,
) -> None:
    """The framework import is resolved in the scope of each use: another
    function's parameter named ``tc`` neither hides it from ``genuine`` ..."""
    source = tmp_path / "service.py"
    source.write_text(_TWO_FUNCTIONS)
    assert analyzers.find_unsafe_tenant_sql(source) == []


def test_a_parameter_named_like_the_import_is_no_receiver_in_its_own_function(
    tmp_path: Path,
) -> None:
    """... nor makes ``tc`` in the function that takes it a framework module."""
    source = tmp_path / "service.py"
    source.write_text(
        "from sqlalchemy import text\n"
        "import sandloom.sdk.tenant as tc\n"
        "def genuine(db):\n    with tc.unscoped():\n        return text('1')\n"
        "def lookalike(db, tc):\n    with tc.unscoped():\n        return text('1')\n"
    )
    found = analyzers.find_unsafe_tenant_sql(source)
    assert len(found) == 1 and "Line 8" in found[0], found


def test_a_function_local_framework_import_is_a_receiver_in_that_function_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "from sqlalchemy import text\n"
        "def local(db):\n    import sandloom.sdk.tenant as tc\n"
        "    with tc.unscoped():\n        return text('1')\n"
        "def other(db, tc):\n    with tc.unscoped():\n        return text('1')\n"
    )
    found = analyzers.find_unsafe_tenant_sql(source)
    assert len(found) == 1 and "Line 8" in found[0], found


@pytest.mark.parametrize("case", list(_LOOK_ALIKE_RECEIVERS))
def test_unscoped_on_a_look_alike_receiver_does_not_admit_raw_sql(
    tmp_path: Path, case: str
) -> None:
    source = tmp_path / "service.py"
    source.write_text("from sqlalchemy import text\n" + _LOOK_ALIKE_RECEIVERS[case])
    found = analyzers.find_unsafe_tenant_sql(source)
    assert len(found) == 1 and "raw SQL requires explicit unscoped()" in found[0], found


@pytest.mark.parametrize(
    ("imports", "expression"),
    [
        ("from sqlalchemy import literal_column", "literal_column('1')"),
        ("from sqlalchemy.sql import literal_column as lc", "lc('1')"),
        ("import sqlalchemy as sa", "sa.literal_column('1')"),
    ],
)
def test_a_literal_column_needs_explicit_system_scope_and_no_marker_admits_it(
    tmp_path: Path, imports: str, expression: str
) -> None:
    """``literal_column`` renders its text as written into any statement. The
    portability marker (``# sandloom: raw-sql``) reviews dialect text, never tenant
    reach, so it does not lift this check."""
    source = tmp_path / "service.py"
    source.write_text(f"{imports}\ndef query(db):\n    return {expression}\n")
    assert analyzers.find_unsafe_tenant_sql(source)
    source.write_text(
        f"{imports}\ndef query(db):\n"
        f"    # sandloom: raw-sql dialect text reviewed for portability\n"
        f"    return {expression}  # sandloom: raw-sql same line\n"
    )
    assert analyzers.find_unsafe_tenant_sql(source)
    source.write_text(
        f"{imports}\nfrom sandloom.core.di import unscoped\n"
        f"def query(db):\n    with unscoped():\n        return {expression}\n"
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
