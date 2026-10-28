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
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.contract_testing import rpc_request_headers, rpc_settings
from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.facade import ClaimsUnavailable, CoreFacade, CoreRuntime
from sandloom.core.module_api import (
    ContractArgumentError,
    ContractIdempotencyKeyMissing,
    ContractProvider,
    ReadContractWriteError,
    bind_contract_session,
    contract_method,
    contract_session,
)
from sandloom.core.rpc import install_service_rpc_routes
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.testing import InMemoryCache
from sandloom.core.unit_of_work import scope as uow_mod_scope


@pytest.fixture(autouse=True)
def dedupe_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    """Command dedupe is fail-closed: give every test a working store."""
    from sandloom.core.idempotency import cache_store as idempotency_cache_store

    mem = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": mem)
    return mem


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


def _core() -> tuple[CoreFacade, list[tuple[str, str]]]:
    core = CoreFacade(app=FastAPI())
    log: list[tuple[str, str]] = []
    core.database = lambda name="default": _FakeDatabase(name, log)  # type: ignore[method-assign]
    return core, log


class _Ledger:
    """A provider with a read and a command contract."""

    _seen: list[Any] = []

    @classmethod
    @contract_method
    async def balance(cls, tenant_id: UUID) -> int:
        cls._seen.append(contract_session())
        return 7

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def spend(cls, tenant_id: UUID, amount: int, operation_key: str) -> int:
        cls._seen.append(contract_session())
        return 7 - amount

    @classmethod
    @contract_method
    async def sneaky_publish(cls) -> int:
        contract_session().info[uow_mod_scope._OUTBOX_PENDING_KEY] = ["event"]
        return 1


def _register_ledger(core: CoreFacade, *, database: str = "ledger_db") -> None:
    with core.module_registration("sandloom.ledger") as api:
        api.register_service("ledger.credits.v1", _Ledger)
    core.module_descriptors = [
        ModuleDescriptor(
            name="sandloom.ledger",
            version="1",
            capabilities=[CapabilityRequirement("database", instance=database)],
        )
    ]


@pytest.mark.asyncio
async def test_local_call_runs_in_providers_own_uow_on_its_own_database() -> None:
    core, log = _core()
    _register_ledger(core)
    _Ledger._seen.clear()
    svc = core.get_service("ledger.credits.v1")
    assert isinstance(svc, ContractProvider)
    assert core.contract_database("ledger.credits.v1") == "ledger_db"

    # The caller holds its own UoW session on another database …
    caller = _FakeSession("caller_db")
    token = uow_mod_scope._current_uow_session.set(caller)  # type: ignore[arg-type]
    stack = uow_mod_scope._active_uow_sessions.set((caller,))  # type: ignore[arg-type]
    try:
        assert await svc.balance(uuid4()) == 7
        # … and is restored after the call.
        current: Any = uow_mod_scope._current_uow_session.get()
        assert current is caller
    finally:
        uow_mod_scope._active_uow_sessions.reset(stack)
        uow_mod_scope._current_uow_session.reset(token)

    provider_session = _Ledger._seen[-1]
    assert provider_session is not caller
    assert provider_session.db_name == "ledger_db"
    # Read contract: own transaction, always rolled back.
    assert log == [("ledger_db", "begin"), ("ledger_db", "rollback")]


@pytest.mark.asyncio
async def test_command_commits_and_requires_idempotency_key() -> None:
    core, log = _core()
    _register_ledger(core)
    svc = core.get_service("ledger.credits.v1")
    assert await svc.spend(uuid4(), 2, "op-1") == 5
    assert log[-2:] == [("ledger_db", "begin"), ("ledger_db", "commit")]
    log.clear()
    with pytest.raises(ContractIdempotencyKeyMissing):
        await svc.spend(uuid4(), 2, "")
    assert log == []  # rejected before any UoW opened


