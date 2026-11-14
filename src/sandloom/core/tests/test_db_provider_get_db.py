"""``get_db`` takes its session from the CONFIGURED ``default`` instance of the
active core, and ``isolation_identity`` names the store a URL points at."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from sandloom.core._internal.db_provider import get_db
from sandloom.core.config.settings import settings
from sandloom.core.drivers.sqlalchemy_database import (
    SQLAlchemyDatabaseProvider,
    _isolation_material,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.ports.database import DatabaseProvider
from sandloom.core.runtime import use_active_core


def _provider(url: str) -> SQLAlchemyDatabaseProvider:
    return SQLAlchemyDatabaseProvider.from_config(
        InstanceConfig("database", "default", "sqlalchemy", {"url": url})
    )


def test_session_factory_is_declared_on_the_contract_and_defaults_to_none() -> None:
    assert isinstance(DatabaseProvider.session_factory, property)
    assert (
        DatabaseProvider.planner_adapter
        is not SQLAlchemyDatabaseProvider.planner_adapter
    )


async def test_get_db_uses_the_session_factory_of_the_configured_default(
    tmp_path: Path,
) -> None:
    """The infrastructure may override the default's URL: ``get_db`` follows
    it, not the process-wide sessionmaker built from ``DATABASE_URL``."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'overridden.db'}"
    provider = _provider(url)
    core: Any = SimpleNamespace(database=lambda name="default": provider)
    assert make_url(url) != make_url(settings.database_url)
    try:
        with use_active_core(core):
            gen = get_db()
            session = await gen.__anext__()
            try:
                assert session.bind is provider.engine
                assert str(session.bind.url) == str(provider.engine.url)
                assert (await session.execute(text("SELECT 1"))).scalar() == 1
            finally:
                await gen.aclose()
    finally:
        await provider.aclose()
    assert (tmp_path / "overridden.db").exists()


async def test_get_db_of_a_driver_without_raw_sessions_says_what_to_do() -> None:
    core: Any = SimpleNamespace(
        database=lambda name="default": SimpleNamespace(session_factory=None)
    )
    with use_active_core(core):
        gen = get_db()
        with pytest.raises(RuntimeError, match="no raw session factory"):
            await gen.__anext__()


async def test_isolation_identity_has_no_credentials_and_tells_stores_apart(
    tmp_path: Path,
) -> None:
    def pg(query: str = "", user: str = "u:secret") -> SQLAlchemyDatabaseProvider:
        return _provider(f"postgresql+asyncpg://{user}@db.example:5432/app{query}")

    a, same_other_credentials = pg(), pg(user="x:other")
    socket_a = pg("?host=/run/pg-a")
    socket_b = pg("?host=/run/pg-b")
    ids = [
        await p.isolation_identity()
        for p in (a, same_other_credentials, socket_a, socket_b)
    ]
    assert ids[0] == ids[1]  # credentials are not part of the identity
    assert "secret" not in str(ids[0])
    assert len({ids[0], ids[2], ids[3]}) == 3  # unix socket directories differ

    one = _provider(f"sqlite+aiosqlite:///{tmp_path / 'one.db'}")
    two = _provider(f"sqlite+aiosqlite:///{tmp_path / 'two.db'}")
    same = _provider(f"sqlite+aiosqlite:///{tmp_path / 'sub' / '..' / 'one.db'}")
    assert await one.isolation_identity() != await two.isolation_identity()
    assert await one.isolation_identity() == await same.isolation_identity()

    for p in (a, same_other_credentials, socket_a, socket_b, one, two, same):
        await p.aclose()


def test_isolation_material_names_the_sqlite_file_absolutely() -> None:
    url = make_url("sqlite+aiosqlite:///rel.db")
    assert str(Path("rel.db").absolute()) in _isolation_material(url, 1)


def test_an_in_memory_sqlite_url_gets_a_marker_of_its_instance() -> None:
    url = make_url("sqlite+aiosqlite:///:memory:")
    assert _isolation_material(url, 1) != _isolation_material(url, 2)
    assert _isolation_material(url, 1) == _isolation_material(url, 1)
