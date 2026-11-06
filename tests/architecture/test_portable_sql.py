"""Architecture rule SANDLOOM-C031 (``portable-sql``): a module and its tests
run SQL every database runs alike.

``func.<name>`` is allowed only from ``PORTABLE_SQL_FUNCTIONS`` (each name is
executed on every shipped database by
``src/sandloom/core/tests/test_portable_sql_functions.py``); raw SQL carries
``# sandloom: raw-sql <reason>``; a test gated by ``skip_unless_db_feature``
runs only where its feature is. Migrations are exempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.archcheck import load_rules, run_checks
from sandloom.core.archcheck.analyzers import find_dialect_imports, find_unportable_sql
from sandloom.core.descriptors.database_forms import PORTABLE_SQL_FUNCTIONS

REPO = Path(__file__).resolve().parents[2]

#: Every business module the repository ships (as test_no_dialect_imports).
MODULE_ROOTS = sorted(
    [p for p in (REPO / "src").glob("sandloom_*") if (p / "__init__.py").exists()]
    + [p for p in (REPO / "examples").glob("*/*/modules/*") if p.is_dir()]
)


def _hits(tmp_path: Path, source: str, *, test_file: bool = False) -> list[str]:
    sample = tmp_path / ("test_sample.py" if test_file else "services.py")
    sample.write_text(source)
    return find_unportable_sql(
        sample, portable_functions=PORTABLE_SQL_FUNCTIONS, test_file=test_file
    )


@pytest.mark.parametrize("root", MODULE_ROOTS, ids=lambda p: p.name)
def test_module_and_its_tests_run_portable_sql(root: Path) -> None:
    offences = [
        f"{path.relative_to(REPO)}: {hit}"
        for path in sorted(root.rglob("*.py"))
        if "migrations" not in path.relative_to(root).parts
        for hit in find_unportable_sql(
            path,
            portable_functions=PORTABLE_SQL_FUNCTIONS,
            test_file="tests" in path.relative_to(root).parts,
        )
    ]
    assert offences == [], "\n".join(offences)


# -- SQL functions -------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy import func\nx = func.now()\n",
        "from sqlalchemy import func\nx = func.current_timestamp()\n",
        "from sqlalchemy import func as f\nx = f.array_agg(T.c.a)\n",
        "import sqlalchemy as sa\nx = sa.func.greatest(1, 2)\n",
        "import sqlalchemy\nx = sqlalchemy.func.now()\n",
        "import sqlalchemy.sql.functions as F\nx = F.func.now()\n",
        "from sqlalchemy.sql import func\nx = func.jsonb_extract_path_text(c, 'a')\n",
        "from sqlalchemy.sql.expression import func\nx = func.unnest(c)\n",
        "from sqlalchemy.sql import functions\nx = functions.now()\n",
        "from sqlalchemy.sql.functions import now\nx = now()\n",
        "from sqlalchemy.sql.functions import now as at\nx = at()\n",
        "from sqlalchemy import *\nx = func.random()\n",
        "from sqlalchemy import func\nx = func.pg_catalog.now()\n",
        "from sqlalchemy import func\nx = func.now  # referenced, not called\n",
        "def f():\n    from sqlalchemy import func\n    return func.now()\n",
    ],
)
def test_a_sql_function_outside_the_list_is_found(tmp_path: Path, source: str) -> None:
    assert len(_hits(tmp_path, source)) == 1, source


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy import func, select\nq = select(func.count()).where(a)\n",
        "import sqlalchemy as sa\nq = sa.func.coalesce(sa.func.sum(c), 0)\n",
        "from sqlalchemy import func as f\nq = f.length(c) == f.abs(d)\n",
        "from sqlalchemy.sql.functions import count\nq = count()\n",
        "from sqlalchemy import func\nq = func.count().label('n')\n",
        # Not SQLAlchemy's func: an unrelated object named func.
        "func = object()\nx = func.now()\n",
        "from sandloom.sdk.data import WallClock\nx = WallClock()\n",
    ],
)
def test_portable_functions_pass(tmp_path: Path, source: str) -> None:
    assert _hits(tmp_path, source) == [], source


@pytest.mark.parametrize(
    ("source", "fix"),
    [
        ("from sqlalchemy import func\nx = func.now()\n", "WallClock()"),
        ("from sqlalchemy import func\nx = func.array_agg(c)\n", "aggregate_list()"),
        ("from sqlalchemy import func\nx = func.jsonb_path_query(c)\n", "json_path()"),
        ("from sqlalchemy import func\nx = func.to_tsvector(c)\n", "fulltext_index()"),
        ("from sqlalchemy import func\nx = func.greatest(a, b)\n", "CASE"),
        ("from sqlalchemy import func\nx = func.lower(c)\n", "CaseInsensitiveText"),
        ("from sqlalchemy import func\nx = func.upper(c)\n", "CaseInsensitiveText"),
        ("from sqlalchemy import func\nx = func.avg(c)\n", "cast it explicitly"),
        ("from sqlalchemy import func\nx = func.soundex(c)\n", "feature form"),
    ],
)
def test_the_finding_names_the_fix(tmp_path: Path, source: str, fix: str) -> None:
    (hit,) = _hits(tmp_path, source)
    assert fix in hit, hit


def test_a_star_import_of_the_functions_module_hides_them(tmp_path: Path) -> None:
    (hit,) = _hits(tmp_path, "from sqlalchemy.sql.functions import *\nx = now()\n")
    assert "hides the SQL functions" in hit


def test_findings_are_in_line_order(tmp_path: Path) -> None:
    source = (
        "from sqlalchemy import func, text\n"
        "a = text('SELECT a FROM t')\n"
        "b = func.now()\n"
        "c = (func.now(), text('SELECT 1'))\n"
    )
    lines = [hit.split(":")[0] for hit in _hits(tmp_path, source)]
    assert lines == ["Line 2", "Line 3", "Line 4", "Line 4"]


# -- raw SQL -------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy import text\nq = text('SELECT a FROM t')\n",
        "from sqlalchemy import text as t\nq = t('SELECT a FROM t')\n",
        "import sqlalchemy as sa\nq = sa.text('SELECT a FROM t')\n",
        "from sqlalchemy import literal_column\nq = literal_column('a + 1')\n",
        "from sqlalchemy import DDL\nq = DDL('CREATE INDEX i ON t (a)')\n",
        "from sqlalchemy.schema import DDL\nq = DDL('ANALYZE t')\n",
        "async def f(c):\n    await c.exec_driver_sql('SELECT 1')\n",
        "q = column.op('@>')(other)\n",
        "q = column.bool_op('~')(other)\n",
        "from sqlalchemy import *\nq = text('SELECT a FROM t')\n",
        "from sqlalchemy import text\nq = text(f'SELECT {a} FROM t')\n",
        "q = insert(T).prefix_with('OR IGNORE')\n",
        "q = select(T).suffix_with('FOR UPDATE NOWAIT')\n",
        "q = select(T).with_hint(T, 'USE INDEX (ix_a)')\n",
        "q = select(T).prefix_with(f'/*+ {hint} */')\n",
    ],
)
def test_raw_sql_is_found(tmp_path: Path, source: str) -> None:
    (hit,) = _hits(tmp_path, source)
    assert "raw SQL" in hit and "# sandloom: raw-sql" in hit, hit


@pytest.mark.parametrize(
    "source",
    [
        # The marker on a line of the call or on the line above it.
        "from sqlalchemy import text\n"
        "q = text('SELECT a FROM t')  # sandloom: raw-sql reviewed: plain SELECT\n",
        "from sqlalchemy import text\n"
        "# sandloom: raw-sql the planner check reads EXPLAIN\n"
        "q = text('EXPLAIN SELECT a FROM t')\n",
        "from sqlalchemy import text\n"
        "q = text(\n"
        "    'SELECT a FROM t'  # sandloom: raw-sql a multi-line call\n"
        ")\n",
        # Literal shapes every database reads alike.
        "from sqlalchemy import text\nc = Column(server_default=text('0'))\n",
        "from sqlalchemy import text\nc = Column(server_default=text('-1.5'))\n",
        "from sqlalchemy import text\nc = Column(server_default=text(\"'draft'\"))\n",
        "from sqlalchemy import literal_column\nc = literal_column('NULL')\n",
        "from sqlalchemy import text\nc = Column(server_default=text(\"'it''s'\"))\n",
        # A clause method with an expression, not text.
        "q = insert(T).prefix_with(expr)\n",
        "from sqlalchemy import text\nc = Column(server_default=text('false'))\n",
        # Not SQLAlchemy's text.
        "def text(s): return s\nq = text('SELECT a FROM t')\n",
    ],
)
def test_marked_or_literal_raw_sql_passes(tmp_path: Path, source: str) -> None:
    assert _hits(tmp_path, source) == [], source


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy import text\nq = text('SELECT 1')  # sandloom: raw-sql\n",
        "from sqlalchemy import text\nq = text('SELECT 1')  # sandloom: raw-sql -\n",
        "from sqlalchemy import text\n"
        "# sandloom: raw-sql two lines above does not count\n"
        "\n"
        "q = text('SELECT 1')\n",
        # A trailing marker on the previous statement is that statement's.
        "from sqlalchemy import text\n"
        "a = text('SELECT 1')  # sandloom: raw-sql reviewed\n"
        "b = text('SELECT 2')\n",
    ],
)
def test_a_marker_needs_a_reason_on_the_call(tmp_path: Path, source: str) -> None:
    assert len(_hits(tmp_path, source)) == 1, source


def test_a_postgresql_cast_is_not_admitted_by_the_marker(tmp_path: Path) -> None:
    (hit,) = _hits(
        tmp_path,
        "from sqlalchemy import text\n"
        "q = text('SELECT id::text FROM t')  # sandloom: raw-sql I know\n",
    )
    assert "PostgreSQL cast (::)" in hit and "sa.cast" in hit


def test_the_cast_moved_from_c028(tmp_path: Path) -> None:
    """C028 no longer reports ``::``: the finding is C031's alone."""
    sample = tmp_path / "models.py"
    sample.write_text("from sqlalchemy import text\ntext('SELECT id::text FROM t')\n")
    assert find_dialect_imports(sample) == []
    assert len(_hits(tmp_path, sample.read_text())) == 1


