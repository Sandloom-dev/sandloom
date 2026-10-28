"""Keyed response binding: the response is authenticated, not only bound.

The provider binds each response to a VERIFIED request with a per-PAIR key:
X25519 ECDH (``cryptography``) between the two services' DEDICATED
key-agreement keys (never their Ed25519 signing keys) + HKDF. The caller
accepts only such a keyed binding from the binding's pinned ``service_id`` —
so a party WITHOUT the pair key (an on-path forger, another registered
service, a responder with no key) cannot get a response accepted. Covers
rotation (by kid), missing / mismatched X25519 keys (the request then never
authenticates: a refusal), startup validation and both transports (a
scripted socket peer).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from pydantic import ValidationError

from sandloom.core import rpc, rpc_binding
from sandloom.core.app_factory import ConfigurationError, enforce_configuration
from sandloom.core.config.settings import Settings, settings
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import RemoteServiceProxy, RemoteServiceUnavailable
from sandloom.core.signing_keys import (
    KeyConfigError,
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)
from sandloom.core.tests import test_rpc_response_binding as _base
from sandloom.core.tests.test_rpc_response_binding import (
    TRANSPORTS,
    BindingPeer,
    Req,
    binding_peer,
    result_body,
)

# The scripted socket peer's fixtures: the shared client of each transport
# and no retry delay.
shared_client = _base.shared_client
_no_retry_delay = _base._no_retry_delay

SERVICE = "demo.echo.v1"

#: A configuration is refused at load (``Settings``) or at boot
#: (:func:`sandloom.core.app_factory.enforce_configuration`).
_REFUSED = (ValidationError, ConfigurationError)


def _pair() -> tuple[str, str]:
    key = generate_private_key("EdDSA")
    return private_pem(key), public_pem(key)


def _dh_pair() -> tuple[str, str]:
    key = generate_x25519_key()
    return x25519_private_pem(key), x25519_public_pem(key)


#: Ed25519 signing keys (callers only: billing; they sign commands).
KEYS = {name: _pair() for name in ("billing", "billing-next")}
SERVICE_KEYS = {"billing": {"b1": KEYS["billing"][1], "b2": KEYS["billing-next"][1]}}
#: DEDICATED X25519 keys (every service, providers included).
DH = {
    name: _dh_pair()
    for name in ("billing", "billing-next", "tenancy", "tenancy-next", "rogue")
}
SERVICE_DH_KEYS = {
    "billing": {"b1": DH["billing"][1], "b2": DH["billing-next"][1]},
    "tenancy": {"t1": DH["tenancy"][1], "t2": DH["tenancy-next"][1]},
    "rogue": {"r1": DH["rogue"][1]},
}


def _rejected(reason: str) -> float:
    value = REGISTRY.get_sample_value(
        "sandloom_rpc_response_binding_rejected_total", {"reason": reason}
    )
    return float(value or 0.0)


# --------------------------------------------------------------------------
# the key agreement
# --------------------------------------------------------------------------


def test_pair_keys_agree_per_direction_and_pair() -> None:
    info = rpc_binding.pair_info("billing", "b1", "tenancy", "t1")
    caller_side = rpc_binding.pair_key(DH["billing"][0], DH["tenancy"][1], info)
    provider_side = rpc_binding.pair_key(DH["tenancy"][0], DH["billing"][1], info)
    assert caller_side == provider_side and len(caller_side) == 32
    # Another service, or the other direction, derives a different key.
    assert rpc_binding.pair_key(DH["rogue"][0], DH["billing"][1], info) != (caller_side)
    reverse = rpc_binding.pair_info("tenancy", "t1", "billing", "b1")
    assert rpc_binding.pair_key(DH["billing"][0], DH["tenancy"][1], reverse) != (
        caller_side
    )


def test_no_custom_curve_code_and_no_signing_key_reuse() -> None:
    """The binding module holds no field arithmetic / Ed25519 conversion, and
    a service's X25519 key is not derived from its signing key."""
    import inspect

    source = inspect.getsource(rpc_binding)
    for marker in ("2**255", "ed25519", "birational", "private_bytes_raw"):
        assert marker not in source, marker
    with pytest.raises(KeyConfigError):  # an Ed25519 PEM is not an X25519 key
        rpc_binding.pair_key(KEYS["billing"][0], DH["tenancy"][1], b"x")


