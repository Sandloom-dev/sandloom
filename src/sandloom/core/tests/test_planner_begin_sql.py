"""The planner's read-only BEGIN statement and its per-tenant cache.

The text comes from the renderer's row security (slice 6); on PostgreSQL it
is byte-identical to the literal the planner wrote before.
"""

from __future__ import annotations

import uuid

import pytest

from sandloom.core.drivers.sql_dialects import POSTGRESQL
from sandloom.core.sql_alchemy.planner_adapter import forms as pa_forms


def _expected(value: str) -> str:
    # The literal of the planner before slice 6, byte for byte.
    return (
        "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY; "
        f"SELECT set_config('sandloom.tenant_id', '{value}', true)"
    )


def _begin() -> pa_forms.ReadOnlyBegin:
    return pa_forms.ReadOnlyBegin(POSTGRESQL.row_security())


def test_exact_uuid_statement_is_cached_and_canonical() -> None:
    begin = _begin()
    tenant = uuid.uuid4()
    first = begin.sql(tenant)
    assert first == _expected(str(tenant))
    assert begin.sql(uuid.UUID(str(tenant))) is first
    assert begin.sql(None) == _expected("")
    assert begin.sql(str(tenant).upper()) == first  # type: ignore[arg-type]


def test_the_rebind_statement_is_the_old_literal() -> None:
    assert _begin().rebind_sql == "SELECT set_config('sandloom.tenant_id', $1, true)"


def test_without_row_security_only_the_begin_is_sent() -> None:
    begin = pa_forms.ReadOnlyBegin(None)
    assert begin.sql(uuid.uuid4()) == "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY"
    assert begin.sql(None) == "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY"
    assert begin.rebind_sql is None


def test_uuid_subclass_is_reparsed_never_trusted() -> None:
    class Sneaky(uuid.UUID):
        def __str__(self) -> str:
            return "x', true); DROP TABLE t; --"

    with pytest.raises(ValueError):
        _begin().sql(Sneaky(int=1))


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa_forms, "_BEGIN_SQL_CACHE_SIZE", 2)
    begin = _begin()
    tenants = [uuid.uuid4() for _ in range(3)]
    for tenant in tenants:
        begin.sql(tenant)
    assert list(begin._cache) == tenants[1:]
