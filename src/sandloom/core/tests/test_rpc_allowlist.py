"""Per-contract authorization and signing-key configuration of internal RPC.

Every request is pair-MAC authenticated (``test_rpc_pair_mac.py``); a
verified caller is then authorized against ``RPC_ALLOWED_CALLS`` — deny by
default: allowed caller -> 200, unlisted caller / method -> 403 (503
``rpc_caller_forbidden`` at the calling proxy), no credential -> 404. A
caller's own configuration is checked at startup (its X25519 key, pinned
bindings, its allowlist entry, its Ed25519 signing key).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from sandloom.core import rpc, rpc_binding, rpc_identity
from sandloom.core.app_factory import ConfigurationError, enforce_configuration
from sandloom.core.cli import keys as keys_cli
from sandloom.core.config.settings import Settings, settings
from sandloom.core.contract_testing import (
    rpc_env,
    rpc_keys,
    rpc_request_headers,
    rpc_settings,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import RemoteServiceProxy, install_service_rpc_routes
from sandloom.core.rpc.wire import _pack, _unpack
from sandloom.core.signing_keys import generate_private_key, public_pem

SERVICE = "demo.scope.v1"
#: Who may call what (identical in every service).
ALLOWED = {
    "billing": {SERVICE: ["whose_tenant"]},
    "identity": ["other.contract.v1"],
}
PEERS = ["tenancy", "billing", "identity"]

#: A configuration is refused at load (``Settings``) or at boot
#: (:func:`sandloom.core.app_factory.enforce_configuration`).
_REFUSED = (ValidationError, ConfigurationError)


# -- provider app -------------------------------------------------------------


class _Session:
    info: dict[str, Any] = {}


class _UoW:
    session: Any = None

    async def __aenter__(self) -> _UoW:
        self.session = _Session()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.session = None


class _Database:
    def uow_factory(self) -> Callable[[], _UoW]:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _app() -> FastAPI:
    from sandloom.core.di import current_tenant_id_optional

    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]

    class Svc:
        @classmethod
        @contract_method
        async def whose_tenant(cls) -> str:
            tid = current_tenant_id_optional()
            return str(tid) if tid else "none"

        @classmethod
        @contract_method
        async def admin_only(cls) -> str:
            return "admin"

    core.register_service(SERVICE, Svc)
    install_service_rpc_routes(app, core)
    return app


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """tenancy serves SERVICE; billing may call whose_tenant only."""
    for name, value in rpc_settings(
        "tenancy", peers=PEERS, allowed_calls=ALLOWED
    ).items():
        monkeypatch.setattr(settings, name, value)
    return _app()


def _caller(service: str) -> Any:
    return settings.model_copy(
        update=rpc_settings(service, peers=PEERS, allowed_calls=ALLOWED)
    )


async def _post(app: FastAPI, caller: str, method: str, tenant: Any = None) -> Any:
    from sandloom.core.di import bind_tenant

    body = _pack({})
    target = f"{SERVICE}/{method}"
    if tenant is None:
        headers = rpc_request_headers(_caller(caller), target, body, provider="tenancy")
    else:
        with bind_tenant(tenant):
            headers = rpc_request_headers(
                _caller(caller), target, body, provider="tenancy"
            )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://tenancy"
    ) as client:
        return await client.post(
            f"/_sandloom/rpc/{target}", content=body, headers=headers
        )


def _code(response: Any) -> str:
    return str(_unpack(response.content)["error"]["code"])


# -- allow / deny ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_caller_succeeds(provider: FastAPI) -> None:
    response = await _post(provider, "billing", "whose_tenant")
    assert response.status_code == 200
    assert _unpack(response.content) == {"ok": True, "result": "none"}


@pytest.mark.asyncio
async def test_allowed_caller_propagates_the_tenant(provider: FastAPI) -> None:
    tenant = uuid4()
    response = await _post(provider, "billing", "whose_tenant", tenant)
    assert _unpack(response.content)["result"] == str(tenant)


@pytest.mark.asyncio
async def test_unlisted_method_is_forbidden(provider: FastAPI) -> None:
    response = await _post(provider, "billing", "admin_only")
    assert response.status_code == 403
    assert _code(response) == "rpc_caller_forbidden"


@pytest.mark.asyncio
async def test_unlisted_caller_is_forbidden(provider: FastAPI) -> None:
    """identity is a known, verified service — but not allowed this contract."""
    response = await _post(provider, "identity", "whose_tenant")
    assert response.status_code == 403
    assert _code(response) == "rpc_caller_forbidden"


def _proxy(app: FastAPI, caller: str) -> RemoteServiceProxy:
    return RemoteServiceProxy(
        service_name=SERVICE,
        binding={"base_url": "http://tenancy", "service_id": "tenancy"},
        default_timeout_seconds=5.0,
        client=AsyncClient(transport=ASGITransport(app=app), base_url="http://tenancy"),
        settings=_caller(caller),
    )


@pytest.mark.asyncio
async def test_proxy_of_an_allowed_caller_calls_and_describes(
    provider: FastAPI,
) -> None:
    proxy = _proxy(provider, "billing")
    assert await proxy.whose_tenant() == "none"
    # A provider's 403 is a deployment problem, not the end user's: the
    # caller surfaces it as 503 (never as the provider's 401 / 403).
    with pytest.raises(rpc.RemoteServiceUnavailable) as denied:
        await proxy.admin_only()
    assert denied.value.status_code == 503
    assert denied.value.code == "rpc_caller_forbidden"
    # The describe GET (contract schema) is authenticated and authorized too.
    async with AsyncClient(
        transport=ASGITransport(app=provider), base_url="http://tenancy"
    ) as client:
        ok = await client.get(
            f"/_sandloom/rpc/{SERVICE}",
            headers=rpc_request_headers(
                _caller("billing"), SERVICE, http_method="GET", provider="tenancy"
            ),
        )
        bare = await client.get(
            f"/_sandloom/rpc/{SERVICE}", headers={"X-Internal-Token": "x" * 40}
        )
    assert ok.status_code == 200
    assert bare.status_code == 404


@pytest.mark.asyncio
async def test_proxy_of_an_unlisted_caller_gets_503(provider: FastAPI) -> None:
    with pytest.raises(rpc.RemoteServiceUnavailable) as denied:
        await _proxy(provider, "identity").whose_tenant()
    assert denied.value.status_code == 503
    assert denied.value.code == "rpc_caller_forbidden"


# -- configuration ------------------------------------------------------------------

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://h:6379/0",
    "BROKER_URL": "redis://h:6379/1",
    "JWT_SECRET": "x" * 40,
    "JWT_PRIVATE_KEY": "",
}


def _settings(**env: Any) -> Settings:
    cfg = Settings(_env_file=None, **{**_BASE_ENV, **env})  # type: ignore[call-arg]
    enforce_configuration(cfg)  # the checks a process runs at boot
    return cfg


def _billing_env(**extra: Any) -> dict[str, Any]:
    return {
        **rpc_env(
            "billing",
            peers=["tenancy"],
            allowed_calls={"billing": ["tenancy.membership.v1"]},
        ),
        "SERVICE_BINDINGS": {
            "tenancy.membership.v1": {"base_url": "http://t", "service_id": "tenancy"}
        },
        **extra,
    }


def test_a_consistent_caller_configuration_validates() -> None:
    cfg = _settings(**_billing_env())
    assert rpc_identity.signing_identity(cfg) is not None
    assert rpc_binding.own_dh_key(cfg) is not None
    # No INTERNAL_API_TOKEN anywhere: it only guards the ops endpoints.
    assert cfg.internal_api_token is None


def test_a_monolith_needs_no_rpc_keys() -> None:
    cfg = _settings()
    assert cfg.service_bindings == {}
    assert rpc_binding.own_dh_key(cfg) is None


def test_every_bound_contract_must_be_allowed_for_the_caller() -> None:
    with pytest.raises(_REFUSED, match="does not allow"):
        _settings(
            **_billing_env(
                SERVICE_BINDINGS={
                    "tenancy.membership.v1": {
                        "base_url": "http://t",
                        "service_id": "tenancy",
                    },
                    "billing.extra.v1": {
                        "base_url": "http://x",
                        "service_id": "tenancy",
                    },
                }
            )
        )


def test_caller_with_bindings_needs_its_own_x25519_key() -> None:
    with pytest.raises(_REFUSED, match="RPC_DH_PRIVATE_KEY"):
        _settings(**_billing_env(RPC_DH_PRIVATE_KEY=""))


def test_every_binding_must_pin_its_provider() -> None:
    with pytest.raises(_REFUSED, match="service_id"):
        _settings(
            **_billing_env(
                SERVICE_BINDINGS={"tenancy.membership.v1": {"base_url": "http://t"}}
            )
        )


def test_a_binding_carries_no_token() -> None:
    with pytest.raises(_REFUSED, match="unknown key"):
        _settings(
            **_billing_env(
                SERVICE_BINDINGS={
                    "tenancy.membership.v1": {
                        "base_url": "http://t",
                        "service_id": "tenancy",
                        "internal_token": "t" * 40,
                    }
                }
            )
        )


def test_signed_commands_need_the_callers_ed25519_key() -> None:
    with pytest.raises(_REFUSED, match="RPC_PRIVATE_KEY"):
        _settings(
            **_billing_env(RPC_PRIVATE_KEY="", RPC_COMMANDS_REQUIRE_SIGNATURE="on")
        )


def test_private_key_must_be_the_published_one() -> None:
    rogue = public_pem(generate_private_key("EdDSA"))
    with pytest.raises(_REFUSED, match="not the public half"):
        _settings(**_billing_env(RPC_SERVICE_KEYS={"billing": {"k1": rogue}}))
    with pytest.raises(_REFUSED, match="missing"):
        _settings(**_billing_env(RPC_KEY_ID="b7"))


def test_service_keys_must_be_ed25519() -> None:
    rsa_pub = public_pem(generate_private_key("RS256"))
    with pytest.raises(_REFUSED, match="Ed25519"):
        _settings(**_billing_env(RPC_SERVICE_KEYS={"billing": {"k1": rsa_pub}}))
    with pytest.raises(_REFUSED, match="RPC_ALLOWED_CALLS"):
        _settings(**_billing_env(RPC_ALLOWED_CALLS={"billing": {"x.v1": 3}}))


def test_allowlist_is_derived_from_service_bindings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert rpc_identity.allowlist_from_bindings(
        {
            "identity": {"tenancy.claims.v1": {}, "billing.claims.v1": {}},
            "tenancy": {},
            "billing": {"tenancy.membership.v1": {}},
        }
    ) == {
        "billing": ["tenancy.membership.v1"],
        "identity": ["billing.claims.v1", "tenancy.claims.v1"],
    }
    assert (
        keys_cli.main(
            [
                "allowlist",
                "--bindings",
                'billing={"tenancy.membership.v1": {"base_url": "http://t"}}',
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"billing": ["tenancy.membership.v1"]}


def test_keys_generate_rpc_validates(capsys: pytest.CaptureFixture[str]) -> None:
    assert keys_cli.main(["generate", "--purpose", "rpc", "--service", "billing"]) == 0
    out = capsys.readouterr().out
    # sandloom: secret-fixture — PEM marker lookup; keys generated during the test.
    begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    first = out.index(end) + len(end)
    private = out[out.index(begin) : first]
    dh_private = out[out.index(begin, first) : out.index(end, first) + len(end)]
    lines = out.strip().splitlines()
    entry = json.loads(
        lines[lines.index(next(x for x in lines if "RPC_SERVICE_KEYS entry" in x)) + 1]
    )
    dh_entry = json.loads(lines[-1])
    (kid,) = entry["billing"]
    assert dh_entry["billing"][kid] != entry["billing"][kid]  # separate keys
    tenancy = rpc_keys("tenancy")
    cfg = _settings(
        **_billing_env(
            RPC_PRIVATE_KEY=private,
            RPC_KEY_ID=kid,
            RPC_SERVICE_KEYS=json.dumps(entry),
            RPC_DH_PRIVATE_KEY=dh_private,
            RPC_DH_KEY_ID=kid,
            RPC_SERVICE_DH_KEYS=json.dumps(
                {**dh_entry, "tenancy": {tenancy.kid: tenancy.dh_public_key}}
            ),
        )
    )
    identity = rpc_identity.signing_identity(cfg)
    own = rpc_binding.own_dh_key(cfg)
    assert identity is not None and identity.kid == kid
    assert own is not None and own.kid == kid