def test_the_mac_covers_the_request_auth(caller: Any) -> None:
    """A provider's answer to a REPLAYED request (re-sent by an on-path party
    under the victim's new nonce) never verifies for the new request."""
    nonce = rpc_binding.new_nonce()
    key = _pair_key_of("tenancy", ("tenancy", "t1"))
    replayed = {rpc_binding.REQ_MAC_HEADER: "mac-of-an-earlier-request"}
    headers = dict(
        rpc_binding.response_headers(
            nonce=nonce,
            key=key,
            req_auth=rpc_binding.request_auth(replayed),
            status_code=200,
            body=b"x",
        )
    )
    sent = {rpc_binding.REQ_MAC_HEADER: "mac-of-this-one"}

    def verdict() -> str | None:
        return rpc_binding.verify(
            settings,
            headers,
            nonce=nonce,
            sent=sent,
            expect_service="tenancy",
            status_code=200,
            body=b"x",
        )

    assert verdict() == "mac_mismatch"
    sent[rpc_binding.REQ_MAC_HEADER] = "mac-of-an-earlier-request"
    assert verdict() is None


# --------------------------------------------------------------------------
# a real provider, the proxy as caller
# --------------------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


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


def _provider_app(cfg: Any) -> FastAPI:
    app = FastAPI()
    core = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = core
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]

    class Echo:
        @classmethod
        @contract_method
        async def echo(cls, value: str) -> str:
            return f"r-{value}"

    core.register_service(SERVICE, Echo)
    rpc.install_service_rpc_routes(app, core)
    return app


