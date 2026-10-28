"""Contract conformance harness: a contract must behave the same local vs RPC.

The configurable-monolith promise is that ``core.get_service(name).m(...)``
is observably identical whether the provider is in-process or behind internal
RPC. :func:`assert_contract_conformance` checks it for a registered contract:
every :class:`ContractCase` is run

1. **locally** — through ``core.get_service`` (the contract runtime), and
2. **over RPC** — through a generated :class:`~sandloom.core.rpc.RemoteServiceProxy`
   talking to the SAME core's internal RPC routes over an in-process ASGI
   transport (pair-MAC authentication, keyed response binding, describe
   endpoint, wire codec — the real thing),

and the two outcomes must match: equal results of the SAME types (recursively;
a pydantic model vs a dict, an Enum vs its value, a tuple vs a list all
differ), or the same exception class with the same status, detail, headers
and code. An unexpected provider failure (a non-HTTP exception locally, a
``503 provider_error`` remotely) counts as the same outcome.

Usage in a module's tests::

    from sandloom.core.contract_testing import (
        UNIQUE, ContractCase, assert_contract_conformance,
    )

    await assert_contract_conformance(
        core,
        "billing.credits.v1",
        [
            ContractCase("get_balance", ("user", user_id)),
            ContractCase("spend", ("organization", other_org, 1, UNIQUE),
                         tenant=my_org, label="foreign org -> 403"),
        ],
    )

``UNIQUE`` anywhere in ``args`` / ``kwargs`` is replaced by a fresh string per
path, so a command's idempotency key never makes the RPC run a replay of the
local one. The RPC path is keyed with ephemeral service keys
(:func:`keyed_rpc_settings`: the core calls itself as ``sandloom-conformance``), so
no RPC configuration is needed. For contracts that read the
test's own (uncommitted) data, run it inside
:func:`sandloom.core.module_api.bind_contract_session` — the in-process transport
runs the provider in the caller's task, so both paths see the same session.

``transport=`` picks how the RPC path travels: ``"asgi"`` (default,
in-process), or ``"aiohttp"`` / ``"httpx"`` — the production client of that
``RPC_TRANSPORT`` over a REAL loopback socket to a uvicorn server started in
the test's event loop (in the test's context, so ``bind_contract_session``
still applies on the provider side).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from typing import Any
from uuid import UUID, uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sandloom.core import rpc_binding, rpc_identity, rpc_pair_mac
from sandloom.core.rpc import RemoteServiceProxy, RemoteServiceUnavailable, rpc_router
from sandloom.core.rpc.client import _new_rpc_client
from sandloom.core.rpc.context import (
    _context_payload,
    _encode_context,
)
from sandloom.core.rpc.outbound import (
    _MacSigner,
    _outbound_mac,
    _rpc_headers,
)
from sandloom.core.rpc.wire import _CTX_HEADER, _NONCE_HEADER
from sandloom.core.signing_keys import (
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)
from sandloom.core.tenant import bind_tenant

#: Placeholder replaced by a fresh unique string on each path (see module doc).
UNIQUE = "__sandloom_conformance_unique__"


@dataclass(frozen=True)
class ContractCase:
    """One call to compare: ``method(*args, **kwargs)`` under ``tenant``."""

    method: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    tenant: UUID | None = None
    label: str = ""

    def describe(self) -> str:
        return self.label or f"{self.method}{self.args!r}"


@dataclass(frozen=True)
class Outcome:
    """A normalized call outcome (comparable across the two paths)."""

    ok: bool
    value: Any = None
    error: tuple[Any, ...] | None = None


class ContractConformanceError(AssertionError):
    """The local and the RPC path disagreed for at least one case."""


def _shape(value: Any) -> Any:
    """``value`` with its exact runtime types made explicit (recursively)."""
    if isinstance(value, BaseModel):
        return (
            "model",
            f"{type(value).__module__}.{type(value).__qualname__}",
            _shape(value.model_dump(mode="python")),
        )
    if isinstance(value, Enum):
        return (
            "enum",
            f"{type(value).__module__}.{type(value).__qualname__}",
            value.value,
        )
    if isinstance(value, dict):
        return (
            "dict",
            sorted(((_shape(k), _shape(v)) for k, v in value.items()), key=repr),
        )
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [_shape(v) for v in value])
    if isinstance(value, (set, frozenset)):
        return (type(value).__name__, sorted((_shape(v) for v in value), key=repr))
    return (type(value).__name__, value)


def _error_shape(exc: BaseException) -> tuple[Any, ...]:
    if (
        isinstance(exc, RemoteServiceUnavailable)
        and getattr(exc, "code", None) == "provider_error"
    ):
        return ("provider_failure",)
    if isinstance(exc, HTTPException):
        return (
            type(exc).__qualname__,
            exc.status_code,
            exc.detail,
            dict(exc.headers or {}),
            getattr(exc, "code", None),
        )
    return ("provider_failure",)


def _materialize(value: Any, unique: str) -> Any:
    if isinstance(value, str) and value == UNIQUE:
        return unique
    if isinstance(value, tuple):
        return tuple(_materialize(v, unique) for v in value)
    if isinstance(value, list):
        return [_materialize(v, unique) for v in value]
    if isinstance(value, dict):
        return {k: _materialize(v, unique) for k, v in value.items()}
    return value


async def _run(target: Any, case: ContractCase, unique: str) -> Outcome:
    args = _materialize(case.args, unique)
    kwargs = _materialize(dict(case.kwargs), unique)
    try:
        method = getattr(target, case.method)
        if case.tenant is not None:
            with bind_tenant(case.tenant):
                value = await method(*args, **kwargs)
        else:
            value = await method(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - every outcome is compared
        return Outcome(ok=False, error=_error_shape(exc))
    return Outcome(ok=True, value=_shape(value))


@dataclass(frozen=True)
class RpcKeys:
    """One service's ephemeral internal-RPC keys (tests only)."""

    service_id: str
    kid: str
    #: Ed25519 (command signatures): private / public PEM.
    private_key: str
    public_key: str
    #: X25519 (request MAC + response binding): private / public PEM.
    dh_private_key: str
    dh_public_key: str


