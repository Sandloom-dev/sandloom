"""The tenant guard's hot-path early exits (no work when nothing is tenant-owned).

* No TenantScoped model mapped in the process: the ``do_orm_execute`` hook
  returns before scanning mappers, generating a cache key or attaching loader
  criteria (measured: guard overhead 76-135 -> 22 Python calls per execute).
* Tenant bound, statement provably reaching no TenantScoped table: no loader
  criteria is attached (it would be a no-op), while tenant-owned statements are
  still scoped and unbound ones still fail closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import String, create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.orm.util import LoaderCriteriaOption

from sandloom.core import tenant as tenant_mod
from sandloom.core.orm import TenantScoped
from sandloom.core.tenant_context import MissingTenantError, bind_tenant


class _Base(DeclarativeBase):
    pass


class _OwnedBase(DeclarativeBase):
    pass


class _Owned(TenantScoped, _OwnedBase):
    """Tenant-owned model (its table is never created: guard-only checks)."""

    __tablename__ = "test_tgee_owned"

    id: Mapped[int] = mapped_column(primary_key=True)


class _Plain(_Base):
    __tablename__ = "test_tgee_plain"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    seen: list[Any] = []

    # Registered AFTER the guard: observes the statement the guard left.
    @event.listens_for(Session, "do_orm_execute")
    def _observe(state: Any) -> None:
        seen.append(state.statement)

    with Session(engine) as sess:
        sess.info["seen"] = seen
        yield sess
    event.remove(Session, "do_orm_execute", _observe)
    engine.dispose()


def _criteria(statement: Any) -> list[Any]:
    return [
        opt
        for opt in getattr(statement, "_with_options", ())
        if isinstance(opt, LoaderCriteriaOption)
    ]


def test_no_tenant_models_skips_all_guard_work(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tenant_mod, "_tenant_tables", lambda: frozenset())

    def _must_not_walk(_statement: Any) -> bool:
        raise AssertionError("the guard walked a statement with no tenant tables")

    monkeypatch.setattr(tenant_mod, "_references_tenant_scoped", _must_not_walk)
    session.execute(select(_Plain)).all()  # unbound
    with bind_tenant(uuid.uuid4()):
        session.execute(select(_Plain)).all()  # bound
    assert all(not _criteria(stmt) for stmt in session.info["seen"])


def test_bound_tenant_non_tenant_statement_gets_no_criteria(session: Session) -> None:
    assert tenant_mod._tenant_tables()  # _Owned is mapped
    with bind_tenant(uuid.uuid4()):
        session.execute(select(_Plain).where(_Plain.name == "x")).all()
    assert not _criteria(session.info["seen"][-1])


def test_tenant_statements_are_still_scoped_and_fail_closed(session: Session) -> None:
    with pytest.raises(MissingTenantError):
        session.execute(select(_Owned))  # unbound: refused before any SQL
    with bind_tenant(uuid.uuid4()), pytest.raises(OperationalError):
        session.execute(select(_Owned))  # (table not created on purpose)
    assert _criteria(session.info["seen"][-1])  # the guard scoped it
