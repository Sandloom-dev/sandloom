"""The tenant guard's statement walk is memoized (hot-path performance).

With NO tenant bound, every ORM execute must prove the statement reaches no
TenantScoped table. That walk is non-trivial (~150us for a 3-table select with
subqueries on a dev laptop), so the verdict is memoized per structural cache key
and the TenantScoped table set is memoized until a new TenantScoped model is
declared. These tests pin that identical statements are NOT re-walked, while
correctness (fail-closed detection) is preserved.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import Column, ForeignKey, MetaData, String, Table, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core import tenant as tenant_mod
from sandloom.core.orm import TenantScoped


class _Base(DeclarativeBase):
    pass


class _Account(_Base):
    __tablename__ = "test_tgc_accounts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(String(100))


class _Grant(_Base):
    __tablename__ = "test_tgc_grants"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("test_tgc_accounts.id"))
    name: Mapped[str] = mapped_column(String(100))


class _Note(TenantScoped, _Base):
    __tablename__ = "test_tgc_notes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True))


def _plain_stmt(email: str) -> Any:
    sub = select(_Grant.account_id).where(_Grant.name == "admin").scalar_subquery()
    return (
        select(_Account)
        .join(_Grant, _Grant.account_id == _Account.id)
        .where(_Account.email == email, _Account.id == sub)
        .limit(10)
    )


def _tenant_in_subquery(email: str) -> Any:
    return select(_Account).where(
        _Account.email == email,
        _Account.id.in_(select(_Note.account_id)),
    )


def _walks() -> int:
    return tenant_mod.guard_cache_stats["walks"]


def _tables_cached() -> bool:
    return tenant_mod._tenant_tables_cache is not None


def _verdicts_cached() -> bool:
    return bool(tenant_mod._reference_cache)


def test_identical_statements_are_walked_once() -> None:
    tenant_mod._invalidate_guard_caches()
    before = _walks()
    # Freshly built statements, different bind VALUES, same structure.
    for i in range(200):
        assert tenant_mod._references_tenant_scoped(_plain_stmt(f"u{i}")) is False
    assert _walks() - before == 1

    before = _walks()
    for i in range(200):
        assert tenant_mod._references_tenant_scoped(_tenant_in_subquery(f"u{i}"))
    assert _walks() - before == 1


def test_structurally_different_statements_get_their_own_verdict() -> None:
    tenant_mod._invalidate_guard_caches()
    # Cache must never smear a "safe" verdict onto a statement that does reach a
    # TenantScoped table (fail-closed correctness is preserved).
    assert tenant_mod._references_tenant_scoped(_plain_stmt("a")) is False
    assert tenant_mod._references_tenant_scoped(_tenant_in_subquery("a")) is True
    assert tenant_mod._references_tenant_scoped(_plain_stmt("b")) is False


def test_same_named_table_in_other_metadata_is_not_confused() -> None:
    tenant_mod._invalidate_guard_caches()
    lookalike = Table(
        "test_tgc_notes",
        MetaData(),
        Column("id", PG_UUID(as_uuid=True), primary_key=True),
    )
    assert tenant_mod._references_tenant_scoped(select(lookalike)) is False
    assert tenant_mod._references_tenant_scoped(select(_Note)) is True


def test_new_tenant_model_invalidates_caches() -> None:
    tenant_mod._invalidate_guard_caches()
    tenant_mod._tenant_tables()
    assert _tables_cached()
    tenant_mod._references_tenant_scoped(_plain_stmt("x"))
    assert _verdicts_cached()

    class _Late(TenantScoped, _Base):
        __tablename__ = "test_tgc_late"

        id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)

    assert not _tables_cached()
    assert not _verdicts_cached()
    assert _Late.__table__ in tenant_mod._tenant_tables()
    assert tenant_mod._references_tenant_scoped(select(_Late)) is True


def test_uncacheable_statement_still_walked_correctly() -> None:
    # text() has a cache key; a statement whose key cannot be produced falls
    # back to walking every time (never fails open).
    assert tenant_mod._references_tenant_scoped(text("select 1")) is False

    class _NoKey:
        def _generate_cache_key(self) -> None:
            return None

        def get_children(self, **_kw: Any) -> list[Any]:
            return []

    before = _walks()
    tenant_mod._references_tenant_scoped(_NoKey())
    tenant_mod._references_tenant_scoped(_NoKey())
    assert _walks() - before == 2


def test_cached_guard_is_cheaper_than_walking() -> None:
    """Micro-benchmark: memoized verdict vs a full walk (generous bound)."""
    tenant_mod._invalidate_guard_caches()
    statements = [_plain_stmt(f"u{i}") for i in range(300)]
    walk_statements = [_plain_stmt(f"w{i}") for i in range(300)]
    # Pre-compute cache keys for both sets so we compare ONLY the guard work
    # (the compiled-SQL cache computes the memoized key anyway).
    for stmt in (*statements, *walk_statements):
        stmt._generate_cache_key()

    t0 = time.perf_counter()
    for stmt in walk_statements:
        tenant_mod._walk_references_tenant_scoped(stmt)
    walk_cost = time.perf_counter() - t0

    t0 = time.perf_counter()
    for stmt in statements:
        tenant_mod._references_tenant_scoped(stmt)
    cached_cost = time.perf_counter() - t0

    assert cached_cost < walk_cost / 2, (cached_cost, walk_cost)
