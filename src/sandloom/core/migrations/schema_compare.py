"""Migrations match models: the schema a database has against the models.

The comparison behind ``migration_drift`` / ``assert_migrations_match_models``
(``sandloom.core.testing.migrations``): Alembic's autogenerate
(``compare_metadata``) with the options and the chain filter every chain's
``env.py`` uses, so a difference found here is one ``sandloom migrate new``
writes; then each index's definition (access method, operator classes, sort
order, included columns, condition), read back from the chain's database and
from an empty one the models were created in, which autogenerate does not
compare. Each difference is one line a reader can act on.
"""

from __future__ import annotations

import warnings
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, pool
from sqlalchemy.engine import make_url

from sandloom.core.migrations.alembic_support import COMPARE_OPTIONS, chain_includes
from sandloom.core.migrations.migrations import _ensure_extensions
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.orm import Base


def _shown(value: Any) -> str:
    """A compared value as SQL reads it: a server default's text, a type's
    DDL name."""
    value = getattr(value, "arg", value)  # DefaultClause -> its argument
    value = getattr(value, "text", value)  # TextClause -> its SQL
    return repr(value) if isinstance(value, str) or value is None else str(value)


def _describe(diff: Any) -> str:
    """One autogenerate difference as a line a reader can act on."""
    if isinstance(diff, list):  # one column's modifications come grouped
        return "; ".join(_describe(d) for d in diff)
    op = diff[0]
    if op.startswith("modify_"):
        _op, _schema, table, column, _existing, in_database, in_models = diff
        what = op.removeprefix("modify_").replace("default", "server default")
        return (
            f"column {table}.{column}: {what} differs (database "
            f"{_shown(in_database)}, models {_shown(in_models)})"
        )
    side, _, kind = op.partition("_")
    where = (
        "in the models, not created by the chain"
        if side == "add"
        else ("created by the chain, not in the models")
    )
    if kind == "column":
        _op, _schema, table, column = diff
        return f"column {table}.{column.name} ({column.type}): {where}"
    obj = diff[1]
    table = getattr(getattr(obj, "table", None), "name", None)
    on = f" on {table}" if kind != "table" and table else ""
    name = getattr(obj, "name", None) or obj
    return f"{kind} {name}{on}: {where}"


def _indexes(connection: Any, tables: list[str]) -> dict[tuple[str, str], Any]:
    """``{(table, index): reflection}`` for ``tables``, as the dialect
    reflects an index (elements, uniqueness, sort order and its options:
    access method, operator classes, condition, included columns)."""
    inspector = inspect(connection)
    return {
        (table, index["name"]): {k: v for k, v in index.items() if k != "name"}
        for table in tables
        for index in inspector.get_indexes(table)
    }


def _index_definition_drift(
    connection: Any, models: Any, owned: frozenset[str]
) -> list[str]:
    """Indexes whose definition in the chain's database differs from the one
    the models build: the access method, an operator class, a sort order,
    included columns, a partial index's condition. Alembic compares only an
    index's columns, its plain expressions and uniqueness.

    Symmetric: ``models`` is an empty database the owned tables were created
    in from the models, and both sides are read back by the same reflection,
    so the database writes each definition the same way (``IN`` as
    ``= ANY (ARRAY[...])``, casts, parentheses) and nothing is normalized by
    hand. Not compared: an index's collation, which SQLAlchemy does not
    reflect."""
    tables = sorted(owned & set(inspect(connection).get_table_names()))
    in_chain = _indexes(connection, tables)
    from_models = _indexes(models, tables)
    found: list[str] = []
    for key in sorted(in_chain.keys() & from_models.keys()):
        chain_index, model_index = in_chain[key], from_models[key]
        if chain_index == model_index:
            continue
        table, name = key
        differs = ", ".join(
            f"{field} (database {chain_index.get(field)!r}, models "
            f"{model_index.get(field)!r})"
            for field in sorted(chain_index.keys() | model_index.keys())
            if chain_index.get(field) != model_index.get(field)
        )
        found.append(f"index {name} on {table}: definition differs: {differs}")
    return found


def _build_from_models(models_url: str, owned: frozenset[str]) -> None:
    """Create the owned tables in the empty database ``models_url`` from the
    models, with the extensions a new service database gets."""
    url = make_url(to_sync_url(models_url))
    _ensure_extensions(url)
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[t for t in Base.metadata.tables.values() if t.name in owned],
            )
    finally:
        engine.dispose()


def schema_differences(url: str, owned: frozenset[str], models_url: str) -> list[str]:
    """The differences between the database at ``url`` and the models, over
    the tables in ``owned``: Alembic's autogenerate comparison with
    :data:`COMPARE_OPTIONS` and the chain filter (:func:`chain_includes`),
    then each index's definition against the same index built from the models
    in the empty database ``models_url``. One line per difference."""
    _build_from_models(models_url, owned)
    engine = create_engine(to_sync_url(url), poolclass=pool.NullPool)
    models = create_engine(to_sync_url(models_url), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection, models.connect() as models_connection:
            dialect = connection.dialect

            def include_object(
                obj: Any, name: str | None, type_: str, reflected: bool, _: Any
            ) -> bool:
                return chain_includes(
                    owned.__contains__, dialect, obj, name, type_, reflected
                )

            context = MigrationContext.configure(
                connection,
                opts={**COMPARE_OPTIONS, "include_object": include_object},
            )
            with warnings.catch_warnings():
                # Alembic cannot compare an index expression that names an
                # operator class (a search_index's ``gin_trgm_ops``) and says
                # so with this warning; _index_definition_drift compares those
                # indexes as the database reflects them instead.
                warnings.filterwarnings(
                    "ignore",
                    message=r"Expression #\d+ .* operator clause",
                    category=UserWarning,
                )
                diffs = compare_metadata(context, Base.metadata)
            return [
                *(_describe(diff) for diff in diffs),
                *_index_definition_drift(connection, models_connection, owned),
            ]
    finally:
        engine.dispose()
        models.dispose()


__all__ = ["schema_differences"]
