"""Test seam over the infrastructure registry.

:func:`override_instance` points one named infrastructure instance (the
``default`` cache, the ``default`` database, ...) at a test backend for the
duration of a block, restoring the previous configuration AND the previously
built provider afterwards (``InfraRegistry.snapshot_instance`` /
``restore_instance``).

:func:`skip_unless_db_feature` skips a test whose database instance lacks a
feature it needs (``arrays`` on SQLite, say): the instance's features as the
application advertises them, build-free (``InfraRegistry.declared_features``).

:func:`has_optimized_reads` (public, ``sandloom.sdk.testing``) answers whether
the planner's optimized executor can run on the test database (the DBAPI
adapter's ``raw_executor``), so a module test never reads ``dialect.name``
(SANDLOOM-C031). :func:`has_serial_writers` (the renderer's ``serial_writers``)
is the kernel's own tests' question, whose branches differ by nesting; a
module's race test skips without overlapping writers instead,
``skip_unless_db_feature("concurrent_writers")``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from sandloom.core.sql_alchemy.dbapi import RAW_EXECUTOR, dbapi_adapter_of
from sandloom.core.sql_alchemy.portable import dialect_name, serial_writers

#: The marker :func:`skip_unless_db_feature` puts on a test.
DB_FEATURE_MARKER = "sandloom_db_feature"
#: Its ``markers`` ini line: the plugin registers it; a conftest that star
#: imports the fixtures adds it in its ``pytest_configure``
#: (``config.addinivalue_line("markers", DB_FEATURE_MARKER_LINE)``).
DB_FEATURE_MARKER_LINE = (
    f"{DB_FEATURE_MARKER}(*features, database='default'): skip unless the "
    "database instance has the features (skip_unless_db_feature)"
)


@asynccontextmanager
async def override_instance(
    core: Any,
    capability: str,
    name: str = "default",
    *,
    driver: str,
    **options: Any,
) -> AsyncIterator[Any]:
    """Configure ``capability``/``name`` with ``driver`` + ``options`` in the block.

    Yields the core's infrastructure registry. On exit the provider built for
    the override (if any) is closed and the previous instance configuration and
    provider are put back — so a lazily built, process-wide provider (e.g. the
    ``default`` engine) survives the test untouched::

        async with override_instance(core, "cache", driver="redis",
                                     url=os.environ["CACHE_URL"]):
            ...
    """
    from sandloom.core.infra.config import InstanceConfig  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    registry = core.infra
    snapshot = registry.snapshot_instance(capability, name)
    registry.configure_instance(
        InstanceConfig(capability=capability, name=name, driver=driver, options=options)
    )
    try:
        yield registry
    finally:
        displaced = registry.restore_instance(snapshot)
        if displaced is not None:
            await displaced.aclose()


def skip_unless_db_feature(
    *features: str, database: str = "default"
) -> pytest.MarkDecorator:
    """Skip the test unless database instance ``database`` has every one of
    ``features`` (``arrays``, ``fulltext_search``, ``json_contains``,
    ``skip_locked``, ``concurrent_writers`` for a race of overlapping writers,
    ...): the features the application advertises for it,
    the same answer its module preflight reads. On a function, a class or as
    ``pytestmark``::

        @skip_unless_db_feature("arrays")
        async def test_tags_are_an_array(client): ...

    The skip names the missing features and the instance.
    """
    if not features:
        raise TypeError("skip_unless_db_feature() needs at least one feature")
    mark: pytest.MarkDecorator = getattr(pytest.mark, DB_FEATURE_MARKER)
    return mark(*features, database=database)


def missing_db_features(
    core: Any, features: Iterable[str], database: str = "default"
) -> list[str]:
    """The ``features`` database instance ``database`` of ``core`` lacks
    (every one when the instance is not configured)."""
    have = core.infra.declared_features("database", database) or frozenset()
    return [feature for feature in features if feature not in have]


def db_feature_skip_reason(core: Any, item: Any) -> str | None:
    """Why ``item`` is skipped by its :func:`skip_unless_db_feature`
    markers, or ``None``."""
    for marker in item.iter_markers(DB_FEATURE_MARKER):
        database = str(marker.kwargs.get("database", "default"))
        missing = missing_db_features(core, marker.args, database)
        if missing:
            return (
                f"database/{database} lacks the feature(s) "
                f"{', '.join(sorted(missing))} (skip_unless_db_feature)"
            )
    return None


def has_serial_writers(bind: Any) -> bool:
    """Whether the database behind ``bind`` (a session, connection or engine)
    has one writer at a time, so racing write transactions run in turn
    rather than overlapping (its renderer's ``serial_writers``)::

        await barrier.wait(serial=has_serial_writers(db))
    """
    return serial_writers(dialect_name(bind))


def has_optimized_reads(bind: Any) -> bool:
    """Whether the planner's optimized executor can run on the database
    behind ``bind`` (an engine, sync or async): its DBAPI adapter runs raw
    statements on the driver connection. Without it every declared read runs
    on the general executor, whatever ``planner_optimized_executor`` says."""
    engine = getattr(bind, "sync_engine", bind)
    return RAW_EXECUTOR in dbapi_adapter_of(engine).capabilities


__all__ = [
    "DB_FEATURE_MARKER",
    "DB_FEATURE_MARKER_LINE",
    "db_feature_skip_reason",
    "has_optimized_reads",
    "has_serial_writers",
    "missing_db_features",
    "override_instance",
    "skip_unless_db_feature",
]