@pytest.mark.asyncio
async def test_runtime_command_binds_its_arguments_once_per_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime binds + validates once; the method wrapper binds once more
    for the dedupe arguments (key check included) — no third binding."""
    import inspect

    core, _log = _core()
    _register_ledger(core)
    svc = core.get_service("ledger.credits.v1")
    binds: list[str] = []
    real_bind, real_partial = inspect.Signature.bind, inspect.Signature.bind_partial

    def bind(self: inspect.Signature, *a: Any, **k: Any) -> Any:
        binds.append("bind")
        return real_bind(self, *a, **k)

    def bind_partial(self: inspect.Signature, *a: Any, **k: Any) -> Any:
        binds.append("bind_partial")
        return real_partial(self, *a, **k)

    monkeypatch.setattr(inspect.Signature, "bind", bind)
    monkeypatch.setattr(inspect.Signature, "bind_partial", bind_partial)
    assert await svc.spend(uuid4(), 2, operation_key="op-once") == 5
    assert binds == ["bind", "bind_partial"]
    binds.clear()
    with pytest.raises(ContractIdempotencyKeyMissing):
        await svc.spend(uuid4(), 2, operation_key="  ")


@pytest.mark.asyncio
async def test_direct_command_call_shares_the_registered_dedupe_namespace() -> None:
    # GLM review P2: a direct call of a REGISTERED provider's command
    # (``_Ledger.spend(...)``) deduped under its qualname on the decorator's
    # database, while ``core.get_service(...)`` deduped under the contract
    # name on the provider's database: the same operation key ran twice.
    from sandloom.core.runtime import use_active_core

    core, log = _core()
    _register_ledger(core)
    _Ledger._seen.clear()
    tenant = uuid4()
    with use_active_core(core):
        assert await _Ledger.spend(tenant, 2, operation_key="op-direct") == 5
        assert log[0] == ("ledger_db", "begin")
        svc = core.get_service("ledger.credits.v1")
        assert await svc.spend(tenant, 2, operation_key="op-direct") == 5
        assert await _Ledger.spend(tenant, 2, operation_key="op-direct") == 5
    assert len(_Ledger._seen) == 1


@pytest.mark.asyncio
async def test_direct_command_call_of_a_provider_registered_twice_is_refused() -> None:
    from sandloom.core.runtime import use_active_core

    core, _log = _core()
    _register_ledger(core)
    core.register_service("ledger.credits_alias.v1", _Ledger)
    with use_active_core(core), pytest.raises(RuntimeError, match="get_service"):
        await _Ledger.spend(uuid4(), 2, operation_key="op-ambiguous")


@pytest.mark.asyncio
async def test_read_contract_cannot_publish() -> None:
    core, log = _core()
    _register_ledger(core)
    with pytest.raises(ReadContractWriteError):
        await core.get_service("ledger.credits.v1").sneaky_publish()
    assert log[-1] == ("ledger_db", "rollback")


@pytest.mark.asyncio
async def test_caller_session_is_an_argument_error() -> None:
    core, log = _core()
    _register_ledger(core)
    _Ledger._seen.clear()
    caller = AsyncSession()
    svc = core.get_service("ledger.credits.v1")
    with pytest.raises(ContractArgumentError):
        await svc.balance(uuid4(), caller)
    with pytest.raises(ContractArgumentError):
        await svc.spend(uuid4(), 1, "op-2", db=caller)
    assert _Ledger._seen == []
    assert log == []  # refused before any UoW opened
    await caller.close()


def test_command_contract_must_name_its_idempotency_key() -> None:
    with pytest.raises(ValueError, match="idempotency_key"):

        @contract_method(command=True)
        async def bad(x: int) -> int:  # pragma: no cover - never called
            return x

    with pytest.raises(ValueError, match="not a parameter"):

        @contract_method(command=True, idempotency_key="key")
        async def bad2(x: int) -> int:  # pragma: no cover - never called
            return x


def test_contract_session_outside_a_call_raises() -> None:
    with pytest.raises(RuntimeError, match="outside a contract invocation"):
        contract_session()


@pytest.mark.asyncio
async def test_bind_contract_session_is_the_test_seam() -> None:
    core, log = _core()
    _register_ledger(core)
    _Ledger._seen.clear()
    owned = object()
    with bind_contract_session(owned):
        assert await core.get_service("ledger.credits.v1").balance(uuid4()) == 7
    assert _Ledger._seen == [owned]
    assert log == []  # no UoW opened: the test owns the transaction


@pytest.mark.asyncio
async def test_local_and_rpc_paths_are_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same provider, same UoW behaviour, same result — local vs internal RPC."""
    from sandloom.core.config.settings import settings

    allowed = {"svc": ["ledger.credits.v1"]}
    for name, value in rpc_settings("svc", allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    core, log = _core()
    app = core.app
    app.state.core = core
    _register_ledger(core)
    install_service_rpc_routes(app, core)
    tenant = uuid4()

    local = await core.get_service("ledger.credits.v1").spend(tenant, 3, "op-l")
    local_log = list(log)
    log.clear()

    target = "ledger.credits.v1/spend"
    body = rpc_wire._pack(
        {"args": [{"$sandloom_type": "uuid", "value": str(tenant)}, 3, "op-r"]}
    )
    no_key = rpc_wire._pack(
        {"args": [{"$sandloom_type": "uuid", "value": str(tenant)}, 3, ""]}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/_sandloom/rpc/{target}",
            content=body,
            headers=rpc_request_headers(settings, target, body, sign=True),
        )
        missing = await client.post(
            f"/_sandloom/rpc/{target}",
            content=no_key,
            headers=rpc_request_headers(settings, target, no_key, sign=True),
        )
    assert resp.status_code == 200, resp.content
    assert rpc_wire._unpack(resp.content)["result"] == local == 4
    assert log == local_log == [("ledger_db", "begin"), ("ledger_db", "commit")]
    # The idempotency-key requirement holds over RPC too (422).
    assert missing.status_code == 422


# ---------------------------------------------------------------------------
# Token claims: local + remote contributors (C2)
# ---------------------------------------------------------------------------


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
async def test_critical_remote_contributor_failure_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, _log = _core()
    _remote(
        core,
        monkeypatch,
        {"tenancy.claims.v1": {"base_url": "http://a"}},  # critical by default
        {"tenancy.claims.v1": _Proxy(error=RuntimeError("unreachable"))},
    )
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4())
    assert exc_info.value.status_code == 503
    assert exc_info.value.owner == "tenancy"


