from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core import rpc
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import (
    rpc_request_headers,
    rpc_response_headers,
    rpc_settings,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import RemoteServiceProxy, install_service_rpc_routes
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc.wire import _CTX_HEADER, _pack, _unb64, _unpack

_Responder = Callable[[str, bytes, dict[str, str]], httpx.Response]

#: This process's RPC identity: every test calls itself as ``svc``.
_SVC = "svc"
_CONTRACTS = [
    "tenancy.membership.v1",
    "demo.scope.v1",
    "demo.errors.v1",
    "demo.ledger.v1",
]


@pytest.fixture(autouse=True)
def _keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Internal RPC is pair-MAC authenticated: key this process as ``svc``,
    # allowed to call the test contracts on itself.
    for name, value in rpc_settings(_SVC, allowed_calls={_SVC: _CONTRACTS}).items():
        monkeypatch.setattr(settings, name, value)


def _auth(target: str, body: bytes = b"", **kwargs: Any) -> dict[str, str]:
    """Authenticated headers of one raw request to ``svc`` itself."""
    return rpc_request_headers(settings, target, body, **kwargs)


def _bound(headers: dict[str, str], body: bytes, status: int = 200) -> httpx.Response:
    """A fake provider answer, keyed to the request like a real one."""
    return httpx.Response(
        status,
        content=body,
        headers=rpc_response_headers(settings, headers, status, body),
    )


class _FakeClient:
    """Stand-in for the shared pooled RPC client used by the proxy tests."""

    def __init__(self, responder: _Responder) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def post(
        self, url: str, *, content: bytes, headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        self.calls.append(
            {"url": url, "content": content, "headers": headers, "timeout": timeout}
        )
        return self._responder(url, content, headers)


@pytest.mark.asyncio
async def test_remote_proxy_decodes_uuid_and_refuses_a_session_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.module_api import ContractCallError

    result_id = uuid4()

    def responder(url: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
        body = _pack(
            {"ok": True, "result": {"$sandloom_type": "uuid", "value": str(result_id)}}
        )
        return _bound(headers, body)

    fake = _FakeClient(responder)
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: fake)
    proxy = RemoteServiceProxy(
        service_name="tenancy.membership.v1",
        binding={
            "base_url": "http://tenancy:2222",
            "timeout_seconds": 0.5,
            "service_id": _SVC,
        },
        default_timeout_seconds=1.0,
    )

    got = await proxy.get_role(uuid4())

    assert got == result_id
    call = fake.calls[0]
    assert call["timeout"] == 0.5  # per-binding timeout, now passed per request
    assert call["url"] == (
        "http://tenancy:2222/_sandloom/rpc/tenancy.membership.v1/get_role"
    )
    assert call["headers"]["X-SANDLOOM-RPC-Auth"].startswith("v=1;caller=svc;")
    assert "X-Internal-Token" not in call["headers"]
    assert call["headers"]["Content-Type"] == "application/msgpack"
    # A contract never takes a session: passing one is an argument error
    # (never sent), exactly like a local call.
    with pytest.raises(ContractCallError) as exc_info:
        await proxy.get_role(uuid4(), db=object())
    assert exc_info.value.status_code == 422
    assert len(fake.calls) == 1


def test_core_get_service_prefers_local_over_remote_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    core = CoreFacade(app=app)
    local = object()
    monkeypatch.setattr(
        core.settings,
        "service_bindings",
        {"tenancy.membership.v1": {"base_url": "http://tenancy:2222"}},
    )
    core.register_service("tenancy.membership.v1", local)

    # The local provider wins, bound to the contract runtime.
    assert core.get_service("tenancy.membership.v1").contract_impl is local


class _Echo:
    @classmethod
    @contract_method
    async def echo(cls, value: int) -> int:
        return value


class _OtherEcho(_Echo):
    pass


def test_get_service_reuses_the_bound_provider_until_its_inputs_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = CoreFacade(app=FastAPI())
    core.register_service("bench.echo.v1", _Echo)
    first = core.get_service("bench.echo.v1")
    # one provider per (impl, database): its method closures are reused too
    assert core.get_service("bench.echo.v1") is first
    assert core.get_service("bench.echo.v1").echo is first.echo

    # another database for the contract -> a provider bound to it
    monkeypatch.setitem(core._service_databases, "bench.echo.v1", "analytics")
    moved = core.get_service("bench.echo.v1")
    assert moved is not first
    assert moved._sandloom_database == "analytics"

    # a re-registered impl (rolled back + registered again) -> a fresh provider
    snapshot = core.snapshot_registrations()
    core.service_registry.remove("bench.echo.v1")
    core.register_service("bench.echo.v1", _OtherEcho)
    assert core.get_service("bench.echo.v1").contract_impl is _OtherEcho
    core.restore_registrations(snapshot)


def test_get_service_reuses_the_remote_proxy_until_the_binding_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    core = CoreFacade(app=app)
    install_service_rpc_routes(app, core)
    bindings = {"tenancy.membership.v1": {"base_url": "http://tenancy:2222"}}
    monkeypatch.setattr(core.settings, "service_bindings", bindings)
    proxy = core.get_service("tenancy.membership.v1")
    assert isinstance(proxy, RemoteServiceProxy)
    assert core.get_service("tenancy.membership.v1") is proxy

    bindings["tenancy.membership.v1"] = {"base_url": "http://tenancy:3333"}
    moved = core.get_service("tenancy.membership.v1")
    assert moved is not proxy
    assert moved._base_url == "http://tenancy:3333"
    monkeypatch.setattr(core.settings, "service_rpc_timeout_seconds", 7.0)
    retimed = core.get_service("tenancy.membership.v1")
    assert retimed is not moved
    assert retimed._timeout_seconds == 7.0


def test_get_service_degrades_explicitly_when_unavailable() -> None:
    # Neither local provider nor remote binding -> explicit 501 (not a bare 500),
    # and still a ServiceNotFound subclass so existing handlers keep working.
    from sandloom.core.module_api._service_registry import ServiceNotFound
    from sandloom.core.rpc import ServiceUnavailableInDeployment

    app = FastAPI()
    core = CoreFacade(app=app)
    with pytest.raises(ServiceUnavailableInDeployment) as exc_info:
        core.get_service("nowhere.svc.v1")
    assert exc_info.value.status_code == 501
    assert isinstance(exc_info.value, ServiceNotFound)


def _decode_ctx(headers: dict[str, str]) -> dict[str, Any]:
    import json

    return dict(json.loads(_unb64(headers[_CTX_HEADER])))


def _proxy(service: str = "demo.scope.v1") -> RemoteServiceProxy:
    return RemoteServiceProxy(
        service_name=service,
        binding={"base_url": "http://demo:1", "service_id": _SVC},
        default_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_rpc_proxy_propagates_bound_tenant_in_the_mac_covered_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A call made under a bound tenant must carry it on the wire so the remote
    # provider can re-bind the same scope — inside the MAC-covered context,
    # never as a bare X-Tenant-Id header a provider would have to trust.
    tenant_id = uuid4()

    def responder(url: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
        return _bound(headers, _pack({"ok": True, "result": "pong"}))

    fake = _FakeClient(responder)
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: fake)

    from sandloom.core.di import bind_tenant

    with bind_tenant(tenant_id):
        await _proxy().ping()
    call = fake.calls[0]
    assert "X-Tenant-Id" not in call["headers"]
    ctx = _decode_ctx(call["headers"])
    assert ctx["tenant_id"] == str(tenant_id)
    assert ctx["aud"] == _SVC
    assert "idk" not in ctx  # commands dedupe on their own idempotency argument


def _scope_app() -> tuple[FastAPI, CoreFacade]:
    from sandloom.core.di import current_tenant_id_optional

    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    _wire_fake_database(core, [])

    class Svc:
        @classmethod
        @contract_method
        async def whose_tenant(cls) -> str:
            tid = current_tenant_id_optional()
            return str(tid) if tid else "none"

    core.register_service("demo.scope.v1", Svc)
    install_service_rpc_routes(app, core)
    return app, core


def _tenant_headers(target: str, body: bytes, tenant: UUID | None) -> dict[str, str]:
    from sandloom.core.di import bind_tenant

    if tenant is None:
        return _auth(target, body)
    with bind_tenant(tenant):
        return _auth(target, body)


@pytest.mark.asyncio
async def test_rpc_server_rebinds_tenant_only_from_verified_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.rpc.context import _encode_context

    app, _core = _scope_app()
    tenant_id = uuid4()
    path = "/_sandloom/rpc/demo.scope.v1/whose_tenant"
    target = "demo.scope.v1/whose_tenant"
    body = _pack({})
    # Another tenant swapped into a verified request's context: the MAC covers
    # the context bytes, so the request no longer authenticates.
    forged = _tenant_headers(target, body, tenant_id)
    forged[_CTX_HEADER] = _encode_context(
        {**_decode_ctx(forged), "tenant_id": str(uuid4())}
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        bound = await client.post(
            path, content=body, headers=_tenant_headers(target, body, tenant_id)
        )
        unbound = await client.post(
            path, content=body, headers=_tenant_headers(target, body, None)
        )
        # A bare header is refused (would let any caller pick a tenant).
        bare = await client.post(
            path,
            content=body,
            headers={
                **_tenant_headers(target, body, None),
                "X-Tenant-Id": str(tenant_id),
            },
        )
        tampered = await client.post(path, content=body, headers=forged)

    assert _unpack(bound.content) == {"ok": True, "result": str(tenant_id)}
    assert _unpack(unbound.content) == {"ok": True, "result": "none"}
    assert bare.status_code == 400
    assert _unpack(bare.content)["error"]["code"] == "rpc_unsigned_tenant"
    assert tampered.status_code == 401


@pytest.mark.asyncio
async def test_rpc_server_hides_itself_from_a_request_without_credentials() -> None:
    app, _core = _scope_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Neither a pair MAC nor a signature: the endpoint does not exist.
        anonymous = await client.post(
            "/_sandloom/rpc/demo.scope.v1/whose_tenant", content=_pack({})
        )
        # The ops token never authenticates internal RPC.
        token = await client.post(
            "/_sandloom/rpc/demo.scope.v1/whose_tenant",
            content=_pack({}),
            headers={"X-Internal-Token": "x" * 40},
        )
    assert anonymous.status_code == 404
    assert token.status_code == 404


class _MemCache:
    """Minimal in-memory CacheProvider surface (incl. CAS) for idempotency."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        self.store[key] = value

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if self.store.get(key) != expected:
            return False
        self.store[key] = value
        return True

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        if self.store.get(key) != expected:
            return False
        del self.store[key]
        return True


@pytest.mark.asyncio
async def test_rpc_proxy_retries_transport_errors_under_a_fresh_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[dict[str, str]] = []

    def responder(url: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
        attempts.append(dict(headers))
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("slow")
        return _bound(headers, _pack({"ok": True, "result": "ok"}))

    fake = _FakeClient(responder)
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: fake)
    monkeypatch.setattr(rpc_proxy, "_RPC_RETRY_DELAY_SECONDS", 0)
    assert await _proxy().ping() == "ok"
    # One logical call, two attempts: never a replay of the first request.
    nonces = {h["X-SANDLOOM-RPC-Nonce"] for h in attempts}
    tags = {h["X-SANDLOOM-RPC-Auth"] for h in attempts}
    assert len(nonces) == 2 and len(tags) == 2


@pytest.mark.asyncio
async def test_rpc_timeout_is_503_and_documents_maybe_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def responder(url: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
        raise httpx.ReadTimeout("no answer")

    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: _FakeClient(responder))
    monkeypatch.setattr(rpc_proxy, "_RPC_RETRY_DELAY_SECONDS", 0)
    with pytest.raises(rpc.RemoteServiceUnavailable) as exc_info:
        await _proxy().ping()
    assert exc_info.value.status_code == 503
    assert "may have committed" in str(exc_info.value.detail)


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _FakeUoW:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.session: _FakeSession | None = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = _FakeSession()
        self._log.append("begin")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._log.append("rollback" if exc else "commit")
        self.session = None

    async def commit(self) -> None:  # pragma: no cover - protocol
        return None

    async def rollback(self) -> None:  # pragma: no cover - protocol
        return None


class _FakeDatabase:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return lambda: _FakeUoW(self._log)

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _wire_fake_database(core: CoreFacade, log: list[str]) -> list[str]:
    """Give a bare facade a fake database provider (records UoW outcomes)."""
    opened: list[str] = []

    def database(name: str = "default") -> _FakeDatabase:
        opened.append(name)
        return _FakeDatabase(log)

    core.database = database  # type: ignore[method-assign]
    return opened


@pytest.mark.asyncio
async def test_rpc_server_runs_contract_in_provider_uow() -> None:
    """The RPC server goes through the SAME contract runtime as a local call.

    The provider's method gets the provider's OWN Unit of Work session (via
    ``contract_session()``), opened on the database its module declares —
    never one opened by the RPC layer itself.
    """
    from sandloom.core.module_api import contract_method, contract_session

    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    log: list[str] = []
    opened = _wire_fake_database(core, log)
    org_id = uuid4()
    acting_user_id = uuid4()
    seen: list[Any] = []

    class Membership:
        @classmethod
        @contract_method
        async def get_role(cls, organization_id: UUID, user_id: UUID) -> str | None:
            assert organization_id == org_id
            assert user_id == acting_user_id
            seen.append(contract_session())
            return "owner"

    class Unsafe(Membership):
        @classmethod
        async def unsafe(cls) -> object:
            return object()

    # A public member that is not a wire-portable contract method never
    # registers (it would be callable locally but not remotely).
    from sandloom.core.contract_wire import ContractTypeError

    with pytest.raises(ContractTypeError, match="unsafe"):
        core.register_service("tenancy.unsafe.v1", Unsafe)
    core.register_service("tenancy.membership.v1", Membership, database="tenancy_db")
    install_service_rpc_routes(app, core)

    body = _pack(
        {
            "args": [
                {"$sandloom_type": "uuid", "value": str(org_id)},
                {"$sandloom_type": "uuid", "value": str(acting_user_id)},
            ]
        }
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/_sandloom/rpc/tenancy.membership.v1/get_role",
            content=body,
            headers=_auth("tenancy.membership.v1/get_role", body),
        )
        unsafe = await client.post(
            "/_sandloom/rpc/tenancy.membership.v1/unsafe",
            content=_pack({}),
            headers=_auth("tenancy.membership.v1/unsafe", _pack({})),
        )
        status = await client.get(
            "/_sandloom/rpc/tenancy.membership.v1",
            headers=_auth("tenancy.membership.v1", http_method="GET"),
        )

    assert resp.status_code == 200
    assert _unpack(resp.content) == {"ok": True, "result": "owner"}
    # Own UoW on the provider's declared database, rolled back (read contract).
    assert "tenancy_db" in opened
    assert log == ["begin", "rollback"]
    assert isinstance(seen[0], _FakeSession)
    assert unsafe.status_code == 404
    assert _unpack(unsafe.content)["error"]["code"] == "method_not_found"
    assert status.status_code == 200
    assert _unpack(status.content)["methods"] == ["get_role"]


def test_a_session_taking_provider_is_refused_at_registration() -> None:
    """A contract method never takes a session (it reads its provider's own
    through ``contract_session()``): an undecorated session-taking method is
    not a contract method and the provider does not register."""
    from sandloom.core.contract_wire import ContractTypeError

    class Undecorated:
        @classmethod
        async def get_role(
            cls, organization_id: UUID, user_id: UUID, db: AsyncSession
        ) -> str | None:  # pragma: no cover - never registered
            return "member"

    class SessionParam:
        @classmethod
        @contract_method
        async def get_role(
            cls, organization_id: UUID, db: AsyncSession
        ) -> str | None:  # pragma: no cover - never registered
            return "member"

    core = CoreFacade(app=FastAPI())
    with pytest.raises(ContractTypeError, match="neither a @contract_method"):
        core.register_service("legacy.membership.v1", Undecorated)
    with pytest.raises(ContractTypeError, match="session parameter"):
        core.register_service("legacy.membership.v1", SessionParam)


def _provider_app() -> tuple[FastAPI, CoreFacade, list[str]]:
    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    log: list[str] = []
    _wire_fake_database(core, log)
    return app, core, log


@pytest.mark.asyncio
async def test_rpc_errors_inside_provider_are_not_masked_as_404_or_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookup failure -> 404, binding failure -> 422, provider errors -> 500/own."""
    from sandloom.core.idempotency import cache_store as idempotency_cache_store

    monkeypatch.setattr(idempotency_cache_store, "cache", lambda: _MemCache())

    class Denied(Exception):
        http_status = 403
        code = "denied"
        detail = "not yours"

    class Provider:
        @classmethod
        @contract_method
        async def attr_bug(cls, n: int) -> int:
            return n.no_such_attribute  # type: ignore[attr-defined, no-any-return]

        @classmethod
        @contract_method
        async def value_bug(cls, n: int) -> int:
            raise ValueError("provider-internal value error")

        @classmethod
        @contract_method
        async def protocol_error(cls, n: int) -> int:
            raise Denied()

        @classmethod
        @contract_method
        async def ok(cls, n: int) -> int:
            return n + 1

    app, core, _log = _provider_app()
    core.register_service("demo.errors.v1", Provider)
    install_service_rpc_routes(app, core)

    async def _call(method: str, payload: dict[str, Any]) -> Any:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = _pack(payload)
            return await client.post(
                f"/_sandloom/rpc/demo.errors.v1/{method}",
                content=body,
                headers=_auth(f"demo.errors.v1/{method}", body),
            )

    attr = await _call("attr_bug", {"args": [1]})
    assert attr.status_code == 500
    assert _unpack(attr.content)["error"]["code"] == "provider_error"
    value = await _call("value_bug", {"args": [1]})
    assert value.status_code == 500
    proto = await _call("protocol_error", {"args": [1]})
    assert proto.status_code == 403
    assert _unpack(proto.content)["error"]["code"] == "denied"
    # Genuine lookup / binding failures keep their statuses.
    missing = await _call("nope", {"args": [1]})
    assert missing.status_code == 404
    assert _unpack(missing.content)["error"]["code"] == "method_not_found"
    bad_args = await _call("ok", {"args": [1, 2]})
    assert bad_args.status_code == 422
    good = await _call("ok", {"args": [1]})
    assert good.status_code == 200
    assert _unpack(good.content)["result"] == 2


class _CountingLedger:
    _calls: list[tuple[int, str]] = []

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def spend(cls, amount: int, operation_key: str) -> int:
        cls._calls.append((amount, operation_key))
        return 100 - amount


@pytest.mark.asyncio
async def test_command_contract_is_deduped_by_the_framework_locally_and_over_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.idempotency import cache_store as idempotency_cache_store
    from sandloom.core.module_api import ContractIdempotencyConflict

    mem = _MemCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda: mem)
    _CountingLedger._calls.clear()
    app, core, log = _provider_app()
    core.register_service("demo.ledger.v1", _CountingLedger)
    install_service_rpc_routes(app, core)
    svc = core.get_service("demo.ledger.v1")

    # Local: replay the stored result; never run the method twice.
    assert await svc.spend(10, "op-local") == 90
    assert await svc.spend(10, "op-local") == 90
    assert _CountingLedger._calls == [(10, "op-local")]
    assert log == ["begin", "commit"]
    with pytest.raises(ContractIdempotencyConflict):
        await svc.spend(20, "op-local")

    # RPC: same runtime, same dedupe namespace (a remote retry of the local
    # call replays too).
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        replay_body = _pack({"args": [10, "op-local"]})
        replay = await client.post(
            "/_sandloom/rpc/demo.ledger.v1/spend",
            content=replay_body,
            headers=_auth("demo.ledger.v1/spend", replay_body),
        )
        conflict_body = _pack({"args": [30, "op-local"]})
        conflict = await client.post(
            "/_sandloom/rpc/demo.ledger.v1/spend",
            content=conflict_body,
            headers=_auth("demo.ledger.v1/spend", conflict_body),
        )
    assert replay.status_code == 200
    assert _unpack(replay.content)["result"] == 90
    assert conflict.status_code == 409
    assert _unpack(conflict.content)["error"]["code"] == "contract_idempotency_conflict"
    assert _CountingLedger._calls == [(10, "op-local")]


@pytest.mark.asyncio
async def test_command_contract_fails_closed_when_the_store_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.idempotency import cache_store as idempotency_cache_store
    from sandloom.core.module_api import ContractIdempotencyUnavailable
    from sandloom.core.testing import InMemoryCache

    monkeypatch.setattr(
        idempotency_cache_store,
        "cache",
        lambda name="default": InMemoryCache(broken=True),
    )
    _CountingLedger._calls.clear()
    app, core, log = _provider_app()
    core.register_service("demo.ledger.v1", _CountingLedger)
    install_service_rpc_routes(app, core)

    with pytest.raises(ContractIdempotencyUnavailable):
        await core.get_service("demo.ledger.v1").spend(1, "op-down")
    body = _pack({"args": [1, "op-down"]})
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/_sandloom/rpc/demo.ledger.v1/spend",
            content=body,
            headers=_auth("demo.ledger.v1/spend", body),
        )
    # The command's dedupe (the contract runtime's, shared with local calls)
    # fails closed over RPC exactly like locally.
    assert resp.status_code == 503
    assert _unpack(resp.content)["error"]["code"] == "contract_idempotency_unavailable"
    assert _CountingLedger._calls == []
    assert log == []