@cache
def rpc_keys(service_id: str) -> RpcKeys:
    """Fresh keys for ``service_id`` (generated once per process)."""
    signing = generate_private_key("EdDSA")
    dh = generate_x25519_key()
    return RpcKeys(
        service_id,
        "k1",
        private_pem(signing),
        public_pem(signing),
        x25519_private_pem(dh),
        x25519_public_pem(dh),
    )


def rpc_settings(
    service_id: str,
    *,
    peers: Iterable[str] = (),
    allowed_calls: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``Settings`` field values keying ``service_id`` for internal RPC.

    Its own Ed25519 and X25519 keys plus the public keys of ``service_id`` and
    every peer (:func:`rpc_keys`), and the ``RPC_ALLOWED_CALLS`` map. Apply
    with ``monkeypatch.setattr(settings, name, value)`` per item, or
    ``settings.model_copy(update=...)``; :func:`rpc_env` gives env names.
    """
    me = rpc_keys(service_id)
    services = [rpc_keys(s) for s in dict.fromkeys([service_id, *peers])]
    return {
        "rpc_service_id": service_id,
        "rpc_private_key": me.private_key,
        "rpc_key_id": me.kid,
        "rpc_dh_private_key": me.dh_private_key,
        "rpc_dh_key_id": me.kid,
        "rpc_service_keys": {k.service_id: {k.kid: k.public_key} for k in services},
        "rpc_service_dh_keys": {
            k.service_id: {k.kid: k.dh_public_key} for k in services
        },
        "rpc_allowed_calls": dict(allowed_calls or {}),
    }


def rpc_env(
    service_id: str,
    *,
    peers: Iterable[str] = (),
    allowed_calls: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """:func:`rpc_settings` as environment variables (maps JSON-encoded)."""
    return {
        name.upper(): value if isinstance(value, str) else json.dumps(value)
        for name, value in rpc_settings(
            service_id, peers=peers, allowed_calls=allowed_calls
        ).items()
    }


def keyed_rpc_settings(
    cfg: Any, contracts: Iterable[str], service_id: str = "sandloom-conformance"
) -> Any:
    """A copy of ``cfg`` keyed as ``service_id`` calling ITSELF.

    The in-process RPC harness serves and calls with it: one service id on
    both sides, allowed exactly ``contracts``.
    """
    return cfg.model_copy(
        update=rpc_settings(
            service_id, allowed_calls={service_id: sorted(set(contracts))}
        )
    )


def rpc_request_headers(
    settings: Any,
    target: str,
    body: bytes = b"",
    *,
    http_method: str = "POST",
    provider: str | None = None,
    sign: bool = False,
) -> dict[str, str]:
    """Authenticated headers of ONE raw internal-RPC request (tests).

    ``target`` is ``"<contract>/<method>"`` (a call) or ``"<contract>"`` (the
    describe GET); the request is MACed with ``settings``' pair key towards
    ``provider`` (default: ``settings``' own service id — a core calling
    itself) under a fresh nonce, and — with ``sign`` — also carries the
    command signature. The bound tenant (``bind_tenant``) travels in the
    context, as on a real call.
    """
    pinned = provider or rpc_identity.service_id(settings)
    key = _outbound_mac(settings, pinned, target.split("/", 1)[0])
    identity = rpc_identity.signing_identity(settings) if sign else None
    payload = _context_payload(identity, pinned)
    context = _encode_context(payload)
    headers = _rpc_headers()
    headers[_CTX_HEADER] = context
    nonce = headers[_NONCE_HEADER] = rpc_binding.new_nonce()
    _MacSigner(key, http_method, target, context, body, identity, payload).apply(
        headers, nonce
    )
    return headers


def rpc_response_headers(
    settings: Any,
    request_headers: Mapping[str, str],
    status_code: int,
    body: bytes,
) -> dict[str, str]:
    """The binding headers a provider keyed with ``settings`` puts on its
    response to ``request_headers`` (tests faking a provider's answer).

    The caller is the one the request's pair-MAC header names; the response
    is keyed with the pair key towards it, exactly as a real provider does
    once that request verified.
    """
    nonce = str(request_headers[rpc_binding.NONCE_HEADER])
    parsed = rpc_pair_mac.parse_header(str(request_headers[rpc_pair_mac.AUTH_HEADER]))
    key = rpc_binding.provider_key(settings, (parsed["caller"], parsed["ck"]))
    return dict(
        rpc_binding.response_headers(
            nonce=nonce,
            key=key,
            req_auth=rpc_binding.request_auth(request_headers),
            status_code=status_code,
            body=body,
        )
    )


def rpc_proxy_for(
    core: Any,
    contract_name: str,
    client: Any,
    base_url: str = "http://sandloom-conformance",
    *,
    settings: Any = None,
) -> Any:
    """A generated RPC proxy for ``contract_name`` served by ``core`` via ``client``.

    ``settings`` (default: the core's) key the call; the binding pins their
    service id — a core calling itself is its own provider.
    """
    cfg = settings if settings is not None else core.settings
    binding: dict[str, object] = {"base_url": base_url}
    provider = rpc_identity.service_id(cfg)
    if provider:
        binding["service_id"] = provider
    return RemoteServiceProxy(
        service_name=contract_name,
        binding=binding,
        default_timeout_seconds=30.0,
        client=client,
        settings=cfg,
    )


def _rpc_app(core: Any, settings: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.core = core
    app.include_router(rpc_router(core, settings=settings))
    return app


def in_process_rpc_client(core: Any, settings: Any = None) -> httpx.AsyncClient:
    """An HTTP client whose requests hit ``core``'s internal RPC routes
    in-process (authenticated and bound under ``settings``, default the core's)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_rpc_app(core, settings)),
        base_url="http://sandloom-conformance",
    )


@asynccontextmanager
async def served_rpc(
    core: Any, app: Any = None, settings: Any = None
) -> AsyncIterator[str]:
    """Serve ``core``'s internal RPC routes (or ``app``) on a loopback socket.

    A uvicorn server (httptools, as deployed) runs in the CURRENT event loop
    and context; yields its base URL.
    """
    config = uvicorn.Config(
        app if app is not None else _rpc_app(core, settings),
        host="127.0.0.1",
        port=0,
        lifespan="off",
        log_level="warning",
        http="httptools",
        access_log=False,
    )
    config.load()
    server = uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    try:
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        await server.shutdown()


@asynccontextmanager
async def rpc_client_for(
    core: Any, transport: str, settings: Any = None
) -> AsyncIterator[tuple[Any, str]]:
    """(client, base_url) of the RPC path for ``transport`` (see module doc)."""
    if transport == "asgi":
        async with in_process_rpc_client(core, settings) as client:
            yield client, "http://sandloom-conformance"
        return

    async with served_rpc(core, settings=settings) as base_url:
        client = _new_rpc_client(transport)
        try:
            yield client, base_url
        finally:
            await client.aclose()


async def check_contract_conformance(
    core: Any,
    contract_name: str,
    cases: Sequence[ContractCase],
    *,
    transport: str = "asgi",
) -> list[tuple[ContractCase, Outcome, Outcome]]:
    """Run every case locally and over RPC; return the mismatches."""
    local = core.get_service(contract_name)
    mismatches: list[tuple[ContractCase, Outcome, Outcome]] = []
    cfg = keyed_rpc_settings(core.settings, [contract_name])
    async with rpc_client_for(core, transport, cfg) as (client, base_url):
        remote = rpc_proxy_for(core, contract_name, client, base_url, settings=cfg)
        for case in cases:
            local_outcome = await _run(local, case, f"local-{uuid4().hex}")
            remote_outcome = await _run(remote, case, f"remote-{uuid4().hex}")
            if local_outcome != remote_outcome:
                mismatches.append((case, local_outcome, remote_outcome))
    return mismatches


async def assert_contract_conformance(
    core: Any,
    contract_name: str,
    cases: Sequence[ContractCase],
    *,
    transport: str = "asgi",
) -> None:
    """Assert ``contract_name`` behaves identically locally and over RPC."""
    mismatches = await check_contract_conformance(
        core, contract_name, cases, transport=transport
    )
    if mismatches:
        lines = [f"{contract_name} differs local vs RPC for {len(mismatches)} case(s):"]
        for case, local_outcome, remote_outcome in mismatches:
            lines.append(
                f"  - {case.describe()}: local={local_outcome!r} rpc={remote_outcome!r}"
            )
        raise ContractConformanceError("\n".join(lines))


__all__ = [
    "UNIQUE",
    "ContractCase",
    "ContractConformanceError",
    "Outcome",
    "RpcKeys",
    "assert_contract_conformance",
    "check_contract_conformance",
    "in_process_rpc_client",
    "keyed_rpc_settings",
    "rpc_client_for",
    "rpc_env",
    "rpc_keys",
    "rpc_proxy_for",
    "rpc_request_headers",
    "rpc_response_headers",
    "rpc_settings",
    "served_rpc",
]
