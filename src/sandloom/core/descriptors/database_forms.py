"""Which ``sandloom.sdk.data`` forms need a ``database`` feature, and which
SQL functions every database runs alike.

Pure data, importable without settings or a database library: ``sandloom
check`` (SANDLOOM-C030, SANDLOOM-C031), the module preflight (SANDLOOM-B033)
and the forms themselves read it. The ``database`` capability's spec defines
the features (``sandloom.core.infra.capabilities``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["DATABASE_FORM_FEATURES", "PORTABLE_SQL_FUNCTIONS"]

#: The ``sandloom.sdk.data`` forms that need a ``database`` feature, and the
#: feature each needs (design/infra-agnostic/spec.md, section 5). A module that
#: uses one declares ``CapabilityRequirement("database", features=...)``
#: (SANDLOOM-C030); a table built with one is refused at startup on an
#: instance without its feature (SANDLOOM-B033). Pure data: ``sandloom check``
#: reads it without importing a database library.
DATABASE_FORM_FEATURES: Mapping[str, str] = MappingProxyType(
    {
        "Array": "arrays",
        "fulltext_index": "fulltext_search",
        "matches": "fulltext_search",
        "json_path": "json_path",
        "json_contains": "json_contains",
        "transaction_lock": "transaction_locks",
        "aggregate_list": "list_aggregates",
    }
)

#: The SQL functions a module may call as ``func.<name>`` (SANDLOOM-C031):
#: each answers the same value, of the same Python type, on every shipped
#: renderer's database, which ``core/tests/test_portable_sql_functions.py``
#: proves by running every name on PostgreSQL and SQLite. SQLAlchemy's
#: generic function registry is no proof (it holds ``array_agg`` and
#: ``now``). Not here on purpose, each with a case in that test:
#:
#: - ``lower`` / ``upper``: SQLite without ICU folds ASCII only
#:   (``lower('ÄÖÉ')`` is ``'ÄÖÉ'``); use ``CaseInsensitiveText``;
#: - ``avg``: a ``Decimal`` on PostgreSQL, a ``float`` on SQLite; cast it
#:   (``sa.cast(func.avg(x), Float)``, a waived line);
#: - ``now`` / ``current_timestamp``: PostgreSQL's is the transaction's
#:   start, SQLite's whole seconds outside the stored time format; use
#:   ``WallClock``;
#: - ``random``: a float in [0, 1) on PostgreSQL, a 64-bit integer on SQLite;
#: - ``greatest`` / ``least``: PostgreSQL only.
#:
#: ``char_length`` is SQLAlchemy's, written ``length`` on SQLite. ``max`` and
#: ``min`` of text order by the column's collation, which may differ. A name
#: joins only with a case in that test.
PORTABLE_SQL_FUNCTIONS: frozenset[str] = frozenset(
    {
        "abs",
        "char_length",
        "coalesce",
        "count",
        "length",
        "max",
        "min",
        "nullif",
        "sum",
    }
)