@pytest.mark.asyncio
async def test_critical_local_contributor_failure_is_503() -> None:
    core, _log = _core()

    async def broken(user_id: Any) -> dict[str, Any]:
        raise RuntimeError("db down")

    with core.module_registration("sandloom_tenancy") as api:
        api.add_claims_contributor(broken, critical=True)
    with pytest.raises(ClaimsUnavailable):
        await core.collect_claims(uuid4())


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


@pytest.mark.asyncio
async def test_unexposable_contributor_runs_on_its_modules_declared_database() -> None:
    """No ``<owner>.claims.v1`` contract (unnameable owner): still the module's DB.

    The fallback used to open the contributor's UoW on ``"default"``.
    """
    core, log = _core()

    async def claims(user_id: Any) -> dict[str, Any]:
        return {"x": 1}

    with core.module_registration("acme.odd-mod") as api:
        api.add_claims_contributor(claims)
    core.module_descriptors = [
        ModuleDescriptor(
            name="acme.odd-mod",
            version="1",
            capabilities=[CapabilityRequirement("database", instance="odd_db")],
        )
    ]
    assert not core.service_registry.contains("odd-mod.claims.v1")
    result = await core.collect_claims(uuid4())
    assert result["x"] == 1
    assert log == [("odd_db", "begin"), ("odd_db", "rollback")]


