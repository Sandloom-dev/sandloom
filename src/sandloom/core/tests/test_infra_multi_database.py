"""Proof of pillar 3: multiple databases simultaneously + total agnosticism.

Two things are proven here:

1. Two database instances configured purely via ``INFRA`` resolve to two
   INDEPENDENT engines/pools and each performs a real round-trip — i.e. the
   framework runs "different databases simultaneously", selected by name.
2. A completely custom database driver (no SQLAlchemy at all) can back a named
   instance, proving the kernel depends only on the abstract UoW contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from sqlalchemy import text

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.infra.contracts import DatabaseProvider
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _two_database_config(url: str) -> InfraConfig:
    return {
        "database": {
            "default": InstanceConfig(
                capability="database",
                name="default",
                driver="sqlalchemy",
                options={"url": url},
            ),
            "analytics": InstanceConfig(
                capability="database",
                name="analytics",
                driver="sqlalchemy",
                # Same physical DB is just the test substrate; the point is two
                # independent engines selected by logical name.
                options={"url": url},
            ),
        }
    }


@pytest.mark.asyncio
async def test_two_databases_are_independent_and_both_work() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    registry = build_infra_registry(
        InMemorySchemaRegistry(),
        config=_two_database_config(url),
        load_plugins=False,
    )
    default_db = registry.provider("database", "default")
    analytics_db = registry.provider("database", "analytics")
    assert isinstance(default_db, DatabaseProvider)
    assert isinstance(analytics_db, DatabaseProvider)

    # Independent engines/pools — this is "different databases simultaneously".
    assert default_db.engine is not analytics_db.engine  # type: ignore[attr-defined]

    for db in (default_db, analytics_db):
        factory = db.uow_factory()
        async with factory() as uow:
            assert uow.session is not None
            result = await uow.session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        health = await db.health()
        assert health.status == "ok"

    # The analytics engine is registry-owned and disposed on aclose.
    await registry.aclose_all()


# --------------------------------------------------------------------------- #
# A fully custom database driver: no SQLAlchemy, just the UoW contract.
# --------------------------------------------------------------------------- #


class _DictSession:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.info: dict[str, Any] = {}

    def put(self, key: str, value: Any) -> None:
        self.store[key] = value


class _DictUnitOfWork(UnitOfWorkContract):
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self.session: Any = None
        self._staged: dict[str, Any] = {}

    async def __aenter__(self) -> _DictUnitOfWork:
        self.session = _DictSession(self._staged)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self) -> None:
        self._store.update(self._staged)

    async def rollback(self) -> None:
        self._staged.clear()


class _DictDatabaseProvider(DatabaseProvider):
    def __init__(self, *, name: str, driver: str, options: dict[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self.store: dict[str, Any] = {}

    def uow_factory(self) -> Any:
        def _factory() -> _DictUnitOfWork:
            return _DictUnitOfWork(self.store)

        return _factory


@pytest.mark.asyncio
async def test_custom_non_sqlalchemy_database_driver() -> None:
    """A dict-backed database works through the same contract as Postgres."""
    # Construct the registry directly and register the custom driver before
    # resolving — exactly how a third-party driver plugin would wire itself.
    registry = InfraRegistry(
        config={
            "database": {
                "memory": InstanceConfig(
                    capability="database",
                    name="memory",
                    driver="dict",
                    options={},
                )
            }
        }
    )
    registry.register_driver(
        "database",
        "dict",
        lambda cfg: _DictDatabaseProvider(
            name=cfg.name, driver=cfg.driver, options=dict(cfg.options)
        ),
    )
    registry.validate()  # custom driver is registered -> passes

    db = registry.provider("database", "memory")
    factory = db.uow_factory()

    # Commit on success
    async with factory() as uow:
        uow.session.put("k", "v")
    assert db.store == {"k": "v"}  # type: ignore[attr-defined]

    # Rollback on error — nothing persisted
    with pytest.raises(ValueError):
        async with factory() as uow:
            uow.session.put("dropped", 1)
            raise ValueError("boom")
    assert "dropped" not in db.store  # type: ignore[attr-defined]
