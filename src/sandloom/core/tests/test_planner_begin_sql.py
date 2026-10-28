"""The planner's read-only BEGIN statement and its per-tenant cache."""

from __future__ import annotations

import uuid

import pytest

from sandloom.core.sql_alchemy.planner_adapter import forms as pa_forms
from sandloom.core.sql_alchemy.rls import TENANT_SETTING


def _expected(value: str) -> str:
    return (
        "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY; "
        f"SELECT set_config('{TENANT_SETTING}', '{value}', true)"
    )


def test_exact_uuid_statement_is_cached_and_canonical() -> None:
    tenant = uuid.uuid4()
    first = pa_forms.begin_read_only_sql(tenant)
    assert first == _expected(str(tenant))
    assert pa_forms.begin_read_only_sql(uuid.UUID(str(tenant))) is first
    assert pa_forms.begin_read_only_sql(None) == _expected("")
    assert pa_forms.begin_read_only_sql(str(tenant).upper()) == first


def test_uuid_subclass_is_reparsed_never_trusted() -> None:
    class Sneaky(uuid.UUID):
        def __str__(self) -> str:
            return "x', true); DROP TABLE t; --"

    with pytest.raises(ValueError):
        pa_forms.begin_read_only_sql(Sneaky(int=1))


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa_forms, "_BEGIN_SQL_CACHE_SIZE", 2)
    monkeypatch.setattr(pa_forms, "_BEGIN_SQL", {})
    tenants = [uuid.uuid4() for _ in range(3)]
    for tenant in tenants:
        pa_forms.begin_read_only_sql(tenant)
    assert list(pa_forms._BEGIN_SQL) == tenants[1:]
