"""Regression seams for the 22 October kernel review."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import create_model

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import FailedModule, ModuleDescriptor, ModuleLoadSummary
from sandloom.core.di import make_db_dependency
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import (
    ModuleLoadError,
    ModuleRegistrationError,
    _invoke_register,
)
from sandloom.core.module_api import ModuleRegistrationConflict, contract_method

ForeignDTO = create_model("ForeignDTO", __module__="app.payments.dto", count=(int, ...))


class ForeignDTOContract:
    @classmethod
    @contract_method
    async def get(cls) -> ForeignDTO:
        return ForeignDTO(count=1)


def test_named_database_dependency_has_one_callable_identity() -> None:
    assert make_db_dependency("analytics") is make_db_dependency("analytics")
    assert make_db_dependency("analytics") is not make_db_dependency("other")


def test_register_must_return_a_descriptor() -> None:
    module = SimpleNamespace(__name__="app.broken", register=lambda _: None)
    with pytest.raises(ModuleRegistrationError, match="must return a ModuleDescriptor"):
        _invoke_register(module, None)  # type: ignore[arg-type]


def test_expected_loader_deferral_is_not_logged_as_a_failed_registration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sandloom.core.loader.plugin_loader import _DeferModule

    core = CoreFacade(app=FastAPI())
    with pytest.raises(_DeferModule):
        with core.module_registration("app.deferred"):
            raise _DeferModule("app.first")
    assert "Discarding partial registration" not in caplog.text


def test_module_load_error_explains_each_failure() -> None:
    summary = ModuleLoadSummary()
    summary.failed.append(
        FailedModule(name="app.a", error="missing capability", required=True)
    )
    assert "missing capability" in str(ModuleLoadError(summary))


def test_module_load_error_redacts_credentials_in_actionable_details() -> None:
    summary = ModuleLoadSummary()
    summary.failed.append(
        FailedModule(
            name="app.a",
            error="connection redis://:private-value@host failed",
            required=True,
        )
    )
    assert "private-value" not in str(ModuleLoadError(summary))


def test_rollback_restores_claims_owner_module_mapping() -> None:
    core = CoreFacade(app=FastAPI())
    core._claims_owner_modules["billing"] = "original.billing"
    snapshot = core.snapshot_registrations()
    core._claims_owner_modules["billing"] = "failed.billing"
    core._claims_owner_modules["new"] = "failed.new"
    core.restore_registrations(snapshot)
    assert core._claims_owner_modules == {"billing": "original.billing"}


def test_registration_refuses_dto_from_another_configured_module() -> None:
    core = CoreFacade(
        app=FastAPI(),
        settings_obj=settings.model_copy(
            update={"sandloom_modules": ["app.reports", "app.payments"]}
        ),
    )
    with pytest.raises(ModuleRegistrationConflict, match="app.payments"):
        with core.module_registration("app.reports") as api:
            api.register_service("reports.foreign.v1", ForeignDTOContract)


def test_module_api_cannot_resolve_siblings_while_registering() -> None:
    core = CoreFacade(app=FastAPI())
    with pytest.raises(ModuleRegistrationConflict, match="registration"):
        with core.module_registration("app.reports") as api:
            api.get_service("another.contract.v1")


def test_command_scope_names_are_checked_at_decoration() -> None:
    async def spend(operation_key: str) -> None:
        pass

    with pytest.raises(ValueError, match="missing"):
        contract_method(
            command=True,
            idempotency_key="operation_key",
            idempotency_scope=("missing",),
        )(spend)


def test_command_scope_changes_are_breaking_in_contract_compatibility() -> None:
    import copy

    from sandloom.core.contract_compat import compare_contract
    from sandloom.core.module_api import contract_schema

    descriptor = {
        "methods": {
            name: value.describe()
            for name, value in contract_schema(ScopedLedger).methods.items()
        }
    }
    before = copy.deepcopy(descriptor)
    before["methods"]["spend"].pop("idempotency_scope")
    report = compare_contract("ledger.review.v1", before, descriptor)
    assert any("idempotency scope changed" in error for error in report.errors)


def test_module_database_resolution_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = CoreFacade(app=FastAPI())
    descriptor = ModuleDescriptor(name="app.ledger", version="1")
    calls = 0
    original = ModuleDescriptor.database_instances

    def resolve(self: ModuleDescriptor) -> set[str]:
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(ModuleDescriptor, "database_instances", resolve)
    core.module_descriptors = [descriptor]
    core._service_owners["ledger.read.v1"] = "app.ledger"
    for _ in range(5):
        assert core.contract_database("ledger.read.v1") == "default"
    assert calls == 1


class ScopedLedger:
    _calls: list[str] = []

    @classmethod
    @contract_method(
        command=True,
        idempotency_key="operation_key",
        idempotency_scope=("customer_id",),
    )
    async def spend(cls, customer_id: str, operation_key: str) -> int:
        cls._calls.append(customer_id)
        return len(cls._calls)


@pytest.fixture
def fallback_core(monkeypatch: pytest.MonkeyPatch) -> CoreFacade:
    from sandloom.core.idempotency import cache_store as idempotency_cache_store
    from sandloom.core.testing import InMemoryCache
    from sandloom.core.tests.test_contract_runtime import _core

    core, _ = _core()
    cache = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": cache)
    ScopedLedger._calls.clear()
    core.register_service("ledger.review.v1", ScopedLedger)
    return core


@pytest.mark.asyncio
async def test_domain_scope_separates_customers_but_replays_each_operation(
    fallback_core: CoreFacade,
) -> None:
    contract = fallback_core.get_service("ledger.review.v1")
    assert await contract.spend("customer-a", "same-key") == 1
    assert await contract.spend("customer-b", "same-key") == 2
    assert await contract.spend("customer-a", "same-key") == 1
    assert ScopedLedger._calls == ["customer-a", "customer-b"]


@pytest.mark.asyncio
async def test_single_tenant_commands_use_one_key_inside_and_outside_uow(
    fallback_core: CoreFacade,
) -> None:
    from uuid import uuid4

    from sandloom.core.tenant_context import bind_tenant

    tenant = uuid4()
    fallback_core.settings = settings.model_copy(
        update={"tenancy_mode": "single", "single_tenant_id": tenant}
    )
    contract = fallback_core.get_service("ledger.review.v1")
    assert await contract.spend("a", "same-key") == 1
    with bind_tenant(tenant):
        assert await contract.spend("a", "same-key") == 1
    assert ScopedLedger._calls == ["a"]


@pytest.mark.asyncio
async def test_dispatch_cancellation_after_commit_keeps_fallback_replay(
    fallback_core: CoreFacade,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from sandloom.core import unit_of_work

    async def cancelled(session: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(unit_of_work, "_dispatch_outbox_after_commit", cancelled)
    contract = fallback_core.get_service("ledger.review.v1")
    with pytest.raises(asyncio.CancelledError):
        await contract.spend("a", "same-key")
    assert await contract.spend("a", "same-key") == 1
    assert ScopedLedger._calls == ["a"]


def test_schema_cache_is_bounded_for_transient_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # contract_schema reads its cache bound from the defining submodule.
    from sandloom.core.module_api import _provider as module_api

    module_api._schema_cache.clear()
    monkeypatch.setattr(module_api, "_SCHEMA_CACHE_MAX", 2)
    for _ in range(5):
        module_api.contract_schema(ScopedLedger())
    assert len(module_api._schema_cache) == 2


def test_metric_name_budget_prevents_unbounded_runtime_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core import facade

    core = CoreFacade(app=FastAPI())
    monkeypatch.setattr(facade, "_FACADE_METRIC_NAME_LIMIT", 2)
    core.increment_metric("one")
    core.register_metric("two", 2)
    core.increment_metric("one")
    with pytest.raises(ValueError, match="budget"):
        core.increment_metric("three")
    with pytest.raises(ValueError, match="budget"):
        core.register_metric("four", 4)
    assert core.list_metrics() == {"one": 2, "two": 2}


def _bearer(sub: str) -> dict[str, str]:
    from sandloom.core.auth import tokens

    token = tokens.create_access_token(data={"sub": sub, "token_type": "access"})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("opened_first", [False, True])
async def test_revocation_at_commit_covers_named_db_in_either_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
    opened_first: bool,
) -> None:
    from typing import Annotated, Any

    from fastapi import HTTPException
    from httpx import ASGITransport, AsyncClient

    from sandloom.core import di
    from sandloom.core.http import route_runtime
    from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
    from sandloom.core.tests.test_before_commit import _Session
    from sandloom.core.tests.test_contract_runtime import _FakeDatabase

    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    sessions: list[_Session] = []
    revoked = False

    def factory(name: str) -> Any:
        session = _Session()
        sessions.append(session)
        return lambda: SQLAlchemyUnitOfWork(lambda: session)

    async def check(claims: Any) -> None:
        if revoked:
            raise HTTPException(401, "revoked during handler")

    monkeypatch.setattr(core, "get_uow_factory", factory)
    monkeypatch.setattr(
        core, "database", lambda name="default": _FakeDatabase(name, [])
    )
    monkeypatch.setattr(route_runtime._verification, "assert_token_valid_now", check)
    deps = [di.at_commit()]
    if opened_first:
        deps.insert(0, di.Database("analytics"))

    async def handler(db: Any) -> dict[str, bool]:
        nonlocal revoked
        revoked = True
        return {"ok": True}

    handler.__annotations__["db"] = Annotated[Any, di.Database("analytics")]
    app.post("/write", dependencies=deps)(handler)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/write", headers=_bearer("u-1"))
    assert response.status_code == 401
    assert len(sessions) == 1
    assert sessions[0].calls == ["begin", "rollback", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_inner", [False, True])
async def test_nested_http_request_has_independent_commit_authority(
    monkeypatch: pytest.MonkeyPatch,
    guard_inner: bool,
) -> None:
    from typing import Annotated, Any

    from fastapi import HTTPException
    from httpx import ASGITransport, AsyncClient

    from sandloom.core import di
    from sandloom.core.http import route_runtime
    from sandloom.core.http.request_authority import RequestAuthorityMiddleware
    from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
    from sandloom.core.tests.test_before_commit import _Session
    from sandloom.core.tests.test_contract_runtime import _FakeDatabase

    app = FastAPI()
    app.add_middleware(RequestAuthorityMiddleware)
    core = CoreFacade(app=app)
    app.state.core = core
    sessions: dict[str, _Session] = {}
    outer_revoked = False
    inner_status: list[int] = []

    def factory(name: str) -> Any:
        session = _Session()
        sessions[name] = session
        return lambda: SQLAlchemyUnitOfWork(lambda: session)

    async def check(claims: Any) -> None:
        if claims.get("sub") == "outer" and outer_revoked:
            raise HTTPException(401, "outer revoked")

    monkeypatch.setattr(core, "get_uow_factory", factory)
    monkeypatch.setattr(
        core, "database", lambda name="default": _FakeDatabase(name, [])
    )
    monkeypatch.setattr(route_runtime._verification, "assert_token_valid_now", check)

    async def inner(db: Any) -> dict[str, bool]:
        return {"ok": True}

    async def outer(db: Any) -> dict[str, bool]:
        nonlocal outer_revoked
        outer_revoked = True
        response = await client.post("/inner", headers=_bearer("owner"))
        inner_status.append(response.status_code)
        return {"ok": True}

    inner.__annotations__["db"] = Annotated[Any, di.Database("inner")]
    outer.__annotations__["db"] = Annotated[Any, di.Database("outer")]
    inner_deps = [di.at_commit()] if guard_inner else []
    app.post("/inner", dependencies=inner_deps, status_code=201)(inner)
    app.post("/outer", dependencies=[di.at_commit()])(outer)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/outer", headers=_bearer("outer"))
    assert inner_status == [201]
    assert response.status_code == 401
    assert sessions["inner"].calls == ["begin", "commit", "close"]
    assert sessions["outer"].calls == ["begin", "rollback", "close"]


def test_authority_boundary_wraps_regular_and_rpc_paths_once() -> None:
    from sandloom.core.http.middlewares import register_middlewares
    from sandloom.core.http.request_authority import RequestAuthorityMiddleware
    from sandloom.core.rpc import install_service_rpc_routes

    app = FastAPI()
    core = CoreFacade(app=app)
    register_middlewares(app)
    install_service_rpc_routes(app, core)
    classes = [item.cls for item in app.user_middleware]
    assert classes[0] is RequestAuthorityMiddleware
    assert classes.count(RequestAuthorityMiddleware) == 1


@pytest.mark.asyncio
async def test_reused_session_does_not_keep_an_old_requests_authority_check() -> None:
    from sandloom.core.tests.test_before_commit import _Session
    from sandloom.core.unit_of_work.hooks import _run_before_commit_callbacks
    from sandloom.core.unit_of_work.request import (
        RequestScope,
        bind_request_scope,
        claim_unit,
    )
    from sandloom.core.unit_of_work.scope import _isolated_request_authority_scope

    session = _Session()
    old_principal_checked: list[bool] = []

    async def old_check() -> None:
        old_principal_checked.append(True)

    old = RequestScope(core=None, policy=None)
    old.check = old_check
    old.at_commit = True

    # The first request is aborted before commit; a test session is reused.
    with _isolated_request_authority_scope(), bind_request_scope(old):
        claim_unit(session)
    with _isolated_request_authority_scope():
        claim_unit(session)  # the next request (no scope) takes the session
        await _run_before_commit_callbacks(session)
    assert old_principal_checked == []

    # A protected commit runs its check once; a later helper invocation does
    # not repeat an already-completed transaction's authorization check.
    with _isolated_request_authority_scope(), bind_request_scope(old):
        claim_unit(session)
        await _run_before_commit_callbacks(session)
        await _run_before_commit_callbacks(session)
    assert old_principal_checked == [True]


@pytest.mark.asyncio
async def test_http_boundary_clears_and_restores_tenant_and_system_bypass() -> None:
    from uuid import uuid4

    from httpx import ASGITransport, AsyncClient

    from sandloom.core.http.request_authority import RequestAuthorityMiddleware
    from sandloom.core.tenant_context import (
        _bypass,
        bind_tenant,
        current_tenant_id_optional,
        unscoped,
    )

    app = FastAPI()
    app.add_middleware(RequestAuthorityMiddleware)

    @app.get("/scope")
    async def scope() -> dict[str, object]:
        tenant = current_tenant_id_optional()
        return {
            "tenant": str(tenant) if tenant is not None else None,
            "system": _bypass.get(),
        }

    tenant = uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with bind_tenant(tenant), unscoped():
            response = await client.get("/scope")
            assert response.json() == {"tenant": None, "system": False}
            assert current_tenant_id_optional() == tenant
            assert _bypass.get() is True


@pytest.mark.asyncio
async def test_http_boundary_does_not_inherit_local_contract_sessions_or_memo() -> None:
    from httpx import ASGITransport, AsyncClient

    from sandloom.core.http.request_authority import RequestAuthorityMiddleware
    from sandloom.core.module_api import _context as module_api_context

    variables = (
        module_api_context._contract_session_var,
        module_api_context._contract_session_override,
        module_api_context._contract_target,
        module_api_context._contract_reads_var,
        module_api_context._read_scope_var,
    )
    app = FastAPI()
    app.add_middleware(RequestAuthorityMiddleware)

    @app.get("/scope")
    async def scope() -> list[bool]:
        return [variable.get() is None for variable in variables]

    markers = [object() for _ in variables]
    tokens = [
        variable.set(marker)
        for variable, marker in zip(variables, markers, strict=True)
    ]
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/scope")
        assert response.json() == [True] * len(variables)
        assert [variable.get() for variable in variables] == markers
    finally:
        for variable, token in reversed(list(zip(variables, tokens, strict=True))):
            variable.reset(token)


@pytest.mark.asyncio
async def test_unrolled_authority_boundary_matches_the_scope_helpers() -> None:
    """The middleware's unrolled sets == the helper scopes, var for var.

    Every module-level ContextVar of the modules the boundary touches is set
    to a marker; the values the app sees inside the middleware must equal
    those inside ``_isolated_request_authority_scope`` (+ the issue-floor
    reset), and every marker must be restored on exit. A
    variable added to either helper without the middleware fails here.
    """
    from contextvars import ContextVar

    from sandloom.core import tenant_context
    from sandloom.core.auth import marks
    from sandloom.core.http.request_authority import RequestAuthorityMiddleware
    from sandloom.core.module_api import _context as module_api  # the contract vars
    from sandloom.core.unit_of_work import scope as uow_scope

    variables = [
        value
        for module in (uow_scope, tenant_context, module_api, marks)
        for value in vars(module).values()
        if isinstance(value, ContextVar)
    ]
    markers = {var: object() for var in variables}
    tokens = [(var, var.set(marker)) for var, marker in markers.items()]
    try:
        seen: dict[ContextVar[object], object] = {}

        async def app(scope: object, receive: object, send: object) -> None:
            seen.update({var: var.get() for var in variables})

        await RequestAuthorityMiddleware(app)({"type": "http"}, None, None)
        assert {var: var.get() for var in variables} == markers

        floors = marks._issue_floors.set(None)
        try:
            with uow_scope._isolated_request_authority_scope():
                expected = {var: var.get() for var in variables}
        finally:
            marks._issue_floors.reset(floors)
        assert seen == expected
        assert any(seen[var] is not markers[var] for var in variables)
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
