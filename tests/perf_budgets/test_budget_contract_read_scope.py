"""Budget: a request making 2 local read-contract calls uses ONE connection.

Deterministic Postgres work counted with SQLAlchemy engine/pool events on the
provider's contract pool (no timing): pool checkouts (each is a pre-ping round
trip), BEGINs, statements and ROLLBACKs.

* without a read scope: every call checks out, begins, runs, rolls back
  (2 calls = 2 checkouts + 2 BEGIN + 2 statements + 2 ROLLBACK = 8 round trips);
* inside the request's read scope: 1 checkout + 1 BEGIN + 2 statements +
  1 ROLLBACK (5), and an identical third call is memoised (0).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, text

from sandloom.core.drivers.sqlalchemy_database import (
    SQLAlchemyDatabaseProvider,
    create_sqlalchemy_database,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.module_api import (
    ContractReadScopeMiddleware,
    contract_method,
    contract_session,
)
from sandloom.core.testing.workers import worker_env_values


def _database_url() -> str:
    env = worker_env_values(Path(__file__).resolve().parents[2] / ".env.test")
    url = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    return url


class Membership:
    @classmethod
    @contract_method
    async def get_role(cls, organization_id: UUID, user_id: UUID) -> str | None:
        await contract_session().execute(text("SELECT 1"))
        return "admin"

    @classmethod
    @contract_method
    async def has_role_at_least(
        cls, organization_id: UUID, user_id: UUID, role: str
    ) -> bool:
        await contract_session().execute(text("SELECT 1"))
        return True


class _Counter:
    def __init__(self) -> None:
        self.counts = {"checkout": 0, "begin": 0, "statement": 0, "rollback": 0}

    def reset(self) -> None:
        for key in self.counts:
            self.counts[key] = 0

    @property
    def round_trips(self) -> int:
        return sum(self.counts.values())

    def hook(self, name: str) -> Any:
        def _inc(*_args: Any, **_kwargs: Any) -> None:
            self.counts[name] += 1

        return _inc


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[SQLAlchemyDatabaseProvider]:
    db = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="budget_scope",
            driver="sqlalchemy",
            options={"url": _database_url()},
        )
    )
    try:
        yield db
    finally:
        await db.aclose()


@pytest.fixture
def core(provider: SQLAlchemyDatabaseProvider) -> CoreFacade:
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": provider  # type: ignore[method-assign]
    facade.register_service("tenancy.membership.v1", Membership)
    return facade


@pytest.fixture
def counter(
    core: CoreFacade, provider: SQLAlchemyDatabaseProvider
) -> Iterator[_Counter]:
    # Build the contract engine (the first contract UoW resolves it).
    core.get_contract_uow_factory("default")
    engine = provider._contract_engine
    assert engine is not None
    counter = _Counter()
    hooks = [
        (engine.sync_engine.pool, "checkout", counter.hook("checkout")),
        (engine.sync_engine, "begin", counter.hook("begin")),
        (engine.sync_engine, "before_cursor_execute", counter.hook("statement")),
        (engine.sync_engine, "rollback", counter.hook("rollback")),
    ]
    for target, name, fn in hooks:
        event.listen(target, name, fn)
    yield counter
    for target, name, fn in hooks:
        event.remove(target, name, fn)


def _app(core: CoreFacade, *, scoped: bool) -> FastAPI:
    app = FastAPI()
    org, user = uuid4(), uuid4()

    @app.get("/guarded")
    async def guarded() -> dict[str, bool]:
        svc = core.get_service("tenancy.membership.v1")
        role = await svc.get_role(org, user)
        ok = await svc.has_role_at_least(org, user, "admin")
        # A second policy atom asking the same question again.
        again = await svc.get_role(org, user)
        return {"ok": bool(role and ok and again)}

    if scoped:
        app.add_middleware(ContractReadScopeMiddleware)
    return app


async def _get(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/guarded")
    assert resp.status_code == 200 and resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_two_local_read_contract_calls_share_one_connection(
    core: CoreFacade, counter: _Counter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.config.settings import settings

    # The default release policy: the shared connection goes back to the pool
    # on the next event-loop turn after a call — back-to-back calls (no other
    # await in between, as in a policy check) deterministically share it.
    monkeypatch.setattr(settings, "contract_read_scope_idle_ms", 0)
    await _get(_app(core, scoped=False))
    baseline = dict(counter.counts)
    # Without the scope: 3 calls -> 3 checkouts / BEGINs / statements / ROLLBACKs.
    assert baseline == {"checkout": 3, "begin": 3, "statement": 3, "rollback": 3}

    counter.reset()
    await _get(_app(core, scoped=True))
    # BUDGET: one checkout, one BEGIN, the 2 distinct calls' statements, one
    # ROLLBACK at request end; the repeated call is memoised (no statement).
    assert counter.counts == {"checkout": 1, "begin": 1, "statement": 2, "rollback": 1}
    assert counter.round_trips <= 5 < sum(baseline.values())