# -- tests: the database-feature marker ----------------------------------------

_PG_ONLY = "from sqlalchemy import func, text\n"


@pytest.mark.parametrize(
    "source",
    [
        _PG_ONLY + "from sandloom.sdk.testing import skip_unless_db_feature\n"
        "@skip_unless_db_feature('arrays')\n"
        "async def test_a(db):\n"
        "    await db.execute(text('SELECT a FROM t'))\n"
        "    await db.scalar(func.array_agg(c))\n",
        _PG_ONLY + "import pytest\n"
        "@pytest.mark.sandloom_db_feature('arrays')\n"
        "async def test_a(db):\n"
        "    await db.execute(text('SELECT a FROM t'))\n",
        _PG_ONLY + "from sandloom.sdk.testing import skip_unless_db_feature\n"
        "@skip_unless_db_feature('json_contains')\n"
        "class TestJson:\n"
        "    async def test_a(self, db):\n"
        "        await db.execute(text('SELECT a FROM t'))\n",
        _PG_ONLY + "from sandloom.sdk.testing import skip_unless_db_feature\n"
        "pytestmark = [skip_unless_db_feature('skip_locked')]\n"
        "async def test_a(db):\n"
        "    await db.execute(text('SELECT a FROM t'))\n",
        # The raw-sql marker serves tests too.
        _PG_ONLY + "async def test_a(db):\n"
        "    # sandloom: raw-sql EXPLAIN reads the planner\n"
        "    await db.execute(text('EXPLAIN SELECT a FROM t'))\n",
    ],
)
def test_a_test_tied_to_a_feature_passes(tmp_path: Path, source: str) -> None:
    assert _hits(tmp_path, source, test_file=True) == [], source


