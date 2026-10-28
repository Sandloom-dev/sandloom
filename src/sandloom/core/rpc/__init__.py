"""Generated-style internal RPC for service-registry contracts.

The runtime keeps the developer-facing API local-first:
``core.get_service("x.y.v1")`` returns the registered Python object when the
provider module is co-deployed, or a generated proxy when ``SERVICE_BINDINGS``
declares a remote provider. Business modules do not author HTTP clients.

Identical semantics local vs remote
-----------------------------------
Both paths go through the same contract runtime
(:class:`sandloom.core.module_api.ContractProvider`) and the same wire codec
(:mod:`sandloom.core.contract_wire`): arguments are bound with Python's rules
(unknown / positional-only / keyword-only violations are a ``422``
:class:`~sandloom.core.module_api.ContractArgumentError` on both paths) and
validated against the provider's annotations; results are encoded once and
decoded against the method's return annotation. The proxy learns that
annotation from the provider's **describe endpoint**
(``GET /_sandloom/rpc/{service}`` -> method schemas + a contract hash), cached per
process and refreshed whenever a response carries a different
``X-SANDLOOM-Contract-Hash`` — so it works when the provider module is not even
installed in the caller (DTO classes resolve when the caller has imported
them, else degrade to plain data).

Errors: a provider ``HTTPException`` / ``http_status`` exception reaches the
caller as the same :class:`~sandloom.core.module_api.ContractCallError` (status,
detail, headers such as ``Retry-After``, code) as locally. Only transport and
trust failures (unreachable provider, a request or response that does not
authenticate, provider crash) are a ``503`` :class:`RemoteServiceUnavailable`; an
unknown service/method at the provider is a ``501``
:class:`ServiceUnavailableInDeployment` (indeterminate for authz).

Authentication (ADR-0004)
-------------------------
Every request — calls and describe / readiness probes — carries
``X-SANDLOOM-RPC-Auth``: an HMAC under a key only the caller and the addressed
provider derive from their X25519 keys (:mod:`sandloom.core.rpc_pair_mac`), over
caller, audience, key ids, method, target, the attempt's nonce, the context
bytes and the body digest. The provider verifies it over the raw bytes, then
the time window and the nonce replay guard, then the caller's allowlist
(``RPC_ALLOWED_CALLS``; 401 unverifiable, 403 not allowed). A COMMAND call
also carries the caller's Ed25519 signature over the request and its nonce
(``X-SANDLOOM-RPC-Service-Signature``, :mod:`sandloom.core.rpc_identity`;
``RPC_COMMANDS_REQUIRE_SIGNATURE``): a leaked provider X25519 key never forges
an effect. A request presenting no credential gets the hidden ``404``.

The MAC-covered **RPC context** (``X-SANDLOOM-RPC-Context``) carries the caller's
bound tenant, an issue time and the audience; the provider re-binds the
tenant ONLY from a verified context, and a bare ``X-Tenant-Id`` header is
rejected.

Response binding
----------------
A response can never be accepted for another call, whatever the HTTP client
does with its connections: every request (each transport attempt) carries a
fresh random nonce (``X-SANDLOOM-RPC-Nonce``); the provider echoes it and adds
``X-SANDLOOM-RPC-Response-MAC`` over (nonce, the request's MAC and signature,
status, body digest) under a per-pair key from the two services' X25519 keys
(:mod:`sandloom.core.rpc_binding`). The caller checks it before it reads the
response: another call's response (stale, unsolicited, swapped or replayed),
a corrupted, forged, unkeyed or wrongly keyed one is rejected
(``sandloom_rpc_response_binding_rejected_total{reason}``), the client's whole
connection pool is discarded (no new call uses it; calls in flight finish)
and the call fails with a transport error the single transport retry handles
like a dropped connection. Binding authenticates responses; it is not a
substitute for TLS/mTLS (no confidentiality).

Dedupe: a decorated COMMAND contract is deduplicated by the contract runtime
on its own idempotency argument — the same store, key and TTL for a local and
an RPC-served call (a transport retry of a call that may have committed
replays its result). Reads keep no record; the RPC path keeps none either.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import sandloom.core.http.request_authority as _request_authority
import sandloom.core.module_api as _module_api
from sandloom.core.module_api._service_registry import (
    ServiceNotFound as ServiceNotFound,
)
from sandloom.core.module_api._service_registry import (
    ServiceUnavailableInDeployment as ServiceUnavailableInDeployment,
)
from sandloom.core.rpc import (
    binding,
    client,
    context,
    dispatch,
    envelope,
    errors,
    inbound_auth,
    outbound,
    proxy,
    readiness,
    server,
    wire,
)
from sandloom.core.rpc.client import check_rpc_transport as check_rpc_transport
from sandloom.core.rpc.client import close_later as close_later
from sandloom.core.rpc.client import close_rpc_client as close_rpc_client
from sandloom.core.rpc.dispatch import _is_command as _is_command
from sandloom.core.rpc.envelope import _decode_envelope as _decode_envelope
from sandloom.core.rpc.errors import (
    RemoteServiceBadResponse as RemoteServiceBadResponse,
)
from sandloom.core.rpc.errors import (
    RemoteServiceUnavailable as RemoteServiceUnavailable,
)
from sandloom.core.rpc.proxy import RemoteServiceProxy as RemoteServiceProxy
from sandloom.core.rpc.proxy import remote_service_proxy as remote_service_proxy
from sandloom.core.rpc.readiness import (
    ContractIncompatibleError as ContractIncompatibleError,
)
from sandloom.core.rpc.readiness import (
    check_remote_contract_compat as check_remote_contract_compat,
)
from sandloom.core.rpc.readiness import (
    check_remote_service_bindings as check_remote_service_bindings,
)
from sandloom.core.rpc.server import RpcFastPathMiddleware as RpcFastPathMiddleware
from sandloom.core.rpc.server import rpc_router as rpc_router


def install_service_rpc_routes(app: Any, core: Any) -> None:
    """Wire the contract runtime into an app (inbound RPC + outbound resolver).

    * injects the remote-service-proxy factory into the facade, so
      ``core.get_service`` can build a remote fallback for a bound contract
      WITHOUT the facade importing this module;
    * mounts the internal RPC routes (plain Starlette routes — no FastAPI
      dependency resolution) AND an outermost fast path that serves
      ``/_sandloom/rpc/*`` before the HTTP middleware stack (CORS, request
      logging, idempotency capture, …) — request authentication, the
      body-size cap and the error envelope are applied by the handler itself;
    * opens a :class:`~sandloom.core.module_api.ContractReadScope` per HTTP request
      (shared read connection + memo for local read contracts).

    This is the single composition point that wires RPC into an app.
    """

    core.set_service_resolver(remote_service_proxy)
    core.add_startup_task(check_rpc_transport)
    core.add_shutdown_task(close_rpc_client)
    app.include_router(rpc_router(core))
    app.add_middleware(_module_api.ContractReadScopeMiddleware)
    # Added last = outermost: RPC requests skip every other middleware.
    app.add_middleware(RpcFastPathMiddleware, core=core)

    # RPC bypasses the ordinary stack, but is still a separate HTTP authority
    # boundary (including an in-process ASGI transport used by an integration).
    _request_authority.install_request_authority(app)


__all__ = [
    "RemoteServiceBadResponse",
    "RemoteServiceProxy",
    "RemoteServiceUnavailable",
    "ServiceUnavailableInDeployment",
    "ContractIncompatibleError",
    "check_remote_contract_compat",
    "check_remote_service_bindings",
    "check_rpc_transport",
    "close_rpc_client",
    "install_service_rpc_routes",
    "remote_service_proxy",
    "rpc_router",
]


_SUBMODULES = (
    wire,
    errors,
    envelope,
    context,
    client,
    outbound,
    binding,
    proxy,
    inbound_auth,
    dispatch,
    server,
    readiness,
)
#: Module global of a submodule -> the submodules that look it up.
_LOOKED_UP_IN: dict[str, tuple[str, ...]] = {}
for _module in _SUBMODULES:
    for _name in vars(_module):
        if not _name.startswith("__"):
            _LOOKED_UP_IN[_name] = (*_LOOKED_UP_IN.get(_name, ()), _module.__name__)
for _module in _SUBMODULES:  # the import system sets these on the package
    _LOOKED_UP_IN.pop(_module.__name__.rpartition(".")[2], None)
del _module, _name


class _RpcPackage(ModuleType):
    """``sandloom.core.rpc`` refuses to rebind a name its submodules look up.

    A name re-exported here is a copy of a submodule's binding, and code reads
    the submodule's: ``monkeypatch.setattr(sandloom.core.rpc, "_unpack", spy)``
    would patch nothing and a negative test would pass vacuously. Patch the
    name where it is looked up (``sandloom.core.rpc.server._unpack``) instead.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        homes = _LOOKED_UP_IN.get(name)
        if homes is not None:
            raise AttributeError(
                f"sandloom.core.rpc.{name} would patch nothing: set it where it is "
                f"looked up ({', '.join(f'{home}.{name}' for home in homes)})"
            )
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _RpcPackage
