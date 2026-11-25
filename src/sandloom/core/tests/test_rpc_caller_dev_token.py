"""A development token does not cross an internal RPC call into a provider that
would refuse it (review of env-unset-not-dev, L3).

The verifier refuses a ``dev_mint`` token unless its own ``ENVIRONMENT`` was set
to ``dev`` or ``test``. A call a service makes for that token's user carries the
user as caller facts (``usr``); the marker travels with them (``dm``) and the
provider applies the same rule, or a production provider would serve the
subject, role and tenant of a token it would have refused over HTTP.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.responses import Response

from sandloom.core.claims import DEV_MINT_CLAIM
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import Settings, settings
from sandloom.core.contract_testing import rpc_request_headers, rpc_settings
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import context as rpc_context
from sandloom.core.rpc import install_service_rpc_routes
from sandloom.core.rpc import server as rpc_server
from sandloom.core.rpc.envelope import _ok_response
from sandloom.core.rpc.wire import _pack, _unpack
from sandloom.core.unit_of_work.request import RequestScope, bind_request_scope

_SVC = "svc"
_CONTRACT = "demo.devtoken.v1"
_PATH = f"/_sandloom/rpc/{_CONTRACT}/ping"
_TARGET = f"{_CONTRACT}/ping"


def _scope(*, dev_mint: bool) -> RequestScope:
    """The request scope of an end user whose token is (not) a dev token."""
    scope = RequestScope(core=None, policy=None)
    scope.subject = str(uuid4())
    scope.claims = {"sub": scope.subject, "iat_us": 1}
    if dev_mint:
        scope.claims[DEV_MINT_CLAIM] = True
    return scope


def _settings(environment: str | None, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings as a process with ``ENVIRONMENT=<environment>`` reads them
    (``None``: unset); ``prod`` is a copy, as its validators need a full
    production configuration none of these rows is about."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    if environment in ("dev", "test"):
        monkeypatch.setenv("ENVIRONMENT", environment)
    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:5433/d"
    )
    if environment == "prod":
        return cfg.model_copy(update={"environment": Environment.prod})
    return cfg


# --- the caller facts carry the marker -----------------------------------------
def test_a_dev_token_user_crosses_the_call_with_the_marker() -> None:
    facts = rpc_context._caller_facts(_scope(dev_mint=True))
    assert facts is not None and facts["dm"] == 1
    assert facts["iat_us"] == 1  # the other token facts are unchanged


def test_an_ordinary_token_user_crosses_without_it() -> None:
    facts = rpc_context._caller_facts(_scope(dev_mint=False))
    assert facts is not None and "dm" not in facts


def test_the_marker_is_in_the_signed_context_a_call_sends() -> None:
    with bind_request_scope(_scope(dev_mint=True)):
        payload = rpc_context._context_payload(None, _CONTRACT)
    assert payload["usr"]["dm"] == 1


# --- a second hop keeps it (closure review, L) ------------------------------------
def _second_hop_facts(first: dict[str, Any]) -> dict[str, Any]:
    """The caller facts a provider sends on when the call it serves (``first``,
    what the first caller sent) makes a call of its own."""
    served = rpc_context._caller_scope(None, first)
    facts = rpc_context._caller_facts(served)
    assert facts is not None
    return facts


def test_the_marker_survives_a_second_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """dev caller -> dev provider A -> prod provider B: A rebuilds the request
    scope from the facts, and B must still see a development token's user."""
    first = rpc_context._caller_facts(_scope(dev_mint=True))
    assert first is not None
    second = _second_hop_facts(first)
    assert second["dm"] == 1 and second["sub"] == first["sub"]
    prod = _settings("prod", monkeypatch)
    assert rpc_context.caller_facts_problem(prod, second) is not None
    # one more hop changes nothing
    assert _second_hop_facts(second)["dm"] == 1


def test_a_second_hop_adds_no_marker_to_an_ordinary_token_user() -> None:
    first = rpc_context._caller_facts(_scope(dev_mint=False))
    assert first is not None
    assert "dm" not in _second_hop_facts(first)


# --- the provider applies the verifier's rule ------------------------------------
@pytest.mark.parametrize(
    ("environment", "refused"),
    [(None, True), ("dev", False), ("test", False), ("prod", True)],
)
def test_a_provider_judges_the_marker_by_its_own_environment(
    environment: str | None, refused: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _settings(environment, monkeypatch)
    problem = rpc_context.caller_facts_problem(cfg, {"sub": "u", "dm": 1})
    assert (problem is not None) is refused


@pytest.mark.parametrize("environment", [None, "dev", "test", "prod"])
def test_a_call_without_the_marker_is_never_refused_for_it(
    environment: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _settings(environment, monkeypatch)
    assert rpc_context.caller_facts_problem(cfg, {"sub": "u"}) is None
    assert rpc_context.caller_facts_problem(cfg, None) is None  # a system call


# --- through the provider's HTTP surface -------------------------------------------
@pytest.fixture(autouse=True)
def _keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in rpc_settings(_SVC, allowed_calls={_SVC: [_CONTRACT]}).items():
        monkeypatch.setattr(settings, name, value)


def _provider() -> FastAPI:
    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core

    class Svc:
        @classmethod
        @contract_method
        async def ping(cls) -> str:
            return "pong"

    core.register_service(_CONTRACT, Svc)
    install_service_rpc_routes(app, core)
    return app


async def _call(
    monkeypatch: pytest.MonkeyPatch, *, dev_mint: bool
) -> tuple[Any, list[Any]]:
    """One signed call made for an end user, against a provider that records
    the caller facts of every call it serves."""
    served: list[Any] = []

    async def handle(**kwargs: Any) -> Response:
        served.append(kwargs["caller"])
        return _ok_response({"ok": True, "result": "pong"})

    monkeypatch.setattr(rpc_server, "_handle_call", handle)
    body = _pack({})
    with bind_request_scope(_scope(dev_mint=dev_mint)):
        headers = rpc_request_headers(settings, _TARGET, body)
    async with AsyncClient(
        transport=ASGITransport(app=_provider()), base_url="http://test"
    ) as client:
        response = await client.post(_PATH, content=body, headers=headers)
    return response, served


@pytest.mark.asyncio
async def test_a_prod_provider_refuses_a_call_made_for_a_dev_token_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", Environment.prod)
    response, served = await _call(monkeypatch, dev_mint=True)
    assert response.status_code == 401
    error = _unpack(response.content)["error"]
    assert error["code"] == "rpc_caller_dev_token"
    assert "ENVIRONMENT=prod" in error["message"]
    assert served == []  # the provider never ran the call


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [Environment.dev, Environment.test])
async def test_a_dev_provider_serves_the_same_call(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "environment", environment)
    response, served = await _call(monkeypatch, dev_mint=True)
    assert response.status_code == 200
    assert len(served) == 1 and served[0]["dm"] == 1


@pytest.mark.asyncio
async def test_a_prod_provider_serves_an_ordinary_token_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", Environment.prod)
    response, served = await _call(monkeypatch, dev_mint=False)
    assert response.status_code == 200
    assert len(served) == 1 and "dm" not in served[0]
