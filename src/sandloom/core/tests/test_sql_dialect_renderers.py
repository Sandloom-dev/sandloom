"""The built-in SQL dialect renderers pass the driver kit's conformance suite,
and the suite fails a renderer that breaks the contract."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from sandloom.core.drivers.sql_dialects import POSTGRESQL, SQLITE, PostgresqlSql
from sandloom.driverkit import SqlDialect
from sandloom.driverkit.testing import SqlDialectConformance


class TestPostgresqlRenderer(SqlDialectConformance):
    renderer: ClassVar[SqlDialect] = POSTGRESQL
    dialect: ClassVar[Any] = postgresql.dialect()  # type: ignore[no-untyped-call]


class TestSqliteRenderer(SqlDialectConformance):
    renderer: ClassVar[SqlDialect] = SQLITE
    dialect: ClassVar[Any] = sqlite.dialect()


class _HalfSearch(PostgresqlSql):
    """Advertises a search index but cannot write its columns."""

    def search_operand(self, column: str, case_insensitive: bool) -> None:  # type: ignore[override]
        return None


class _WrongOptions(PostgresqlSql):
    """Writes another database's partial-index option."""

    def partial_index_options(self, where: Any) -> dict[str, Any]:
        return {"sqlite_where": where}


@pytest.mark.parametrize(
    ("renderer", "check"),
    [
        (_HalfSearch(), "test_search_index_is_whole_or_absent"),
        (_WrongOptions(), "test_partial_index_options_restrict_the_index"),
    ],
)
def test_the_suite_fails_a_renderer_that_breaks_the_contract(
    renderer: SqlDialect, check: str
) -> None:
    class Broken(SqlDialectConformance):
        pass

    Broken.renderer = renderer
    Broken.dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    with pytest.raises(AssertionError):
        getattr(Broken(), check)()