def test_the_feature_marker_covers_only_its_test(tmp_path: Path) -> None:
    source = (
        _PG_ONLY + "from sandloom.sdk.testing import skip_unless_db_feature\n"
        "@skip_unless_db_feature('arrays')\n"
        "async def test_a(db):\n"
        "    await db.execute(text('SELECT a FROM t'))\n"
        "async def test_b(db):\n"
        "    await db.execute(text('SELECT b FROM t'))\n"
    )
    (hit,) = _hits(tmp_path, source, test_file=True)
    assert hit.startswith("Line 7:"), hit


def test_the_feature_marker_does_not_serve_module_code(tmp_path: Path) -> None:
    source = (
        _PG_ONLY + "from sandloom.sdk.testing import skip_unless_db_feature\n"
        "@skip_unless_db_feature('arrays')\n"
        "async def load(db):\n"
        "    await db.execute(text('SELECT a FROM t'))\n"
    )
    assert len(_hits(tmp_path, source)) == 1


# -- through run_checks (what ``sandloom check`` executes) ----------------------


def _app(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "src" / "acme" / "modules"
    (pkg / "orders").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "orders" / "__init__.py").write_text("")
    for name, source in files.items():
        path = pkg / "orders" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: src/acme/modules\n"
        "modules_package: acme.modules\n"
        "public_api:\n  - sandloom.sdk\n"
    )
    return manifest


