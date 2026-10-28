"""The ``MoneyLedger`` model mixin (ADR-0009 item 7, spec §5.6).

A money ledger names its operation-key column once::

    class CreditLedgerEntry(MoneyLedger, Base):
        __tablename__ = "billing_credit_ledger"
        __operation_key__ = "operation_key"
        ...

The class is validated when it is defined: the column exists, is ``NOT
NULL`` and belongs to a ``UNIQUE`` constraint or unique index, so every row
the ledger accepts is deduped by construction (a retried operation conflicts
on its key instead of moving money twice). The write-admission cursor hook
(:mod:`sandloom.core.sql_alchemy.write_admission`) recognises an ``INSERT`` into a
ledger table in a request and records the money effect there (the automatic
audit row of a sensitive write, ADR-0009 item 10).
"""

from __future__ import annotations

from typing import Any, ClassVar

#: Table name -> operation-key column of every defined money ledger.
MONEY_TABLES: dict[str, str] = {}


class MoneyLedgerError(TypeError):
    """A ``MoneyLedger`` model does not name a deduping operation key."""

    sandloom_code = "SANDLOOM-R009"


def _unique_columns(table: Any) -> list[frozenset[str]]:
    sets = [
        frozenset(c.name for c in constraint.columns)
        for constraint in getattr(table, "constraints", ())
        if type(constraint).__name__ in ("UniqueConstraint", "PrimaryKeyConstraint")
    ]
    sets.extend(
        frozenset(c.name for c in index.columns)
        for index in getattr(table, "indexes", ())
        if getattr(index, "unique", False)
    )
    sets.extend(
        frozenset({column.name})
        for column in getattr(table, "columns", ())
        if getattr(column, "unique", False)
    )
    return sets


class MoneyLedger:
    """Model mixin of a money ledger: names its unique operation-key column."""

    __operation_key__: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("__abstract__"):
            return
        key = getattr(cls, "__operation_key__", None)
        name = f"{cls.__module__}.{cls.__qualname__}"
        if not isinstance(key, str) or not key:
            raise MoneyLedgerError(
                f"{name}: a MoneyLedger names its operation-key column: "
                '__operation_key__ = "operation_key" (SANDLOOM-R009)'
            )
        table = cls.__dict__.get("__table__")
        if table is None:
            raise MoneyLedgerError(
                f"{name}: MoneyLedger found no mapped table: a money ledger is a "
                "mapped model (class Ledger(MoneyLedger, Base) with __tablename__)"
            )
        column = table.columns.get(key)
        if column is None:
            raise MoneyLedgerError(
                f"{name}: __operation_key__ = {key!r} names no column of {table.name}"
            )
        if column.nullable:
            raise MoneyLedgerError(
                f"{name}: the operation key {key!r} must be NOT NULL "
                "(mapped_column(..., nullable=False))"
            )
        if not any(key in cols for cols in _unique_columns(table)):
            raise MoneyLedgerError(
                f"{name}: the operation key {key!r} must belong to a UNIQUE "
                "constraint (e.g. UniqueConstraint('customer_id', "
                f"'{key}')), so a retried operation conflicts instead of "
                "moving money twice"
            )
        MONEY_TABLES[str(table.name)] = key


__all__ = ["MONEY_TABLES", "MoneyLedger", "MoneyLedgerError"]
