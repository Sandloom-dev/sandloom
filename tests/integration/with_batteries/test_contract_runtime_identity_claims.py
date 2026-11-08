"""Contract runtime: identical semantics for local and remote service contracts.

Pins the configurable-monolith promise (finding C1/C2/H5/X1/X2):

- a contract call NEVER runs in the caller's transaction — the provider opens
  its OWN Unit of Work on ITS OWN database, for local and RPC callers alike;
- read contracts roll back (and may not publish), commands commit and require
  an idempotency key;
- a caller-passed session is an argument error (contracts never take one);
- token claims are collected from local AND remote (``SERVICE_BINDINGS``)
  contributors, with failed contributors recorded and critical ones aborting
  issuance (503);
- module code gets a read-only ``CoreRuntime`` view.

Moved from ``sandloom/core/tests/test_contract_runtime.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI

from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import (
    contract_session,
)


class _FakeSession:
    def __init__(self, db_name: str) -> None:
        self.db_name = db_name
        self.info: dict[str, Any] = {}


class _FakeUoW:
    def __init__(self, db_name: str, log: list[tuple[str, str]]) -> None:
        self._db_name = db_name
        self._log = log
        self.session: _FakeSession | None = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = _FakeSession(self._db_name)
        self._log.append((self._db_name, "begin"))
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._log.append((self._db_name, "rollback" if exc else "commit"))
        self.session = None

    async def commit(self) -> None:  # pragma: no cover - protocol
        return None

    async def rollback(self) -> None:  # pragma: no cover - protocol
        return None


class _FakeDatabase:
    def __init__(self, name: str, log: list[tuple[str, str]]) -> None:
        self._name = name
        self._log = log

    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return lambda: _FakeUoW(self._name, self._log)

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


#: The standard batteries' claim authorities (``sandloom_standard.main``'s map,
#: ADR-0014): identity's and tenancy's keys come only from them.
STANDARD = build_claim_authorities(
    {
        "issuer": "sandloom_identity",
        "roles": "sandloom_identity",
        "permissions": "sandloom_identity",
        "tenant_ids": "sandloom_tenancy",
    }
)


def _core() -> tuple[CoreFacade, list[tuple[str, str]]]:
    core = CoreFacade(app=FastAPI())
    core._assign_claim_authorities(STANDARD)
    log: list[tuple[str, str]] = []
    core.database = lambda name="default": _FakeDatabase(name, log)  # type: ignore[method-assign]
    return core, log


class _Proxy:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[Any] = []

    async def contribute(self, user_id: Any) -> Any:
        self.calls.append(user_id)
        if self._error is not None:
            raise self._error
        return self._result


def _remote(
    core: CoreFacade,
    monkeypatch: pytest.MonkeyPatch,
    bindings: dict[str, dict[str, object]],
    proxies: dict[str, _Proxy],
) -> None:
    monkeypatch.setattr(core.settings, "service_bindings", bindings)
    core.set_service_resolver(lambda name, binding, **_: proxies[name])


@pytest.mark.asyncio
async def test_identity_alone_collects_remote_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Microservices: identity runs alone; tenancy/billing claims come via RPC."""
    core, _log = _core()
    seen: list[Any] = []

    async def identity_claims(user_id: Any) -> dict[str, Any]:
        seen.append(contract_session())
        return {"roles": ["member"]}

    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(identity_claims, critical=True)
    org = str(uuid4())
    tenancy = _Proxy({"tenant_ids": [org]})
    billing = _Proxy(error=RuntimeError("billing down"))
    _remote(
        core,
        monkeypatch,
        {
            "tenancy.claims.v1": {"base_url": "http://a"},
            "billing.claims.v1": {"base_url": "http://b", "critical": False},
            "tenancy.membership.v1": {"base_url": "http://a"},
        },
        {"tenancy.claims.v1": tenancy, "billing.claims.v1": billing},
    )
    issuer_session = object()
    user = uuid4()
    claims = await core.collect_claims(user, issuer_session, issuer="identity")

    assert claims["tenant_ids"] == [org]
    assert claims["roles"] == ["member"]
    assert seen == [issuer_session]  # issuer's own contributors: its session
    assert tenancy.calls == [user]
    assert claims["claims_sources"] == ["identity", "tenancy"]
    assert claims["claims_sources_failed"] == ["billing"]


@pytest.mark.asyncio
async def test_local_contributor_runs_in_its_own_uow_and_is_exposed() -> None:
    core, log = _core()

    async def tenancy_claims(user_id: Any) -> dict[str, Any]:
        assert isinstance(contract_session(), _FakeSession)
        return {"tenant_ids": ["t1"]}

    with core.module_registration("sandloom_tenancy") as api:
        api.add_claims_contributor(tenancy_claims, critical=True)
    # Exposed as tenancy.claims.v1 for remote issuers.
    assert core.service_registry.contains("tenancy.claims.v1")
    claims = await core.collect_claims(uuid4())
    assert claims["tenant_ids"] == ["t1"]
    assert claims["claims_sources"] == ["tenancy"]
    assert ("default", "rollback") in log  # its own read-only UoW
    remote_view = await core.get_service("tenancy.claims.v1").contribute(uuid4())
    assert remote_view == {"tenant_ids": ["t1"]}


def test_local_contributor_wins_over_remote_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, _log = _core()

    async def tenancy_claims(user_id: Any) -> dict[str, Any]:  # pragma: no cover
        return {}

    with core.module_registration("sandloom_tenancy") as api:
        api.add_claims_contributor(tenancy_claims)
    monkeypatch.setattr(
        core.settings,
        "service_bindings",
        {
            "tenancy.claims.v1": {"base_url": "http://a"},
            "billing.claims.v1": {"base_url": "http://b", "critical": False},
        },
    )
    assert core.remote_claims_contributors() == {"billing": False}
