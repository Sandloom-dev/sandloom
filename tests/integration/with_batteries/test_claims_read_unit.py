"""Claims owners on one database share one read unit and run back to back.

``collect_claims`` used to run every non-issuer owner concurrently: on a
monolith tenancy and billing (same database) each checked out a contract-pool
connection and paid its own ``BEGIN … ROLLBACK`` — 3 connections and 12
PostgreSQL round trips per refresh (perf review 2026-10-22, item 1). Owners of
one database now run sequentially on the contract read scope's shared unit;
the issuer, other databases and remote owners still run concurrently. Every
owner keeps its own call, outcome and provenance entry.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.facade import ClaimsUnavailable, CoreFacade
from sandloom.core.module_api import contract_read_scope, contract_session
from sandloom.core.testing.fixtures import *  # noqa: F403 - db_engine
from sandloom.core.tests.test_contract_runtime import _core


def _register(
    core: CoreFacade, module: str, fn: Any, *, critical: bool = False
) -> None:
    with core.module_registration(module) as api:
        api.add_claims_contributor(fn, critical=critical)


@pytest.mark.asyncio
async def test_same_database_owners_share_one_read_unit() -> None:
    core, log = _core()
    sessions: list[Any] = []

    def make(name: str) -> Any:
        async def contributor(user_id: Any) -> dict[str, Any]:
            sessions.append(contract_session())
            return {name: True}

        return contributor

    _register(core, "sandloom_tenancy", make("t"), critical=True)
    _register(core, "sandloom_billing", make("b"))

    claims = await core.collect_claims(uuid4())

    assert claims["t"] is True and claims["b"] is True
    assert claims["claims_sources"] == ["billing", "tenancy"]
    assert claims["claims_sources_failed"] == []
    assert sessions[0] is sessions[1]  # one shared read unit
    assert log == [("default", "begin"), ("default", "rollback")]


@pytest.mark.asyncio
async def test_an_outer_read_scope_is_reused() -> None:
    core, log = _core()
    _register(core, "sandloom_tenancy", lambda user_id: _value({"t": 1}))
    _register(core, "sandloom_billing", lambda user_id: _value({"b": 2}))

    async with contract_read_scope(idle_ms=-1) as scope:
        claims = await core.collect_claims(uuid4())
        assert log == [("default", "begin")]  # still held by the request scope
        assert scope.stats["opened"] == 1 and scope.stats["reused"] == 1
    assert claims["t"] == 1 and claims["b"] == 2
    assert log == [("default", "begin"), ("default", "rollback")]


async def _value(value: dict[str, Any]) -> dict[str, Any]:
    return value


@pytest.mark.asyncio
async def test_a_failing_owner_drops_the_unit_and_keeps_its_own_outcome() -> None:
    core, log = _core()
    sessions: list[Any] = []

    async def broken(user_id: Any) -> dict[str, Any]:
        sessions.append(contract_session())
        raise RuntimeError("tenancy query failed")

    async def billing(user_id: Any) -> dict[str, Any]:
        sessions.append(contract_session())
        return {"plan": "pro"}

    _register(core, "sandloom_tenancy", broken)  # non-critical
    _register(core, "sandloom_billing", billing)

    claims = await core.collect_claims(uuid4())

    assert claims["plan"] == "pro"
    assert claims["claims_sources"] == ["billing"]
    assert claims["claims_sources_failed"] == ["tenancy"]
    assert sessions[0] is not sessions[1]  # the next owner got a fresh unit
    assert sorted(log) == sorted([("default", "begin"), ("default", "rollback")] * 2)


@pytest.mark.asyncio
async def test_critical_failure_in_a_shared_lane_still_aborts_in_order() -> None:
    core, _log = _core()
    ran: list[str] = []

    async def tenancy(user_id: Any) -> dict[str, Any]:
        ran.append("tenancy")
        raise RuntimeError("down")

    async def billing(user_id: Any) -> dict[str, Any]:
        ran.append("billing")
        raise RuntimeError("down too")

    _register(core, "sandloom_tenancy", tenancy, critical=True)
    _register(core, "sandloom_billing", billing, critical=True)
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4())
    assert exc_info.value.owner == "tenancy"
    assert ran == ["tenancy", "billing"]  # every owner still ran


class _AbortedTransaction(Exception):
    """What the driver raises once an earlier statement aborted the txn."""

    sqlstate = "25P02"


@pytest.mark.asyncio
async def test_an_aborted_shared_transaction_is_retried_on_a_fresh_unit() -> None:
    core, log = _core()
    poisoned: set[int] = set()
    calls: list[str] = []

    async def swallows(user_id: Any) -> dict[str, Any]:
        calls.append("tenancy")
        poisoned.add(id(contract_session()))  # "caught" an SQL error
        return {"t": 1}

    async def billing(user_id: Any) -> dict[str, Any]:
        calls.append("billing")
        if id(contract_session()) in poisoned:
            raise RuntimeError("wrapped") from _AbortedTransaction()
        return {"b": 2}

    _register(core, "sandloom_tenancy", swallows)
    _register(core, "sandloom_billing", billing, critical=True)

    claims = await core.collect_claims(uuid4())

    assert claims["t"] == 1 and claims["b"] == 2
    assert claims["claims_sources"] == ["billing", "tenancy"]
    assert calls == ["tenancy", "billing", "billing"]
    assert log.count(("default", "begin")) == 2


@pytest.mark.asyncio
async def test_owners_on_different_databases_still_run_concurrently() -> None:
    core, _log = _core()
    started: list[str] = []
    both = asyncio.Event()

    def make(name: str) -> Any:
        async def contributor(user_id: Any) -> dict[str, Any]:
            started.append(name)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=2)
            return {name: True}

        return contributor

    _register(core, "sandloom_tenancy", make("t"))
    _register(core, "acme.reports", make("r"))
    core.module_descriptors = [
        ModuleDescriptor(
            name="acme.reports",
            version="1",
            capabilities=[CapabilityRequirement("database", instance="reports")],
        )
    ]
    claims = await core.collect_claims(uuid4())
    assert claims["t"] is True and claims["r"] is True


@pytest.mark.asyncio
async def test_issuer_runs_concurrently_with_the_shared_lane() -> None:
    core, _log = _core()
    issuer_started = asyncio.Event()
    lane_started = asyncio.Event()

    async def identity(user_id: Any) -> dict[str, Any]:
        issuer_started.set()
        await asyncio.wait_for(lane_started.wait(), timeout=2)
        return {"roles": ["member"]}

    async def tenancy(user_id: Any) -> dict[str, Any]:
        lane_started.set()
        await asyncio.wait_for(issuer_started.wait(), timeout=2)
        return {"tenant_ids": []}

    _register(core, "sandloom_identity", identity, critical=True)
    _register(core, "sandloom_tenancy", tenancy, critical=True)
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims["roles"] == ["member"]
    assert claims["claims_sources"] == ["identity", "tenancy"]


@pytest.mark.asyncio
async def test_real_database_aborted_transaction_is_detected(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real PostgreSQL "current transaction is aborted" error is recognised."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sandloom.core import facade as facade_mod
    from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork

    core, _log = _core()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    core.get_contract_uow_factory = (  # type: ignore[method-assign]
        lambda name="default": lambda: SQLAlchemyUnitOfWork(maker)
    )

    async def swallows(user_id: Any) -> dict[str, Any]:
        try:
            await contract_session().execute(text("SELECT 1/0"))
        except Exception:  # noqa: BLE001, S110 - the bug under test
            pass
        return {"t": 1}

    async def billing(user_id: Any) -> dict[str, Any]:
        value = (await contract_session().execute(text("SELECT 2"))).scalar_one()
        return {"b": value}

    _register(core, "sandloom_tenancy", swallows)
    _register(core, "sandloom_billing", billing, critical=True)

    claims = await core.collect_claims(uuid4())
    assert claims["t"] == 1 and claims["b"] == 2
    assert claims["claims_sources"] == ["billing", "tenancy"]

    # Even without the facade's lane guard billing is not poisoned: the read
    # scope itself drops the aborted shared unit and reruns the call in its
    # own Unit of Work (ContractReadScope.run, SQLSTATE 25P02).
    monkeypatch.setattr(facade_mod._claims, "_in_failed_transaction", lambda exc: False)
    claims = await core.collect_claims(uuid4())
    assert claims["t"] == 1 and claims["b"] == 2
    assert claims["claims_sources"] == ["billing", "tenancy"]


@pytest.mark.asyncio
async def test_precomputed_issuer_contributor_is_not_called_but_stamped() -> None:
    core, _log = _core()
    calls: list[str] = []

    async def roles(user_id: Any) -> dict[str, Any]:
        calls.append("roles")
        return {"roles": ["from-query"]}

    async def extra(user_id: Any) -> dict[str, Any]:
        calls.append("extra")
        return {"extra": True}

    async def tenancy(user_id: Any) -> dict[str, Any]:
        calls.append("tenancy")
        return {"tenant_ids": []}

    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(roles, critical=True)
        api.add_claims_contributor(extra)
    _register(core, "sandloom_tenancy", tenancy, critical=True)

    claims = await core.collect_claims(
        uuid4(), object(), issuer="identity", precomputed={roles: {"roles": ["cas"]}}
    )

    assert claims["roles"] == ["cas"] and claims["extra"] is True
    assert sorted(calls) == ["extra", "tenancy"]  # only the precomputed one skipped
    assert claims["claims_sources"] == ["identity", "tenancy"]


@pytest.mark.asyncio
async def test_precomputed_never_stands_in_for_another_owner() -> None:
    core, _log = _core()

    async def tenancy(user_id: Any) -> dict[str, Any]:
        return {"tenant_ids": ["real"]}

    _register(core, "sandloom_tenancy", tenancy, critical=True)
    # No issuer session: ignored; and tenancy is not the issuer anyway.
    claims = await core.collect_claims(
        uuid4(), precomputed={tenancy: {"tenant_ids": ["forged"]}}
    )
    assert claims["tenant_ids"] == ["real"]
    claims = await core.collect_claims(
        uuid4(),
        object(),
        issuer="identity",
        precomputed={tenancy: {"tenant_ids": ["forged"]}},
    )
    assert claims["tenant_ids"] == ["real"]
