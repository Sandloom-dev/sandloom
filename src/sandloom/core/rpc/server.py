"""The provider's HTTP surface: the two internal RPC handlers, the router
and the outermost fast-path middleware.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

import sandloom.core.http.middlewares as _middlewares
import sandloom.core.observability.request_context as _request_context
from sandloom.core import contract_wire
from sandloom.core.http.error_handlers import describe_exception
from sandloom.core.module_api._service_registry import ServiceNotFound
from sandloom.core.observability import metrics
from sandloom.core.rpc.binding import _bind_response, _binding_inputs, _BindingSend
from sandloom.core.rpc.context import _parse_rpc_tenant
from sandloom.core.rpc.dispatch import (
    _handle_call,
    _is_command,
    _resolve_local_contract,
)
from sandloom.core.rpc.envelope import _error_response, _ok_response
from sandloom.core.rpc.errors import _RpcContextError
from sandloom.core.rpc.inbound_auth import (
    _core_settings,
    _presents_credential,
    _verify_pair_mac,
)
from sandloom.core.rpc.wire import (
    _ACCEPT_CODEC_HEADER,
    ACCESS_LIFETIME_DESCRIBE_KEY,
    RpcRequest,
    _unpack,
)

logger = logging.getLogger("sandloom.core.rpc")

#: Bound by assignment (keeps the ``.set()`` method-call fast path).
request_id_var = _request_context.request_id_var


#: Path prefix of the internal RPC endpoints.
RPC_PATH_PREFIX = "/_sandloom/rpc/"


_RPC_CALL_TEMPLATE = "/_sandloom/rpc/{service_name}/{method_name}"


_RPC_STATUS_TEMPLATE = "/_sandloom/rpc/{service_name}"


class _RpcEndpoints:
    """The two internal RPC handlers, bound to a core (plain ASGI callables)."""

    def __init__(self, core: Any, settings: Any = None) -> None:
        self.core = core
        #: Settings override (the conformance harness serves a keyed copy);
        #: ``None`` = the active core's settings.
        self.settings = settings

    def _active_core(self, request: Request) -> Any:
        app = request.scope.get("app")
        state = getattr(app, "state", None)
        return getattr(state, "core", None) or self.core

    def _cfg(self, core: Any) -> Any:
        return self.settings if self.settings is not None else _core_settings(core)

    async def call_service(self, request: Request) -> Response:
        params = request.path_params
        response = await self.serve_call(
            request, str(params["service_name"]), str(params["method_name"])
        )
        _bind_response(response, request, self._cfg(self._active_core(request)))
        return response

    async def service_status(self, request: Request) -> Response:
        if request.method != "GET":
            # Starlette also routes HEAD here; the MAC / signature names GET.
            return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)
        response = await self.serve_status(
            request, str(request.path_params["service_name"])
        )
        _bind_response(response, request, self._cfg(self._active_core(request)))
        return response

    async def serve_call(
        self, request: Request, service_name: str, method_name: str
    ) -> Response:
        active_core = self._active_core(request)
        cfg = self._cfg(active_core)
        headers = request.headers
        if not _presents_credential(headers):
            return _hidden_not_found()
        try:
            raw = await request.body()
        except HTTPException:
            raise  # the body-size cap (413) — handled by the cap middleware
        try:
            context = await _verify_pair_mac(
                cfg,
                headers,
                scope=request.scope,
                http_method="POST",
                target=f"{service_name}/{method_name}",
                body=raw,
                contract=service_name,
                method=method_name,
                command=lambda: _is_command(active_core, service_name, method_name),
            )
            tenant_id = _parse_rpc_tenant(context.get("tenant_id"))
        except _RpcContextError as exc:
            return _error_response(
                status_code=exc.status_code, code=exc.code, message=exc.message
            )
        except HTTPException as exc:
            return _error_response(
                status_code=exc.status_code,
                code="rpc_context_invalid",
                message=str(exc.detail),
            )
        # Decode only AFTER the MAC over the raw bytes verified:
        # MessagePack allocates by declared sizes, so an unauthenticated
        # caller must never reach the decoder.
        try:
            decoded = _unpack(raw) if raw else {}
        except Exception:  # noqa: BLE001 - any malformed body is a 422
            return _error_response(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="rpc_decode_error",
                message="Request body is not valid MessagePack",
            )
        if not isinstance(decoded, dict):
            return _error_response(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="rpc_decode_error",
                message="RPC request body must be a map",
            )
        payload = _parse_rpc_body(decoded)
        if payload is None:
            return _error_response(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="rpc_decode_error",
                message="RPC request body must be {args: [...], kwargs: {...}}",
            )
        accept_hash = _accepted_typed_hash(headers.get(_ACCEPT_CODEC_HEADER))
        return await _handle_call(
            core=active_core,
            service_name=service_name,
            method_name=method_name,
            payload=payload,
            tenant_id=tenant_id,
            accept_typed_hash=accept_hash,
            # the caller facts (verified with the context): the end user the
            # call runs for and what the provider's checks read
            caller=context.get("usr"),
        )

    async def serve_status(self, request: Request, service_name: str) -> Response:
        active_core = self._active_core(request)
        cfg = self._cfg(active_core)
        if not _presents_credential(request.headers):
            return _hidden_not_found()
        try:
            await _verify_pair_mac(
                cfg,
                request.headers,
                scope=request.scope,
                http_method="GET",
                target=service_name,
                body=b"",
                contract=service_name,
                method=None,
                command=lambda: False,
            )
        except _RpcContextError as exc:
            return _error_response(
                status_code=exc.status_code, code=exc.code, message=exc.message
            )
        try:
            binding = _resolve_local_contract(active_core, service_name)
        except ServiceNotFound:
            return _error_response(
                status_code=status.HTTP_404_NOT_FOUND,
                code="service_not_found",
                message=f"Service {service_name!r} is not registered",
            )
        schema = binding.contract_schema
        describe = schema.describe()
        # Additive: this process's access-token lifetime. It must be identical
        # in every service (revocation-mark TTLs derive from it); a bound
        # caller compares it with its own and fails /ready on drift. Not part
        # of the contract hash / snapshot.
        lifetime = getattr(cfg, "access_token_expire_minutes", None)
        if isinstance(lifetime, int):
            describe[ACCESS_LIFETIME_DESCRIBE_KEY] = lifetime
        return _ok_response(
            {
                "ok": True,
                "service": service_name,
                "methods": sorted(schema.methods),
                # The describe document: method schemas + a stable hash the
                # proxy caches and compares with X-SANDLOOM-Contract-Hash.
                "contract": describe,
            },
            contract_hash=schema.hash,
        )


def _hidden_not_found() -> Response:
    # 404, not 403: hide the existence of internal endpoints (same body as
    # the framework's 404 for an unknown route).
    return Response(
        content=b'{"detail":"Not Found"}',
        status_code=status.HTTP_404_NOT_FOUND,
        media_type="application/json",
    )


def _parse_rpc_body(decoded: dict[Any, Any]) -> RpcRequest | None:
    """The request body's args / kwargs / codec, or ``None`` when malformed.

    Hand-checked (no pydantic model validation of a possibly large argument
    tree): ``args`` a list, ``kwargs`` a map with string keys, ``codec`` a
    supported wire codec.
    """
    args = decoded.get("args", [])
    kwargs = decoded.get("kwargs", {})
    codec = decoded.get("codec", contract_wire.CODEC_TAGGED)
    if (
        not isinstance(args, list)
        or not isinstance(kwargs, dict)
        or not all(isinstance(k, str) for k in kwargs)
        or isinstance(codec, bool)
        or codec not in contract_wire.SUPPORTED_CODECS
    ):
        return None
    return RpcRequest.model_construct(args=args, keyword_args=kwargs, codec=codec)


def _accepted_typed_hash(raw: str | None) -> str | None:
    """The contract hash a caller accepts a codec-v2 result for, if any."""
    if not raw:
        return None
    codec, _, contract_hash = raw.partition(";")
    if codec.strip() != str(contract_wire.CODEC_TYPED) or not contract_hash:
        return None
    return contract_hash.strip()


def rpc_router(core: Any, *, settings: Any = None) -> APIRouter:
    """The internal RPC routes serving ``core``'s local contracts.

    ``request.app.state.core`` (when set) wins over ``core``. The routes are
    plain Starlette ``Route``s — the handlers parse, authenticate and encode
    themselves, so no FastAPI dependency resolution / request validation runs
    per call. Mounted by :func:`install_service_rpc_routes`; usable on its own
    to serve a core in-process (the contract conformance harness does).
    ``settings`` overrides the
    core's settings for authentication and response binding (the
    conformance harness serves under an ephemeral keyed copy).
    """
    endpoints = _RpcEndpoints(core, settings)
    router = APIRouter(include_in_schema=False)
    router.add_route(
        "/_sandloom/rpc/{service_name}/{method_name}",
        endpoints.call_service,
        methods=["POST"],
        include_in_schema=False,
    )
    router.add_route(
        "/_sandloom/rpc/{service_name}",
        endpoints.service_status,
        methods=["GET"],
        include_in_schema=False,
    )
    return router


class RpcFastPathMiddleware:
    """Outermost pure-ASGI layer serving ``/_sandloom/rpc/*`` directly.

    Internal RPC is service-to-service traffic: it needs neither CORS, nor the
    idempotency-capture / timing / security-header middlewares, nor FastAPI's
    routing + dependency resolution (perf4: ~25% of an RPC call's CPU). This
    layer matches the two RPC routes itself and calls the same handlers the
    mounted routes use, after the request-body cap (``MAX_REQUEST_BODY_BYTES``)
    and request-id propagation. Any other request — or an RPC path of another
    shape / method — continues down the normal stack unchanged.
    """

    def __init__(self, app: Any, core: Any) -> None:
        self.app = app
        self._endpoints = _RpcEndpoints(core)
        cfg = _core_settings(core)
        rpc_limit = int(getattr(cfg, "rpc_max_request_bytes", 4 * 1024 * 1024))
        global_limit = int(getattr(cfg, "effective_max_request_body_bytes", 0))
        limit = min(rpc_limit, global_limit) if global_limit > 0 else rpc_limit
        self._capped = _middlewares.BodySizeLimitMiddleware(
            self._serve, max_bytes=limit
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith(
            RPC_PATH_PREFIX
        ):
            target = self._match(scope)
            if target is not None:
                scope.setdefault("path_params", {})
                inputs = _binding_inputs(scope)
                if inputs is not None:
                    send = _BindingSend(
                        send, inputs, scope, _core_settings(self._endpoints.core)
                    )
                await self._capped(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _match(scope: Any) -> tuple[str, ...] | None:
        parts = scope["path"][len(RPC_PATH_PREFIX) :].split("/")
        method = scope.get("method")
        if method == "POST" and len(parts) == 2 and all(parts):
            return (parts[0], parts[1])
        if method == "GET" and len(parts) == 1 and parts[0]:
            return (parts[0],)
        return None

    async def _serve(self, scope: Any, receive: Any, send: Any) -> None:
        target = self._match(scope)
        assert target is not None  # noqa: S101 - type narrowing: matched in __call__
        request = Request(scope, receive)
        request_id = request.headers.get("x-request-id")
        token = request_id_var.set(request_id[:128] if request_id else None)
        method = "POST" if len(target) == 2 else "GET"
        path = _RPC_CALL_TEMPLATE if len(target) == 2 else _RPC_STATUS_TEMPLATE
        started = time.perf_counter()
        status_code = 500
        try:
            try:
                if len(target) == 2:
                    response = await self._endpoints.serve_call(request, *target)
                else:
                    response = await self._endpoints.serve_status(request, target[0])
            except HTTPException:
                raise  # body cap -> 413 by the cap middleware
            except Exception as exc:  # never leak a bare 500
                logger.error(
                    "Internal RPC handler failed for %s: %s",
                    scope.get("path"),
                    describe_exception(exc),
                    exc_info=True,
                )
                response = _error_response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    code="provider_error",
                    message="Remote provider failed",
                )
            if request_id:
                response.headers["X-Request-ID"] = request_id[:128]
            status_code = response.status_code
            await response(scope, receive, send)
        finally:
            request_id_var.reset(token)
            # The same HTTP RED series the (bypassed) timing middleware emits
            # (label children cached: same series, no per-call label lookup).
            _metric_child(metrics.http_request_duration_seconds, method, path).observe(
                time.perf_counter() - started
            )
            _metric_child(
                metrics.http_requests_total, method, path, str(status_code)
            ).inc()


#: (metric, labels...) -> its labelled child. Keyed by the metric object, so a
#: re-created registry (tests) never feeds a stale child.
_metric_children: dict[tuple[Any, ...], Any] = {}


def _metric_child(metric: Any, *labels: str) -> Any:
    key = (metric, *labels)
    child = _metric_children.get(key)
    if child is None:
        if len(_metric_children) >= 256:
            _metric_children.clear()
        names = ("method", "path", "status")[: len(labels)]
        child = metric.labels(**dict(zip(names, labels, strict=True)))
        _metric_children[key] = child
    return child