@pytest.fixture
def caller(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The process settings = the CALLER: billing (b1)."""
    monkeypatch.setattr(settings, "rpc_service_keys", SERVICE_KEYS)
    monkeypatch.setattr(settings, "rpc_allowed_calls", {"billing": [SERVICE]})
    monkeypatch.setattr(settings, "rpc_service_id", "billing")
    monkeypatch.setattr(settings, "rpc_private_key", KEYS["billing"][0])
    monkeypatch.setattr(settings, "rpc_key_id", "b1")
    monkeypatch.setattr(settings, "rpc_service_dh_keys", SERVICE_DH_KEYS)
    monkeypatch.setattr(settings, "rpc_dh_private_key", DH["billing"][0])
    monkeypatch.setattr(settings, "rpc_dh_key_id", "b1")
    monkeypatch.setattr(settings, "rpc_dh_extra_private_keys", {})
    # tenancy publishes t1 and t2; callers address t1 until it rotates.
    monkeypatch.setattr(settings, "rpc_service_dh_current_kids", {"tenancy": "t1"})
    return settings


def _provider_cfg(
    *, pair: str | None = "tenancy", kid: str = "t1", **extra: Any
) -> Any:
    """tenancy: a PROVIDER-ONLY service — no signing key, only its X25519 key."""
    return settings.model_copy(
        update={
            "rpc_service_id": "tenancy",
            "rpc_private_key": None,
            "rpc_key_id": None,
            "rpc_dh_private_key": DH[pair][0] if pair else None,
            "rpc_dh_key_id": kid if pair else None,
            **extra,
        }
    )


def _proxy(
    app: FastAPI, seen: list[Any], service_id: str | None = "tenancy"
) -> RemoteServiceProxy:
    async def record(response: Any) -> None:
        seen.append(response)

    binding: dict[str, object] = {"base_url": "http://tenancy"}
    if service_id:
        binding["service_id"] = service_id
    return RemoteServiceProxy(
        service_name=SERVICE,
        binding=binding,
        default_timeout_seconds=5.0,
        client=AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://tenancy",
            event_hooks={"response": [record]},
        ),
    )


@pytest.mark.asyncio
async def test_the_caller_accepts_the_pinned_providers_pair_keyed_response(
    caller: Any,
) -> None:
    seen: list[Any] = []
    proxy = _proxy(_provider_app(_provider_cfg()), seen)
    before = sum(_rejected(r) for r in rpc_binding.REJECT_REASONS)
    assert await proxy.echo("v") == "r-v"
    assert sum(_rejected(r) for r in rpc_binding.REJECT_REASONS) == before
    # The call, then the (bound, keyed) describe GET it triggered.
    assert len(seen) >= 1
    for response in seen:
        assert response.headers[rpc_binding.BINDING_HEADER] == (
            "2;alg=x25519;kid=t1;svc=tenancy"
        )
    # The pair key never travels: no token, no key material on the wire.
    assert "X-Internal-Token" not in seen[0].request.headers


@pytest.mark.asyncio
async def test_a_403_of_a_verified_caller_is_keyed_too(caller: Any) -> None:
    app = _provider_app(_provider_cfg(rpc_allowed_calls={"billing": ["other.v1"]}))
    seen: list[Any] = []
    with pytest.raises(RemoteServiceUnavailable) as denied:
        await _proxy(app, seen).echo("v")
    assert denied.value.code == "rpc_caller_forbidden"  # the provider's 403
    assert seen[0].headers[rpc_binding.BINDING_HEADER].startswith("2;alg=x25519")


@pytest.mark.asyncio
async def test_an_unpinned_binding_never_sends_a_call(caller: Any) -> None:
    seen: list[Any] = []
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(_provider_app(_provider_cfg()), seen, service_id=None).echo("v")
    assert exc.value.code == "rpc_auth_unavailable"
    assert seen == []


@pytest.mark.asyncio
async def test_pinned_to_another_service_is_refused(caller: Any) -> None:
    """The request is MACed for rogue (aud=rogue): tenancy refuses it."""
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(_provider_app(_provider_cfg()), [], "rogue").echo("v")
    assert exc.value.code == "rpc_auth_invalid"


# -- rotation -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_key_rotation_overlap_and_after(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider switches its X25519 key to t2 while both kids are
    published: callers address t2 (the current kid) and accept its keyed
    responses. A caller whose RPC_SERVICE_DH_KEYS lacks t2 (rotation step 1
    skipped) still addresses t1, which the provider no longer holds: the
    call is refused, never answered with an unverifiable key."""
    monkeypatch.setattr(settings, "rpc_service_dh_current_kids", {"tenancy": "t2"})
    app = _provider_app(_provider_cfg(pair="tenancy-next", kid="t2"))
    seen: list[Any] = []
    assert await _proxy(app, seen).echo("v") == "r-v"
    assert "kid=t2" in seen[0].headers[rpc_binding.BINDING_HEADER]
    caller_view = {k: dict(v) for k, v in SERVICE_DH_KEYS.items()}
    del caller_view["tenancy"]["t2"]
    # The provider keeps ITS (full) copy of the map.
    app = _provider_app(
        _provider_cfg(
            pair="tenancy-next", kid="t2", rpc_service_dh_keys=SERVICE_DH_KEYS
        )
    )
    monkeypatch.setattr(settings, "rpc_service_dh_keys", caller_view)
    monkeypatch.setattr(settings, "rpc_service_dh_current_kids", {})
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(app, []).echo("v")
    assert exc.value.code == "rpc_auth_invalid"


@pytest.mark.asyncio
async def test_caller_key_rotation(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both of the caller's keys rotate (signing b2, X25519 b2): the provider
    keys the response with the X25519 kid its verified MAC named."""
    app = _provider_app(_provider_cfg())
    monkeypatch.setattr(settings, "rpc_private_key", KEYS["billing-next"][0])
    monkeypatch.setattr(settings, "rpc_key_id", "b2")
    monkeypatch.setattr(settings, "rpc_dh_private_key", DH["billing-next"][0])
    monkeypatch.setattr(settings, "rpc_dh_key_id", "b2")
    assert await _proxy(app, []).echo("rotated") == "r-rotated"


# -- missing / mismatched X25519 keys ---------------------------------------------


@pytest.mark.asyncio
async def test_provider_without_an_x25519_key_refuses_unkeyed(caller: Any) -> None:
    """A provider with no X25519 key cannot verify (nor key) anything: its
    refusal echoes the nonce alone and the caller surfaces it as 503."""
    seen: list[Any] = []
    before = _rejected("unkeyed")
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(_provider_app(_provider_cfg(pair=None)), seen).echo("v")
    assert exc.value.code == "rpc_auth_invalid"
    assert _rejected("unkeyed") == before + 1  # never retried
    assert rpc_binding.BINDING_HEADER not in seen[0].headers


@pytest.mark.asyncio
async def test_caller_without_an_x25519_key_never_sends_a_call(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rpc_dh_private_key", None)
    monkeypatch.setattr(settings, "rpc_dh_key_id", None)
    seen: list[Any] = []
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(_provider_app(_provider_cfg()), seen).echo("v")
    assert exc.value.code == "rpc_auth_unavailable"
    assert seen == []


@pytest.mark.asyncio
async def test_provider_key_not_matching_its_published_kid_is_refused(
    caller: Any,
) -> None:
    """The provider holds tenancy-next's private key under kid t1 (whose
    published key is tenancy's): it derives another pair key, so the
    caller's MAC never verifies there."""
    app = _provider_app(_provider_cfg(pair="tenancy-next", kid="t1"))
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(app, []).echo("v")
    assert exc.value.code == "rpc_auth_invalid"


@pytest.mark.asyncio
async def test_caller_unknown_to_the_provider_is_refused(caller: Any) -> None:
    """The provider does not know billing's X25519 kid: no pair key (never a
    guess), so the request does not authenticate."""
    partial = {k: v for k, v in SERVICE_DH_KEYS.items() if k != "billing"}
    app = _provider_app(_provider_cfg(rpc_service_dh_keys=partial))
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(app, []).echo("v")
    assert exc.value.code == "rpc_auth_invalid"


# --------------------------------------------------------------------------
# forgers on a real socket, both transports
# --------------------------------------------------------------------------


def _forged(req: Req, key: rpc_binding.ProviderKey | None) -> bytes:
    body = result_body(req.expected)
    head = b"HTTP/1.1 200 OK\r\ncontent-type: application/msgpack\r\n"
    assert req.nonce is not None
    for name, value in rpc_binding.response_headers(
        nonce=req.nonce,
        key=key,
        req_auth=rpc_binding.request_auth(
            {
                rpc_binding.SVC_SIG_HEADER: req.headers.get(
                    "x-sandloom-rpc-service-signature"
                ),
                rpc_binding.REQ_MAC_HEADER: req.headers.get("x-sandloom-rpc-auth"),
            }
        ),
        status_code=200,
        body=body,
    ):
        head += f"{name}: {value}\r\n".encode()
    return head + b"content-length: %d\r\n\r\n" % len(body) + body


def _pair_key_of(signer: str, announce: tuple[str, str]) -> Any:
    """A v2 key from ``signer``'s X25519 key while announcing (svc, kid)."""
    info = rpc_binding.pair_info("billing", "b1", *announce)
    return rpc_binding.ProviderKey(
        rpc_binding.descriptor(announce[1], announce[0]),
        rpc_binding.pair_key(DH[signer][0], DH["billing"][1], info),
    )


FORGERS: dict[str, tuple[Callable[[], Any], str]] = {
    # Knows every public key, holds none of the pair's private keys: no MAC.
    "no_key": (lambda: None, "unkeyed"),
    # A registered service keying with ITS OWN pair key, honestly announced.
    "other_service": (
        lambda: _pair_key_of("rogue", ("rogue", "r1")),
        "service_mismatch",
    ),
    # The same, claiming to be tenancy (its key is not tenancy's pair key).
    "impersonator": (
        lambda: _pair_key_of("rogue", ("tenancy", "t1")),
        "mac_mismatch",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_client", TRANSPORTS, indirect=True)
@pytest.mark.parametrize("forger", sorted(FORGERS))
async def test_a_forged_response_without_the_pair_key_is_rejected(
    shared_client: str, caller: Any, forger: str
) -> None:
    make_key, reason = FORGERS[forger]

    async def forge_first(peer: BindingPeer, req: Req) -> bool:
        if req.n == 1:
            req.writer.write(_forged(req, make_key()))
        else:  # the retry: the genuine provider's answer
            genuine = _pair_key_of("tenancy", ("tenancy", "t1"))
            req.writer.write(_forged(req, genuine))
        return True

    async with binding_peer(forge_first) as p:
        proxy = RemoteServiceProxy(
            service_name=SERVICE,
            binding={"base_url": p.url, "service_id": "tenancy"},
            default_timeout_seconds=2.0,
        )
        before = _rejected(reason)
        assert await proxy.echo("x") == "r-x"
        assert _rejected(reason) == before + 1
        # The request carried the caller's pair MAC, never a token.
        first = p.requests[0]
        assert first.headers.get("x-sandloom-rpc-auth", "").startswith(
            "v=1;caller=billing;"
        )
        assert "x-internal-token" not in first.headers


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://h:6379/0",
    "BROKER_URL": "redis://h:6379/1",
    "JWT_SECRET": "x" * 40,
    "JWT_PRIVATE_KEY": "",
}


def _settings(binding: dict[str, Any], **extra: Any) -> Settings:
    env = {
        **_BASE_ENV,
        "RPC_SERVICE_ID": "billing",
        "RPC_PRIVATE_KEY": KEYS["billing"][0],
        "RPC_KEY_ID": "b1",
        "RPC_SERVICE_KEYS": SERVICE_KEYS,
        "RPC_DH_PRIVATE_KEY": DH["billing"][0],
        "RPC_DH_KEY_ID": "b1",
        "RPC_SERVICE_DH_KEYS": SERVICE_DH_KEYS,
        "RPC_ALLOWED_CALLS": {"billing": ["tenancy.membership.v1"]},
        "SERVICE_BINDINGS": {"tenancy.membership.v1": binding},
        **extra,
    }
    cfg = Settings(_env_file=None, **env)
    enforce_configuration(cfg)  # the checks a process runs at boot
    return cfg


PINNED = {"base_url": "http://t", "service_id": "tenancy"}


def test_every_binding_is_pinned_to_a_keyed_provider() -> None:
    with pytest.raises(_REFUSED, match="service_id"):
        _settings({"base_url": "http://t"})
    with pytest.raises(_REFUSED, match="no key for 'nobody'"):
        _settings({"base_url": "http://t", "service_id": "nobody"})
    cfg = _settings(PINNED)
    assert cfg.service_bindings["tenancy.membership.v1"]["service_id"] == "tenancy"
    with pytest.raises(_REFUSED, match="service_id must be"):
        _settings({"base_url": "http://t", "service_id": ""})


def test_a_caller_refuses_to_start_without_its_x25519_key() -> None:
    with pytest.raises(_REFUSED, match="RPC_DH_PRIVATE_KEY"):
        _settings(PINNED, RPC_DH_PRIVATE_KEY="", RPC_DH_KEY_ID="")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"RPC_DH_KEY_ID": ""}, "RPC_DH_KEY_ID is required"),
        ({"RPC_DH_KEY_ID": "b9"}, "is missing"),
        ({"RPC_DH_PRIVATE_KEY": DH["billing-next"][0]}, "not the public half"),
        ({"RPC_DH_PRIVATE_KEY": KEYS["billing"][0]}, "not X25519"),
        (
            {"RPC_SERVICE_DH_KEYS": {"billing": {"b1": KEYS["billing"][1]}}},
            "not X25519",
        ),
        ({"RPC_SERVICE_DH_KEYS": {"billing": {}}}, "non-empty"),
        ({"RPC_SERVICE_DH_KEYS": {"billing": {"b 1": DH["billing"][1]}}}, "kid"),
    ],
    ids=[
        "no-kid",
        "unpublished-kid",
        "mismatched-public",
        "ed25519-as-private",
        "ed25519-published",
        "empty-entry",
        "bad-kid",
    ],
)
def test_x25519_key_configuration_is_validated(
    extra: dict[str, Any], message: str
) -> None:
    with pytest.raises(_REFUSED, match=message):
        _settings(PINNED, **extra)


@pytest.fixture(autouse=True)
async def _close_shared_client() -> AsyncIterator[None]:
    yield
    await rpc.close_rpc_client()
