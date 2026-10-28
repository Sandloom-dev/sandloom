"""The relational driver contract the kernel's unit machinery relies on
(infra-agnostic boundary): the lazy request unit needs
``LazyUnitOfWorkContract``, the commit hooks ask the driver for unflushed
changes through ``DatabaseProvider.has_pending_changes``, and the SQLAlchemy
driver implements and advertises both (``write_admission``)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.contracts.uow import LazyUnitOfWorkContract
from sandloom.core.drivers.sqlalchemy_database import SQLAlchemyDatabaseProvider
from sandloom.core.infra.contracts import DatabaseProvider
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.unit_of_work import LazyUnit
from sandloom.core.unit_of_work.hooks import _pending_orm_changes
from sandloom.core.unit_of_work.scope import stash_pending_check


class _EagerOnlyUoW:
    session: Any = None

    async def __aenter__(self) -> _EagerOnlyUoW:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


def test_the_lazy_unit_needs_the_lazy_uow_contract() -> None:
    assert isinstance(
        SQLAlchemyUnitOfWork(session_factory=None), LazyUnitOfWorkContract
    )  # type: ignore[arg-type]
    assert not isinstance(_EagerOnlyUoW(), LazyUnitOfWorkContract)
    core = SimpleNamespace(get_uow_factory=lambda _name: _EagerOnlyUoW)
    with pytest.raises(RuntimeError, match="LazyUnitOfWorkContract"):
        LazyUnit(core, "default").bind_lazily()


def test_the_commit_hooks_ask_the_driver_for_pending_changes() -> None:
    class _Provider:
        def has_pending_changes(self, session: Any) -> bool:
            return bool(session.info.get("dirty"))

    session = SimpleNamespace(info={"dirty": True})
    assert _pending_orm_changes(session) is False  # no driver answer: none
    stash_pending_check(session, _Provider())
    assert _pending_orm_changes(session) is True
    session.info["dirty"] = False
    assert _pending_orm_changes(session) is False


def test_the_sqlalchemy_driver_reports_unflushed_orm_changes() -> None:
    assert DatabaseProvider.has_pending_changes is not (
        SQLAlchemyDatabaseProvider.has_pending_changes
    )
    clean = SimpleNamespace(sync_session=SimpleNamespace(_is_clean=lambda: True))
    dirty = SimpleNamespace(sync_session=SimpleNamespace(_is_clean=lambda: False))
    provider = object.__new__(SQLAlchemyDatabaseProvider)
    assert provider.has_pending_changes(clean) is False
    assert provider.has_pending_changes(dirty) is True
