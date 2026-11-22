"""The saga store's per-step statements are built once and bound per call.

A statement built per call (``.values(**row)``: one bind per column, then the
cache-key walk over all of them) cost more Python than the round trip it
sent. The SQL is unchanged; what these tests pin is that a second call
builds nothing, and that the bound tenant, id and values still reach the
database (a statement cached with one call's values would answer the next
call with them).
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import pytest

import sandloom.core.sql_alchemy.portable as _portable
import sandloom.core.sql_alchemy.saga_store as store
from sandloom.core.descriptors.sagas import operation_key
from sandloom.core.drivers.sql_dialects import PostgresqlSql
from sandloom.core.sql_alchemy.portable import for_update_for, sql_dialect
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.tests.saga_fixtures import (
    Ledger,
    enroll_input,
    enroll_saga,
    install_fakes,
)
from sandloom.core.unit_of_work import db_session

ENROLL = enroll_saga()


@pytest.fixture
def renders(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """One entry per ``FOR UPDATE`` statement the store builds."""
    built: list[bool] = []
    monkeypatch.setattr(store, "_PREBUILT", {})  # nothing built by an earlier test

    def counting(*args: Any, **kwargs: Any) -> Any:
        built.append(kwargs["skip_locked"])
        return for_update_for(*args, **kwargs)

    monkeypatch.setattr(store, "for_update_for", counting)
    return built


def test_operation_key_refuses_exactly_colon_and_whitespace() -> None:
    """The per-part check is one regex search; it must refuse the characters
    the per-character ``str.isspace`` check refused, and no other."""
    for code in range(sys.maxunicode + 1):
        char = chr(code)
        if 0xD800 <= code <= 0xDFFF:  # lone surrogates: not text
            continue
        refused = char == ":" or char.isspace()
        if refused:
            with pytest.raises(ValueError):
                operation_key("a", f"x{char}y")
        else:
            assert operation_key("a", f"x{char}y") == f"a:x{char}y"


async def test_lock_statement_is_built_once_per_shape(
    saga_harness, renders: list[bool]
) -> None:
    async with db_session() as session:
        for _ in range(3):
            for tenant in (store.ANY_TENANT, None, uuid.uuid4()):
                assert (
                    await store.lock_instance(session, uuid.uuid4(), tenant=tenant)
                    is None
                )
    # three tenant shapes, one statement each; the third call of a shape and
    # a different tenant id reuse it
    assert renders == [True, True, True]


async def test_lock_statement_keeps_skip_locked_in_its_shape(
    saga_harness, renders: list[bool]
) -> None:
    async with db_session() as session:
        for skip_locked in (True, False, True, False):
            await store.lock_instance(
                session, uuid.uuid4(), tenant=None, skip_locked=skip_locked
            )
    assert renders == [True, False]


async def test_a_renderer_registered_again_is_asked_again(
    saga_harness, renders: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache key holds the renderer: one that replaces the registered
    renderer (a driver loaded later, a test's renderer without ``skip_locked``)
    builds its own statement instead of being served the earlier one."""
    async with db_session() as session:
        await store.lock_instance(session, uuid.uuid4(), tenant=None)
        assert renders == [True]
        # A new renderer object for the dialect this run is on (PostgreSQL
        # or SQLite), so the test holds on every test database.
        name = session.get_bind().dialect.name
        registered = sql_dialect(name)
        assert registered is not None
        monkeypatch.setitem(_portable._registered, name, type(registered)())
        await store.lock_instance(session, uuid.uuid4(), tenant=None)
    assert renders == [True, True]


class _UnhashableRenderer(PostgresqlSql):
    """A renderer that defines ``__eq__`` without ``__hash__``."""

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = None  # type: ignore[assignment]


async def test_an_unhashable_renderer_still_builds_and_caches(
    saga_harness, renders: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    sql_dialect("postgresql")  # the entry points are read first
    monkeypatch.setitem(_portable._registered, "postgresql", _UnhashableRenderer())
    async with db_session() as session:
        for _ in range(2):
            await store.lock_instance(session, uuid.uuid4(), tenant=None)
    assert renders == [True]  # built once, served the second time


async def test_cached_lock_statement_binds_each_calls_tenant(saga_harness) -> None:
    install_fakes(saga_harness, Ledger())
    handle = await saga_harness.start(ENROLL, saga_id="pre-1", input=enroll_input())
    other = uuid.uuid4()
    async with db_session() as session:
        for _ in range(2):  # the second round runs the cached statements
            assert (
                await store.lock_instance(
                    session, handle.instance_id, tenant=store.ANY_TENANT
                )
            ) is not None
            assert (
                await store.lock_instance(session, handle.instance_id, tenant=None)
            ) is not None  # the harness starts a tenant-less instance
            assert (
                await store.lock_instance(session, handle.instance_id, tenant=other)
            ) is None
            assert (
                await store.lock_instance(
                    session, uuid.uuid4(), tenant=store.ANY_TENANT
                )
            ) is None


async def test_cached_writes_bind_each_calls_values(saga_harness) -> None:
    """``update_instance`` and ``upsert_step`` reuse one statement: the second
    call's values, not the first's, are what the row holds."""
    install_fakes(saga_harness, Ledger())
    first = await saga_harness.start(ENROLL, saga_id="pre-2", input=enroll_input())
    second = await saga_harness.start(ENROLL, saga_id="pre-3", input=enroll_input())
    async with db_session() as session:
        for handle, version in ((first, 7), (second, 9), (first, 11)):
            row = await store.lock_instance(
                session, handle.instance_id, tenant=store.ANY_TENANT
            )
            assert row is not None
            row.version = version
            await store.update_instance(session, row)
            await store.upsert_step(
                session,
                {
                    "instance_id": row.id,
                    "step": "probe",
                    "direction": "do",
                    "saga_type": row.saga_type,
                    "tenant_id": row.tenant_id,
                    "status": "succeeded",
                    "outcome": "success",
                    "attempts": version,
                    "operation_key": None,
                    "first_attempt_at": row.created_at,
                    "last_attempt_at": row.created_at,
                    "last_duration_ms": 1.0,
                    "last_error": None,
                },
            )
        versions = {}
        for handle in (first, second):
            row = await store.lock_instance(
                session, handle.instance_id, tenant=store.ANY_TENANT
            )
            assert row is not None
            versions[handle.saga_id] = row.version
        attempts = {}
        for handle in (first, second):
            (probe,) = [
                s
                for s in await store.steps_of(session, handle.instance_id)
                if s["step"] == "probe"
            ]
            attempts[handle.saga_id] = probe["attempts"]
    assert versions == {"pre-2": 11, "pre-3": 9}
    assert attempts == {"pre-2": 11, "pre-3": 9}
