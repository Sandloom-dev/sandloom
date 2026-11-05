"""The kernel names no database (design/infra-agnostic/spec.md, section 4).

Database knowledge belongs to the SQL dialect renderers and the database
driver (``sandloom/core/drivers/**``). Anywhere else in the framework a
dialect import (C028's analyzer), a comparison of ``dialect.name`` or
``.driver``, a PostgreSQL-only ``func.*`` call, or PostgreSQL-only SQL in
``text()`` / ``exec_driver_sql()`` is counted per file.

Also counted: a subscript by a database's name (``dialect_options["postgresql"]``),
a URL or driver-name prefix test (``url.startswith("postgresql")``,
``drivername.startswith("sqlite")``) and a PostgreSQL error-code check
(``exc.pgcode``, ``getattr(exc, "sqlstate")``).

The remaining sites are listed in ``kernel_dialect_allowlist.json``, each with
the reason it stays: an ops or migration path SQLite never reaches
(SANDLOOM-B028), or the test harness's per-database provisioning. The list is
a ratchet: a file may not gain a site, a file not listed may have none, and a
count that dropped must be lowered in the list in the same change, so it
only shrinks.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from sandloom.core.archcheck.analyzers import find_dialect_imports

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "src" / "sandloom"
ALLOWLIST = Path(__file__).with_name("kernel_dialect_allowlist.json")

#: Where database knowledge belongs, and what this check does not cover.
EXEMPT = (
    "core/drivers/",  # the database driver and its renderers
    "driverkit/",  # the API and conformance kit for driver authors
    "core/archcheck/",  # the analyzers name dialects to find them
    "core/migrations/core_chain/",  # the core chain is PostgreSQL's DDL history
)

#: ``<dialect>_<option>=`` keywords, such as ``postgresql_readonly=``.
DIALECT_OPTION_PREFIXES = ("postgresql_", "sqlite_", "mysql_", "mssql_", "oracle_")

#: SQL functions only PostgreSQL has (called as ``func.<name>``).
PG_FUNCTIONS = frozenset(
    {
        "clock_timestamp",
        "array_agg",
        "unnest",
        "set_config",
        "current_setting",
        "hashtext",
        "hashtextextended",
        "greatest",
    }
)
#: PostgreSQL-only SQL inside a string passed to ``text()``/``exec_driver_sql()``:
#: functions and system catalogues (``pg_locks``, ``pg_roles``), casts, RLS DDL.
PG_SQL = re.compile(
    r"\bpg_[a-z_]+\b|set_config\(|current_setting\(|clock_timestamp\(|::\w|"
    r"SKIP LOCKED|ROW LEVEL SECURITY|CREATE POLICY|ANY\(|unnest\(",
    re.IGNORECASE,
)


#: Names a database goes by in a URL, a ``dialect_options`` key or a driver name.
DATABASE_NAMES = (
    "postgresql",
    "postgres",
    "sqlite",
    "mysql",
    "mariadb",
    "mssql",
    "oracle",
)
#: Attributes that hold a PostgreSQL error code (asyncpg ``sqlstate``, psycopg ``pgcode``).
ERROR_CODE_ATTRIBUTES = frozenset({"pgcode", "sqlstate"})


def _names_database(value: object) -> bool:
    return isinstance(value, str) and value.lower().startswith(DATABASE_NAMES)


def _strings(node: ast.AST) -> list[object]:
    if isinstance(node, ast.Constant):
        return [node.value]
    if isinstance(node, ast.Tuple):
        return [e.value for e in node.elts if isinstance(e, ast.Constant)]
    return []


def _is_dialect_attribute(node: ast.AST) -> bool:
    """``<x>.dialect.name`` / ``dialect.name`` / ``<x>.dialect.driver``."""
    if not isinstance(node, ast.Attribute) or node.attr not in ("name", "driver"):
        return False
    target = node.value
    return (isinstance(target, ast.Name) and target.id == "dialect") or (
        isinstance(target, ast.Attribute) and target.attr == "dialect"
    )


def _sql_text(node: ast.Call) -> str | None:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in ("text", "exec_driver_sql") or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        return "".join(
            v.value
            for v in first.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def sites(path: Path) -> int:
    """How many places in ``path`` name a database."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = len(find_dialect_imports(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            _is_dialect_attribute(side) for side in (node.left, *node.comparators)
        ):
            count += 1
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "func"
                and (func.attr in PG_FUNCTIONS or func.attr.startswith("pg_"))
            ):
                count += 1
            sql = _sql_text(node)
            if sql is not None and PG_SQL.search(sql):
                count += 1
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("startswith", "endswith")
                and node.args
                and any(_names_database(v) for v in _strings(node.args[0]))
            ):
                count += 1  # url.startswith("postgresql"), drivername.startswith(...)
            if (
                isinstance(func, ast.Name)
                and func.id in ("getattr", "hasattr")
                and len(node.args) > 1
                and any(v in ERROR_CODE_ATTRIBUTES for v in _strings(node.args[1]))
            ):
                count += 1  # getattr(exc, "pgcode", None)
            if isinstance(func, ast.Attribute) and func.attr == "with_for_update":
                # SKIP LOCKED / FOR SHARE compile to nothing on SQLite.
                count += any(k.arg in ("skip_locked", "read") for k in node.keywords)
        elif isinstance(node, ast.Subscript) and any(
            _names_database(v) for v in _strings(node.slice)
        ):
            count += 1  # dialect_options["postgresql"], dialect_kwargs["sqlite_where"]
        elif isinstance(node, ast.Attribute) and node.attr in ERROR_CODE_ATTRIBUTES:
            count += 1  # exc.pgcode / exc.orig.sqlstate
        elif (
            isinstance(node, ast.keyword)
            and node.arg is not None
            and node.arg.startswith(DIALECT_OPTION_PREFIXES)
            and not node.arg.endswith(("_where", "_using"))  # counted by C028
        ):
            count += 1
    return count