def test_facts_contract_build_failure_fails_registration() -> None:
    # GLM review: any failure exposing <owner>.facts.v1 was only logged, so a
    # broken facts contract surfaced as a silently local-only owner in a
    # split deployment. Only an unnameable owner (an invalid contract name)
    # stays local-only by design; any other failure aborts the load.
    core, _log = _core()

    class _BrokenFact:
        name = "demo.broken"
        owner_module = "demo"

        @staticmethod
        def build_facts_contract(core: Any, owner: str) -> Any:
            raise RuntimeError("cannot build the facts contract")

    with pytest.raises(RuntimeError, match="cannot build"):
        core.register_access_fact(_BrokenFact())

    class _OddFact(_BrokenFact):
        name = "demo.odd"
        owner_module = "odd-mod"

        @staticmethod
        def build_facts_contract(core: Any, owner: str) -> Any:
            return object()

    core.register_access_fact(_OddFact())  # unnameable owner: local-only
    assert not core.service_registry.contains("odd-mod.facts.v1")


@pytest.mark.asyncio
async def test_collect_claims_refuses_a_caller_session_without_issuer() -> None:
    core = CoreFacade(app=FastAPI())
    with pytest.raises(ValueError, match="issuer"):
        await core.collect_claims(uuid4(), object())
    claims = await core.collect_claims(uuid4())
    assert claims == {"claims_sources": [], "claims_sources_failed": []}


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


# ---------------------------------------------------------------------------
# H5 / X1 / X2
# ---------------------------------------------------------------------------


async def test_get_core_returns_read_only_runtime_view() -> None:
    from sandloom.core.di import get_core

    core = CoreFacade(app=FastAPI())
    core.app.state.core = core
    request = Request({"type": "http", "app": core.app})
    view = await get_core(request)
    assert isinstance(view, CoreRuntime)
    for forbidden in (
        "event_bus",
        "infra",
        "service_registry",
        "register_service",
        "add_claims_contributor",
        "app",
        "set_infra",
    ):
        assert not hasattr(view, forbidden), forbidden
    assert view.settings is core.settings
    with pytest.raises(AttributeError):
        view.event_bus = object()  # type: ignore[attr-defined]


def test_di_reexports_typed_contract_errors() -> None:
    from sandloom.core import di
    from sandloom.core.module_api._service_registry import (
        ServiceNotFound,
        ServiceUnavailableInDeployment,
    )

    assert di.ServiceUnavailableInDeployment is ServiceUnavailableInDeployment
    assert di.ServiceNotFound is ServiceNotFound
    # The legacy RPC idempotency seam is gone.
    assert not hasattr(di, "rpc_idempotency_key")
    # Private UoW state is no longer re-exported from the public module.
    for private in ("_current_uow_session", "_OUTBOX_STORE_KEY", "_stash_stores"):
        assert not hasattr(di, private), private


def test_increment_metric_is_an_atomic_counter() -> None:
    from prometheus_client import REGISTRY

    core = CoreFacade(app=FastAPI())
    name = f"test_counter_{uuid4().hex[:8]}"

    def bump() -> None:
        for _ in range(500):
            core.increment_metric(name)

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert core.get_metric(name) == 4000
    exported = REGISTRY.get_sample_value(
        "sandloom_facade_counter_total", {"name": name}
    )
    assert exported == 4000
    with pytest.raises(ValueError):
        core.increment_metric(name, -1)


def test_shared_metrics_uses_the_real_counter() -> None:
    from sandloom.shared.authz.metrics import increment

    core = CoreFacade(app=FastAPI())
    calls: list[tuple[str, int]] = []
    original = core.increment_metric

    def spy(name: str, amount: int | float = 1) -> None:
        calls.append((name, int(amount)))
        original(name, amount)

    core.increment_metric = spy  # type: ignore[method-assign]
    increment(core, "access_denied_total")
    assert calls == [("access_denied_total", 1)]
    assert core.get_metric("access_denied_total") == 1


class _BarrierProxy:
    """A remote claims contract that only returns once ALL peers have started."""

    def __init__(self, name: str, started: dict[str, Any], expected: int) -> None:
        self._name = name
        self._started = started
        self._expected = expected

    async def contribute(self, user_id: Any) -> dict[str, Any]:
        import asyncio

        self._started[self._name] = True
        event = self._started.setdefault("_all", asyncio.Event())
        if len([k for k in self._started if not k.startswith("_")]) == self._expected:
            event.set()
        await asyncio.wait_for(event.wait(), timeout=2)
        return {f"{self._name}_claim": True}


