"""Attack-path regressions from the 22 October security review."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from sandloom.core import idempotency, rpc, rpc_binding, rpc_pair_mac
from sandloom.core.auth import hashing, marks, tokens, verification
from sandloom.core.auth.blocks import BlockUnavailableError
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.http.error_handlers import protocol_response
from sandloom.core.http.middlewares import _add_security_headers
from sandloom.core.idempotency import capture as idempotency_capture
from sandloom.core.idempotency import fingerprint as idempotency_fingerprint
from sandloom.core.idempotency import guard as idempotency_guard_module
from sandloom.core.ingress.client_ip import get_client_ip
from sandloom.core.rpc import client as rpc_client
from sandloom.core.rpc import inbound_auth as rpc_inbound_auth
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import server as rpc_server
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.rpc_transport import HttpxRpcClient, RpcResponseTooLarge


@pytest.fixture
def keyed_caller(monkeypatch) -> None:
    """The process keyed as service ``caller`` with the ``provider`` peer
    published (every outbound call needs its pair key)."""
    for name, value in rpc_settings("caller", peers=("provider",)).items():
        monkeypatch.setattr(settings, name, value)


def test_duplicate_xff_lines_preserve_proxy_appended_client() -> None:
    request = Request(
        {
            "type": "http",
            "client": ("10.0.0.5", 1234),
            "headers": [
                (b"x-forwarded-for", b"203.0.113.99"),
                (b"x-forwarded-for", b"198.51.100.2"),
            ],
        }
    )
    config = SimpleNamespace(
        trust_proxy_headers=True, trusted_proxy_ips=["10.0.0.0/24"], is_prod=True
    )
    assert get_client_ip(request, config=config) == "198.51.100.2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", [verification.TOKEN_BLOCKED, verification.TOKEN_REVOKED]
)
async def test_replay_checks_live_auth_before_cache_lookup(
    monkeypatch, outcome
) -> None:
    token = tokens.create_access_token({"sub": str(uuid4()), "token_type": "access"})
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/effect",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"idempotency-key", b"already-completed"),
            ],
            "client": ("127.0.0.1", 1234),
        }
    )
    request._body = b"{}"
    status = AsyncMock(return_value=outcome)
    reserve = AsyncMock(return_value=("replay", {"status": 200, "body": ""}))
    monkeypatch.setattr(verification, "token_access_status", status)
    monkeypatch.setattr(idempotency_guard_module, "reserve_key", reserve)
    dependency = idempotency.idempotency_guard(scope="review")
    with pytest.raises(HTTPException) as rejected:
        await dependency(request)
    assert rejected.value.status_code == 401
    status.assert_awaited_once()
    reserve.assert_not_awaited()


def test_binding_with_a_non_pair_alg_is_refused_as_malformed() -> None:
    """Only the keyed x25519 binding exists: a response descriptor naming any
    other algorithm (the removed token-keyed one included) is refused."""
    nonce = rpc_binding.new_nonce()
    headers = {
        rpc_binding.NONCE_HEADER: nonce,
        rpc_binding.BINDING_HEADER: "2;alg=token;kid=k1;svc=provider",
        rpc_binding.MAC_HEADER: "0" * 64,
    }
    reason = rpc_binding.verify(
        settings,
        headers,
        nonce=nonce,
        sent={},
        expect_service="provider",
        status_code=200,
        body=b"ok",
    )
    assert reason == "malformed"


def test_stale_budget_requires_explicit_ordinary_read_class() -> None:
    """ADR-0009 removed the staleness budget outright: no route can declare
    one (stronger than "only class R may")."""
    import sandloom.core.di as di

    assert not hasattr(di, "revocation_freshness")
    assert not hasattr(di, "strict_revocation")


def test_block_cache_failure_never_discloses_subject() -> None:
    subject = str(uuid4())
    response = protocol_response(
        BlockUnavailableError(f"could not block user {subject}: cache unavailable"), 503
    )
    assert subject.encode() not in response.body


@pytest.mark.asyncio
async def test_httpx_fallback_ignores_environment_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8181")
    monkeypatch.setattr(settings, "rpc_transport", "aiohttp")
    client = rpc_client._new_rpc_client("httpx")
    try:
        transport = getattr(client, "_client", client)
        assert transport._trust_env is False
    finally:
        await client.aclose()


def test_rpc_context_rejects_duplicate_keys() -> None:
    raw = (
        b'{"tenant_id":"first","tenant_id":"second","iat":'
        + str(int(time.time())).encode()
        + b"}"
    )
    with pytest.raises(rpc_pair_mac.AuthError) as rejected:
        rpc_pair_mac.decode_context(rpc_wire._b64(raw))
    assert rejected.value.reason == "malformed"


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [True, False])
async def test_httpx_fallback_caps_streamed_body_and_closes(declared_length) -> None:
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * 9

        async def aclose(self):
            self.closed = True

    body = Body()

    def handler(request):
        headers = {"Content-Length": "9"} if declared_length else {}
        return httpx.Response(200, headers=headers, stream=body)

    client = HttpxRpcClient(
        max_response_bytes=8, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(RpcResponseTooLarge):
            await client.get("http://provider/rpc", headers={}, timeout=1)
        assert body.closed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retired_httpx_client_is_closed_and_released(monkeypatch) -> None:
    client = SimpleNamespace(is_closed=False)

    async def close():
        client.is_closed = True

    client.aclose = close
    callbacks = []
    monkeypatch.setattr(rpc_client, "_rpc_client_instance", client)
    monkeypatch.setattr(rpc_client, "_retired_clients", [])
    monkeypatch.setattr(rpc_client, "close_later", callbacks.append)
    rpc_client._discard_pool(client)
    assert rpc_client._retired_clients == [client]
    await callbacks[0]()
    assert client.is_closed
    assert rpc_client._retired_clients == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("keyed_caller")
async def test_describe_failures_are_single_flight_and_negative_cached(
    monkeypatch,
) -> None:
    monkeypatch.setattr(rpc_proxy, "_remote_contracts", {})
    monkeypatch.setattr(rpc_proxy, "_describe_failed", {})

    async def describe(*args, **kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(status_code=503)

    get = AsyncMock(side_effect=describe)
    monkeypatch.setattr(rpc_proxy, "_bound_get", get)
    proxy = rpc.RemoteServiceProxy(
        service_name="demo.v1",
        binding={"base_url": "http://provider", "service_id": "provider"},
        default_timeout_seconds=1,
        client=object(),
    )
    await asyncio.gather(*(proxy._remote_contract("hash") for _ in range(20)))
    assert get.await_count == 1
    await proxy._remote_contract("hash")
    assert get.await_count == 1
    # A newly reported schema hash can retry immediately.
    await proxy._remote_contract("new-hash")
    assert get.await_count == 2


def test_argon_budget_is_divided_between_actual_workers(monkeypatch) -> None:
    monkeypatch.setattr(settings, "password_hash_concurrency", None)
    monkeypatch.setattr(hashing, "_available_cpus", lambda: 4)
    monkeypatch.setenv("SANDLOOM_WORKER_COUNT", "4")
    assert hashing.password_hash_concurrency() == 1


@pytest.mark.asyncio
async def test_error_response_is_sent_before_cache_release(monkeypatch) -> None:
    events = []
    release = AsyncMock(side_effect=lambda armed: events.append("release"))
    monkeypatch.setattr(idempotency_capture, "_release_key", release)
    armed = SimpleNamespace(in_flight_only=False)
    capture = idempotency_capture._ResponseCapture(
        {idempotency_fingerprint._ARMED_ATTR: armed}
    )
    start = {"type": "http.response.start", "status": 400, "headers": []}
    await capture.before_send(start)
    events.append("headers_sent")
    await capture.after_send(start)
    body = {"type": "http.response.body", "body": b"error"}
    await capture.before_send(body)
    events.append("body_sent")
    await capture.after_send(body)
    assert events == ["headers_sent", "body_sent", "release"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("keyed_caller")
async def test_rpc_call_has_overall_deadline(monkeypatch) -> None:
    proxy = rpc.RemoteServiceProxy(
        service_name="demo.deadline.v1",
        binding={"base_url": "http://provider", "service_id": "provider"},
        default_timeout_seconds=0.01,
        client=None,
    )

    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(proxy, "_post", slow)
    with pytest.raises(rpc.RemoteServiceUnavailable) as rejected:
        await asyncio.wait_for(proxy.echo(), timeout=0.5)
    assert rejected.value.code == "rpc_timeout"


def test_pair_mac_key_selection_does_not_depend_on_json_order(monkeypatch) -> None:
    config = SimpleNamespace(rpc_service_dh_current_kids={})
    keys = {"second": "pem-2", "first": "pem-1"}
    monkeypatch.setattr(rpc_pair_mac, "published_keys", lambda cfg, service: keys)
    selected = rpc_pair_mac.addressed_kid(config, "provider")
    keys = dict(reversed(list(keys.items())))
    assert rpc_pair_mac.addressed_kid(config, "provider") == selected
    config.rpc_service_dh_current_kids = {"provider": "first"}
    assert rpc_pair_mac.addressed_kid(config, "provider") == ("first", "pem-1")
    config.rpc_service_dh_current_kids = {"provider": "missing"}
    with pytest.raises(ValueError, match="not published"):
        rpc_pair_mac.addressed_kid(config, "provider")


@pytest.mark.asyncio
async def test_rpc_dedicated_body_cap_is_enforced_before_handler() -> None:
    async def app(scope, receive, send):
        raise AssertionError("oversized RPC reached its handler")

    config = SimpleNamespace(
        rpc_max_request_bytes=4, effective_max_request_body_bytes=100
    )
    middleware = rpc.RpcFastPathMiddleware(app, SimpleNamespace(settings=config))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware), base_url="http://test"
    ) as client:
        response = await client.post("/_sandloom/rpc/demo.v1/run", content=b"12345")
    assert response.status_code == 413


def test_json_security_headers_do_not_break_html_assets() -> None:
    headers = [(b"content-type", b"application/json; charset=utf-8")]
    _add_security_headers(headers)
    assert (
        dict(headers)[b"content-security-policy"]
        == b"default-src 'none'; frame-ancestors 'none'"
    )
    html = [(b"content-type", b"text/html")]
    _add_security_headers(html)
    assert b"content-security-policy" not in dict(html)


def test_authenticated_forbidden_peer_cannot_flood_auth_logs(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(rpc_inbound_auth, "_auth_logged", {})
    for _ in range(10):
        rpc_inbound_auth._log_auth_rejection("caller_forbidden", "known-peer")
    assert (
        len(
            [
                r
                for r in caplog.records
                if getattr(r, "event", None) == "rpc.auth_rejected"
            ]
        )
        == 1
    )


def _armed_scope() -> object:
    """A request scope whose commit-time re-check is armed (a user write)."""
    from sandloom.core.unit_of_work.request import RequestScope

    scope = RequestScope(core=None, policy=None)
    scope.subject = "u-1"
    scope.claims = {"sub": "u-1", "iat_us": 123, "jti": "j"}

    async def _check() -> None:
        return None

    scope.check = _check
    scope.armed = True
    return scope


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["command", None])
@pytest.mark.usefixtures("keyed_caller")
async def test_guarded_request_never_sends_remote_command(monkeypatch, kind) -> None:
    """G-REVOKE-18 reworded (spec §4.2, G-REVOKE-24): under an armed
    commit-time re-check a remote command is no longer refused — the re-check
    is PROPAGATED in the MAC-covered RPC context, so the provider re-checks
    the caller's subject before its own COMMIT (the command never commits
    unchecked: stronger than the old refusal, which removed the capability)."""
    import json

    from sandloom.core.rpc.wire import _CTX_HEADER, _unb64
    from sandloom.core.unit_of_work.request import bind_request_scope

    proxy = rpc.RemoteServiceProxy(
        service_name="guarded.effect.v1",
        binding={"base_url": "http://provider", "service_id": "provider"},
        default_timeout_seconds=1,
    )
    if kind is not None:
        contract = rpc_proxy._RemoteContract(
            hash="h",
            returns={},
            methods={
                "run": rpc_proxy._RemoteMethod(kind=kind, params=(), returns=None)
            },
        )
        monkeypatch.setattr(
            rpc_proxy,
            "_remote_contracts",
            {(proxy._base_url, proxy._service_name): contract},
        )
    else:
        monkeypatch.setattr(rpc_proxy, "_remote_contracts", {})
        monkeypatch.setattr(proxy, "_remote_contract", AsyncMock(return_value=None))

    class Sent(Exception):
        pass

    sent: list[dict[str, str]] = []

    async def post(url, body, headers, signer, **_kwargs):
        sent.append(dict(headers))
        raise Sent

    monkeypatch.setattr(proxy, "_post", post)
    with bind_request_scope(_armed_scope()):  # type: ignore[arg-type]
        with pytest.raises(Sent):
            await proxy.run()
    [headers] = sent
    context = json.loads(_unb64(headers[_CTX_HEADER]))
    assert context["usr"] == {"sub": "u-1", "iat_us": 123, "rc": 1}


@pytest.mark.asyncio
@pytest.mark.usefixtures("keyed_caller")
async def test_guarded_request_can_still_send_described_remote_read(
    monkeypatch,
) -> None:
    from sandloom.core.unit_of_work.request import bind_request_scope

    proxy = rpc.RemoteServiceProxy(
        service_name="guarded.read.v1",
        binding={"base_url": "http://provider", "service_id": "provider"},
        default_timeout_seconds=1,
    )
    contract = rpc_proxy._RemoteContract(
        hash="h",
        returns={},
        methods={"read": rpc_proxy._RemoteMethod(kind="read", params=(), returns=None)},
    )
    monkeypatch.setattr(
        rpc_proxy,
        "_remote_contracts",
        {(proxy._base_url, proxy._service_name): contract},
    )

    class ReadSent(Exception):
        pass

    post = AsyncMock(side_effect=ReadSent)
    monkeypatch.setattr(proxy, "_post", post)
    with bind_request_scope(_armed_scope()):  # type: ignore[arg-type]
        with pytest.raises(ReadSent):
            await proxy.read()
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_rpc_http_boundary_isolates_and_restores_outer_commit_authority(
    monkeypatch,
) -> None:
    from sandloom.core.facade import CoreFacade
    from sandloom.core.http.middlewares import register_middlewares
    from sandloom.core.http.request_authority import RequestAuthorityMiddleware
    from sandloom.core.unit_of_work.request import (
        bind_request_scope,
        current_request_scope,
    )

    seen = []

    from starlette.responses import Response

    async def call(self, request, service_name, method_name):
        seen.append(current_request_scope())
        return Response(content=b"ok", status_code=200)

    monkeypatch.setattr(rpc_server._RpcEndpoints, "serve_call", call)
    app = FastAPI()
    register_middlewares(app)
    core = CoreFacade(app=app)
    rpc.install_service_rpc_routes(app, core)
    wrappers = [item.cls for item in app.user_middleware]
    assert wrappers[0] is RequestAuthorityMiddleware
    assert wrappers.count(RequestAuthorityMiddleware) == 1

    outer = _armed_scope()
    with bind_request_scope(outer):  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post("/_sandloom/rpc/demo.v1/run")).status_code == 200
        assert seen == [None]
        assert current_request_scope() is outer


@pytest.mark.asyncio
async def test_new_http_request_does_not_inherit_outer_issue_floors() -> None:
    from sandloom.core.http.middlewares import register_middlewares

    app = FastAPI()
    register_middlewares(app)

    @app.get("/independent-operation")
    async def independent_operation():
        floors_inherited = bool(marks._issue_floors.get())
        marks._note_issue_floor("child-subject", 43)
        return {"floors_inherited": floors_inherited}

    original = {"outer-subject": 42}
    floors_token = marks._issue_floors.set(original)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/independent-operation")
        assert response.json() == {"floors_inherited": False}
        assert marks._issue_floors.get() == original
    finally:
        marks._issue_floors.reset(floors_token)
