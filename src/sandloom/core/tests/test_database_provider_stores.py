"""DatabaseProvider outbox/audit store exposure."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import AuditStore, DatabaseProvider, OutboxStore
from sandloom.core.tasks.relay import _database_targets
from sandloom.core.unit_of_work.scope import _stash_stores


class _FakeDb(DatabaseProvider):
    def uow_factory(self) -> Callable[[], UnitOfWorkContract]:
        raise NotImplementedError

    def features(self) -> frozenset[str]:
        return frozenset({"transactions"})


def _unused_uow() -> UnitOfWorkContract:
    raise AssertionError("the UoW factory must not be called while resolving targets")


class _NoOutboxDb(_FakeDb):
    def uow_factory(self) -> Callable[[], UnitOfWorkContract]:
        return _unused_uow


class _BrokenOutboxDb(_NoOutboxDb):
    def outbox(self) -> OutboxStore | None:
        raise RuntimeError("plugin outbox failed")


class _Registry:
    def __init__(self, provider: DatabaseProvider) -> None:
        self.provider = provider

    def instances(self, capability: str) -> list[str]:
        assert capability == "database"
        return ["default"]

    def resolve(
        self,
        capability: str,
        name: str = "default",
        *,
        contract: type[Any] | None = None,
    ) -> DatabaseProvider:
        assert capability == "database"
        assert name == "default"
        return self.provider


class _BrokenRegistry(_Registry):
    def resolve(
        self,
        capability: str,
        name: str = "default",
        *,
        contract: type[Any] | None = None,
    ) -> DatabaseProvider:
        raise RuntimeError("database resolution failed")


def _core_with_registry(registry: _Registry) -> CoreFacade:
    core = CoreFacade(FastAPI())
    core.set_infra(cast(Any, registry))
    return core


def test_sqlalchemy_provider_exposes_stores() -> None:
    from sandloom.core.drivers.sqlalchemy_database import create_sqlalchemy_database

    provider = create_sqlalchemy_database(
        InstanceConfig(
            capability="database", name="default", driver="sqlalchemy", options={}
        )
    )
    assert isinstance(provider.outbox(), OutboxStore)
    assert isinstance(provider.audit(), AuditStore)
    feats = provider.features()
    assert "transactional_outbox" in feats
    assert "audit" in feats


def test_default_database_has_no_outbox_unless_supported() -> None:
    # A backend that does not override outbox()/audit() returns None — the
    # contract default. publish_event refuses such a backend for transactional
    # events.
    db = _FakeDb(name="x", driver="fake", options={})
    assert db.outbox() is None
    assert db.audit() is None


def test_registry_database_without_outbox_has_no_relay_target() -> None:
    provider = _NoOutboxDb(name="default", driver="custom", options={})
    core = _core_with_registry(_Registry(provider))

    assert _database_targets(core) == []


def test_relay_surfaces_outbox_provider_failure() -> None:
    provider = _BrokenOutboxDb(name="default", driver="broken", options={})
    core = _core_with_registry(_Registry(provider))

    with pytest.raises(RuntimeError, match="plugin outbox failed"):
        _database_targets(core)


def test_stash_stores_surfaces_registry_resolution_failure() -> None:
    provider = _NoOutboxDb(name="default", driver="broken", options={})
    core = _core_with_registry(_BrokenRegistry(provider))
    session = cast(Any, SimpleNamespace(info={}))

    with pytest.raises(RuntimeError, match="database resolution failed"):
        _stash_stores(session, core, "default")


def test_stash_stores_rejects_bare_facade_mode() -> None:
    core = CoreFacade(FastAPI())
    session = cast(Any, SimpleNamespace(info={}))

    with pytest.raises(RuntimeError, match="Infrastructure registry not configured"):
        _stash_stores(session, core, "default")


def test_relay_rejects_bare_facade_mode() -> None:
    core = CoreFacade(FastAPI())

    with pytest.raises(RuntimeError, match="Infrastructure registry not configured"):
        _database_targets(core)