@pytest.mark.asyncio
async def test_independent_claims_contributors_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two contributors that each wait for the other only finish if concurrent;
    outcomes still merge in registration order with the same provenance."""
    core, _log = _core()
    started: dict[str, Any] = {}
    _remote(
        core,
        monkeypatch,
        {
            "tenancy.claims.v1": {"base_url": "http://a"},
            "billing.claims.v1": {"base_url": "http://b"},
        },
        {
            "tenancy.claims.v1": _BarrierProxy("tenancy", started, 2),  # type: ignore[dict-item]
            "billing.claims.v1": _BarrierProxy("billing", started, 2),  # type: ignore[dict-item]
        },
    )
    claims = await core.collect_claims(uuid4())
    assert claims["tenancy_claim"] is True and claims["billing_claim"] is True
    assert claims["claims_sources"] == ["billing", "tenancy"]
    assert claims["claims_sources_failed"] == []


@pytest.mark.asyncio
async def test_first_critical_failure_in_registration_order_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, _log = _core()
    _remote(
        core,
        monkeypatch,
        {
            "tenancy.claims.v1": {"base_url": "http://a"},
            "billing.claims.v1": {"base_url": "http://b"},
        },
        {
            "tenancy.claims.v1": _Proxy(error=RuntimeError("tenancy down")),
            "billing.claims.v1": _Proxy(error=RuntimeError("billing down")),
        },
    )
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4())
    assert exc_info.value.owner == "tenancy"


@pytest.mark.asyncio
async def test_bound_test_session_keeps_contributors_sequential() -> None:
    import asyncio

    core, _log = _core()
    running: list[int] = []
    overlaps: list[int] = []

    def make(name: str) -> Any:
        async def contributor(user_id: Any) -> dict[str, Any]:
            if running:
                overlaps.append(1)
            running.append(1)
            await asyncio.sleep(0)
            running.pop()
            return {name: True}

        return contributor

    with core.module_registration("sandloom_tenancy") as api:
        api.add_claims_contributor(make("t"))
    with core.module_registration("sandloom_billing") as api:
        api.add_claims_contributor(make("b"))
    with bind_contract_session(_FakeSession("default")):
        claims = await core.collect_claims(uuid4())
    assert claims["t"] and claims["b"]
    assert overlaps == []  # one shared session: never concurrent


@pytest.mark.asyncio
async def test_get_service_resolves_from_the_active_core_and_optional_only_when_undeployed() -> (
    None
):
    # Public API 1.0 §5.3: ``get_service(name, optional=True)`` is ``None`` only
    # when the provider is not deployed (no local provider, no binding).
    from sandloom.core import di
    from sandloom.core.runtime import use_active_core

    core, _log = _core()
    _register_ledger(core)
    with use_active_core(core):
        svc = di.get_service("ledger.credits.v1")
        assert isinstance(svc, ContractProvider)
        assert di.get_service("ledger.credits.v1", optional=True) is not None
        assert di.get_service("nobody.here.v1", optional=True) is None
        with pytest.raises(di.ServiceUnavailableInDeployment):
            di.get_service("nobody.here.v1")


def test_classvar_constants_on_a_provider_are_allowed_and_never_exposed() -> None:
    from typing import ClassVar

    from sandloom.core.module_api._provider import contract_schema

    class _WithConstants:
        NAME: ClassVar[str] = "ledger.credits.v1"
        LIMIT: ClassVar[int] = 3

        @classmethod
        @contract_method
        async def balance(cls, tenant_id: UUID) -> int:
            return cls.LIMIT

    assert set(contract_schema(_WithConstants).methods) == {"balance"}

    class _PlainAttribute:
        LIMIT = 3  # not a ClassVar: still a non-portable public member

        @classmethod
        @contract_method
        async def balance(cls, tenant_id: UUID) -> int:
            return 1

    with pytest.raises(Exception, match="LIMIT"):
        contract_schema(_PlainAttribute)