def test_the_rule_reads_module_code_and_tests_not_migrations(tmp_path: Path) -> None:
    now = "from sqlalchemy import func\nx = func.now()\n"
    manifest = _app(
        tmp_path,
        {
            "services.py": now,
            "tests/test_orders.py": now,
            "tests/conftest.py": "from sqlalchemy import text\nq = text('SELECT 1')\n",
            "migrations/versions/0001_init.py": now
            + "from sqlalchemy import text\nq = text('CREATE INDEX i ON t (a)')\n",
        },
    )
    violations = run_checks(load_rules(manifest), only=["portable-sql"])
    found = sorted(
        (v.file.relative_to(tmp_path / "src/acme/modules/orders").as_posix(), v.code)
        for v in violations
    )
    assert found == [
        ("services.py", "SANDLOOM-C031"),
        ("tests/conftest.py", "SANDLOOM-C031"),
        ("tests/test_orders.py", "SANDLOOM-C031"),
    ]
    assert all(v.severity == "error" for v in violations)


def test_the_rule_honours_the_general_waiver(tmp_path: Path) -> None:
    manifest = _app(
        tmp_path,
        {
            "services.py": "from sqlalchemy import func\n"
            "x = func.replace(c, 'a', 'b')  # sandloom: allow portable-sql "
            "replace() is in both databases\n"
        },
    )
    assert run_checks(load_rules(manifest), only=["portable-sql"]) == []


# -- asking which database runs the code ---------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "def f(db):\n    return db.get_bind().dialect.name == 'sqlite'\n",
        "def f(engine):\n    return engine.dialect.name\n",
        "def f(url):\n    return url.get_backend_name()\n",
        "def f(bind):\n    return dialect_name(bind)\n",
    ],
)
def test_reading_the_dialect_is_found(tmp_path: Path, source: str) -> None:
    for test_file in (False, True):
        (hit,) = _hits(tmp_path, source, test_file=test_file)
        assert "asks which database" in hit, hit
        assert 'skip_unless_db_feature("concurrent_writers")' in hit, hit
        assert "has_serial_writers" not in hit, hit


@pytest.mark.parametrize(
    "source",
    [
        "URL = 'postgresql://user:secret@db/app'\n",
        "def f():\n    return create_async_engine('postgresql+asyncpg://u@h/d')\n",
        "def f():\n    return f'sqlite+aiosqlite:///{path}'\n",
        "from sqlalchemy import create_mock_engine\n"
        "d = create_mock_engine('postgresql+asyncpg://', lambda *a: None).dialect\n",
        "import pytest\n"
        "@pytest.mark.skipif(False, reason='x')\n"
        "@skip_unless_db_feature('arrays')\n"
        "def test_x():\n    assert 'postgresql://' not in leaked()\n",
    ],
)
def test_a_database_url_literal_is_found(tmp_path: Path, source: str) -> None:
    for test_file in (False, True):
        (hit,) = _hits(tmp_path, source, test_file=test_file)
        assert "names a backend" in hit and "configuration" in hit, hit


@pytest.mark.parametrize(
    "source",
    [
        "# sandloom: raw-sql the production URL's shape, parsed only\n"
        "URL = 'postgresql+asyncpg://u@h/d'\n",
        "TEXT = 'see the postgresql:// docs'\n",
        "REDIS = 'redis://127.0.0.1:6379/0'\n",
        "def f(settings):\n    return make_url(settings.database_url).drivername\n",
    ],
)
def test_a_marked_or_other_url_passes(tmp_path: Path, source: str) -> None:
    assert _hits(tmp_path, source, test_file=True) == [], source


@pytest.mark.parametrize(
    "source",
    [
        # Compiling for the bind's dialect asks nothing.
        "def f(stmt, db):\n    return stmt.compile(dialect=db.bind.dialect)\n",
        "def f(db):\n"
        "    # sandloom: raw-sql PostgreSQL's planner (no query-plan feature)\n"
        "    return db.bind.dialect.name == 'postgresql'\n",
        "from sandloom.sdk.testing import has_optimized_reads\n"
        "def f(engine):\n    return has_optimized_reads(engine)\n",
    ],
)
def test_asking_for_a_capability_passes(tmp_path: Path, source: str) -> None:
    assert _hits(tmp_path, source, test_file=True) == [], source


def test_a_feature_marker_does_not_admit_reading_the_dialect(tmp_path: Path) -> None:
    source = (
        "from sandloom.sdk.testing import skip_unless_db_feature\n"
        "@skip_unless_db_feature('arrays')\n"
        "async def test_a(db):\n"
        "    assert db.bind.dialect.name == 'postgresql'\n"
    )
    (hit,) = _hits(tmp_path, source, test_file=True)
    assert hit.startswith("Line 4:"), hit
    pytestmark = (
        "from sandloom.sdk.testing import skip_unless_db_feature\n"
        "pytestmark = skip_unless_db_feature('arrays')\n"
        "def test_a(db):\n    assert db.bind.dialect.name == 'postgresql'\n"
    )
    assert len(_hits(tmp_path, pytestmark, test_file=True)) == 1