def current() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(EXEMPT) or "/tests/" in f"/{relative}":
            continue
        n = sites(path)
        if n:
            found[relative] = n
    return found


def allowed_sites() -> dict[str, int]:
    """The allowlist's counts; every entry states why its sites stay."""
    entries: dict[str, dict[str, object]] = json.loads(
        ALLOWLIST.read_text(encoding="utf-8")
    )
    unexplained = [
        p for p, e in entries.items() if not str(e.get("reason", "")).strip()
    ]
    assert not unexplained, f"allowlist entries without a reason: {unexplained}"
    return {path: int(str(entry["sites"])) for path, entry in entries.items()}


def test_the_kernel_gains_no_database_specific_site() -> None:
    allowed = allowed_sites()
    found = current()
    grown = {
        path: (allowed.get(path, 0), n)
        for path, n in found.items()
        if n > allowed.get(path, 0)
    }
    assert not grown, (
        "database-specific code outside the driver (allowed, found): "
        f"{grown}. Ask the SQL dialect renderer instead "
        "(sandloom.core.sql_alchemy.portable, design/infra-agnostic/spec.md)."
    )


def test_the_allowlist_only_shrinks() -> None:
    allowed = allowed_sites()
    found = current()
    stale = {
        path: (n, found.get(path, 0))
        for path, n in allowed.items()
        if found.get(path, 0) < n
    }
    assert not stale, (
        "sites were removed: lower kernel_dialect_allowlist.json to "
        f"{ {p: f for p, (_, f) in stale.items()} } (drop the zeros)"
    )


def test_the_check_counts_each_kind_of_site(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from sqlalchemy.dialects import postgresql\n"
        "if engine.dialect.name == 'postgresql': pass\n"
        "stmt = select(func.pg_advisory_xact_lock(1))\n"
        "conn.exec_driver_sql('SELECT set_config(1)')\n"
        "text('SELECT a FROM t WHERE b = :b')\n"
        "q = select(t).with_for_update(skip_locked=True)\n"
        "conn.execution_options(postgresql_readonly=True)\n"
        "where = ix.dialect_options['postgresql'].get('where')\n"
        "if url.startswith(('postgresql', 'postgres')): pass\n"
        "if engine.url.drivername.startswith('sqlite+'): pass\n"
        "if exc.orig.pgcode == '40001': pass\n"
        "code = getattr(exc, 'sqlstate', None)\n"
        "kept = name.startswith('ix_')\n"
        "value = options['timeout']\n"
    )
    assert sites(sample) == 11
