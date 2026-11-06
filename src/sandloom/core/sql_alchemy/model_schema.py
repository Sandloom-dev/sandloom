"""The schema of a database that takes it from the models (SANDLOOM-B028).

A database whose renderer says ``schema_from_models`` (SQLite) runs no
migration chain: the chains are PostgreSQL's DDL history (row security,
``clock_timestamp()``, trigram indexes). Its schema is ``metadata.create_all``
over every mapped table, kernel tables included, at startup.

``create_all`` creates a missing table and never alters an existing one. A
database whose tables differ from the models (a column, its nullability, an
index or a unique constraint added, removed or changed since it was built)
is therefore refused at startup, naming each difference, instead of failing
on the first statement that touches it. Only an explicit signal recreates it
(``DB_RECREATE_ON_MODEL_CHANGE``, which ``sandloom dev run`` sets): the driver
then sets the database aside, never dropping a table in place, and builds a
fresh one. Nothing is ever dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import Index, MetaData, Table, UniqueConstraint, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

# Every kernel table is mapped before the schema is built.
import sandloom.core.sql_alchemy.models
import sandloom.core.sql_alchemy.models.audit  # noqa: F401
from sandloom.core.infra.config import database_instance_url
from sandloom.core.sql_alchemy.portable import dialect_of_url, schema_from_models

logger = logging.getLogger(__name__)

#: Sets the database of an engine aside (renames its files) and returns
#: where it went; the driver's, per database.
SetAside = Callable[[], Awaitable[str]]


class ModelSchemaDrift(RuntimeError):
    """A database that takes its schema from the models holds tables that
    differ from the models; ``create_all`` never alters a table
    (SANDLOOM-B028)."""


def _declared_uniques(table: Table) -> set[frozenset[str]]:
    found = {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    found |= {frozenset([c.name]) for c in table.columns if c.unique and not c.index}
    return found


def _created_here(index: Index, connection: Any) -> bool:
    """``create_all`` emits ``index`` on this connection's database: an
    index with a ``ddl_if`` condition (``search_index`` where the renderer
    has no search index) may not be."""
    condition = getattr(index, "_ddl_if", None)
    return condition is None or bool(condition._should_execute(None, index, connection))


def _declared_indexes(table: Table, connection: Any) -> set[tuple[str, bool]]:
    return {
        (str(index.name), bool(index.unique))
        for index in table.indexes
        if isinstance(index, Index) and _created_here(index, connection)
    }


def _table_drift(inspector: Any, table: Table, connection: Any) -> list[str]:
    name = table.name
    stored = {c["name"]: c for c in inspector.get_columns(name)}
    declared = {c.name: c for c in table.columns}
    drift = [f"{name}.{c} (not in the database)" for c in declared.keys() - stored]
    drift += [f"{name}.{c} (not in the models)" for c in stored.keys() - declared]
    for column in declared.keys() & stored.keys():
        if bool(stored[column]["nullable"]) != bool(declared[column].nullable):
            want = "NULL" if declared[column].nullable else "NOT NULL"
            drift.append(f"{name}.{column} (the models say {want})")
    indexes = {(str(i["name"]), bool(i["unique"])) for i in inspector.get_indexes(name)}
    for index, unique in sorted(_declared_indexes(table, connection) - indexes):
        drift.append(
            f"{name} index {index}{' (unique)' if unique else ''} (not in the database)"
        )
    for index, unique in sorted(indexes - _declared_indexes(table, connection)):
        drift.append(
            f"{name} index {index}{' (unique)' if unique else ''} (not in the models)"
        )
    uniques = {
        frozenset(u["column_names"]) for u in inspector.get_unique_constraints(name)
    }
    for columns in sorted(_declared_uniques(table) - uniques, key=sorted):
        drift.append(
            f"{name} unique ({', '.join(sorted(columns))}) (not in the database)"
        )
    for columns in sorted(uniques - _declared_uniques(table), key=sorted):
        drift.append(
            f"{name} unique ({', '.join(sorted(columns))}) (not in the models)"
        )
    return drift


def schema_drift(connection: Any, metadata: MetaData) -> list[str]:
    """Each difference between the tables of ``metadata`` and the database,
    over the tables present on both sides: columns, their nullability,
    indexes (by name and uniqueness) and unique constraints (by columns),
    both ways. A table in another schema is reported, never skipped: this
    database has one. Sorted; empty: none. ``connection`` is a sync
    SQLAlchemy connection."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    drift: list[str] = []
    for table in metadata.sorted_tables:
        if table.schema is not None:
            drift.append(f"{table.schema}.{table.name} (a schema this database lacks)")
        elif table.name in existing:
            drift += _table_drift(inspector, table, connection)
    return sorted(drift)


async def ensure_model_schema(
    engine: AsyncEngine,
    metadata: MetaData,
    *,
    where: str,
    set_aside: SetAside | None = None,
) -> list[str]:
    """Create every missing table of ``metadata`` in one transaction of
    ``engine`` (a writable unit: on SQLite ``BEGIN IMMEDIATE``, so processes
    starting together build it one after another).

    On drift (:func:`schema_drift`) raise :class:`ModelSchemaDrift`; with
    ``set_aside`` (the explicit recreate signal), dispose the engine, set the
    database aside and build a fresh one. Returns the drift that was set
    aside (empty: none). ``where`` names the database in the messages.
    """
    async with engine.begin() as connection:
        drift: list[str] = await connection.run_sync(
            lambda sync: schema_drift(sync, metadata)
        )
        if not drift:
            await connection.run_sync(lambda sync: metadata.create_all(bind=sync))
            return []
    if set_aside is None:
        raise ModelSchemaDrift(
            f"{where} takes its schema from the models, and its tables differ "
            f"from them: {', '.join(drift)}. create_all never alters a table "
            "[SANDLOOM-B028]: move the database file away, or start the app "
            "with `sandloom dev run`, which sets it aside and builds a fresh "
            "one; its data is not migrated"
        )
    await engine.dispose()
    moved = await set_aside()
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: metadata.create_all(bind=sync))
    logger.warning(
        "%s differed from the models (%s): set aside as %s, a fresh database "
        "built [SANDLOOM-B028]",
        where,
        ", ".join(drift),
        moved,
        extra={"event": "database.model_schema_set_aside"},
    )
    return drift


async def ensure_model_schemas(
    infra: Any, metadata: MetaData, settings: Any, *, recreate: bool
) -> list[str]:
    """Ask every database instance of ``infra`` whose schema comes from the
    models to build it from ``metadata``
    (``DatabaseProvider.ensure_model_schema``); returns the instances that
    did. One whose tables differ from the models refuses (SANDLOOM-B028),
    unless ``recreate``: then its file is set aside and a fresh one built.
    No provider is built for a database the migration chains own."""
    built: list[str] = []
    for name in infra.instances("database"):
        config = infra.instance_config("database", name)
        url = database_instance_url(config.options if config else None, settings)
        if not url or not schema_from_models(dialect_of_url(url)):
            continue
        provider = infra.provider("database", name)
        ensure = getattr(provider, "ensure_model_schema", None)
        if ensure is not None and await ensure(metadata, recreate=recreate):
            built.append(name)
    return built


__all__ = [
    "ModelSchemaDrift",
    "SetAside",
    "ensure_model_schema",
    "ensure_model_schemas",
    "schema_drift",
]
