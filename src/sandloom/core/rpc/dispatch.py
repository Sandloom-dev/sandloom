"""Provider-side dispatch: resolve the local contract, decode arguments,
invoke it in the provider's own UoW, map the outcome to the envelope.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast
from uuid import UUID

from fastapi import Response, status

import sandloom.core.module_api as _module_api
import sandloom.core.unit_of_work.request as _request
import sandloom.core.unit_of_work.risk as _risk
from sandloom.core import contract_wire
from sandloom.core.http.error_handlers import describe_exception
from sandloom.core.module_api import (
    ContractCallError,
    ContractMethodRuntime,
    ContractProvider,
)
from sandloom.core.module_api._service_registry import (
    ServiceNotFound,
)
from sandloom.core.rpc.context import _call_effects, _caller_scope
from sandloom.core.rpc.envelope import _error_response, _ok_response
from sandloom.core.rpc.wire import RpcRequest
from sandloom.core.tenant import bind_tenant

logger = logging.getLogger("sandloom.core.rpc")


def _is_command(core: Any, service_name: str, method_name: str) -> bool:
    """Is the target a command (shared nonce layer)? Unknown -> yes."""
    try:
        binding = _resolve_local_contract(core, service_name)
        runtime = binding.contract_schema.methods.get(method_name)
    except Exception:  # noqa: BLE001 - conservative: treat as a command
        return True
    return runtime is None or runtime.kind != "read"


def _resolve_local_contract(core: Any, service_name: str) -> ContractProvider:
    """The registered provider bound to the contract runtime.

    Uses the SAME binding as a local ``core.get_service`` call
    (``CoreFacade.local_contract``), so a remote caller gets identical
    semantics: the provider runs in its own Unit of Work on its own database.
    """
    local_contract = getattr(core, "local_contract", None)
    if callable(local_contract):
        service = local_contract(service_name)
        if service is None:
            raise ServiceNotFound(f"Service '{service_name}' not found")
        return cast(ContractProvider, service)

    return cast(
        ContractProvider,
        _module_api.bind_contract(
            core.service_registry.get(service_name),
            core=core,
            database="default",
            name=service_name,
        ),
    )


def _lookup(
    core: Any, service_name: str, method_name: str
) -> tuple[ContractProvider, ContractMethodRuntime] | Response:
    """Resolve the exposed method, or the 404 envelope of a failed LOOKUP."""
    try:
        binding = _resolve_local_contract(core, service_name)
    except ServiceNotFound:
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="service_not_found",
            message=f"Service {service_name!r} is not registered",
        )
    runtime = (
        None
        if method_name.startswith("_")
        else binding.contract_schema.methods.get(method_name)
    )
    if runtime is None:
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="method_not_found",
            message=f"Method {method_name!r} is not exposed by {service_name!r}",
        )
    return binding, runtime


async def _handle_call(
    *,
    core: Any,
    service_name: str,
    method_name: str,
    payload: RpcRequest,
    tenant_id: UUID | None,
    accept_typed_hash: str | None = None,
    caller: Any = None,
) -> Response:
    """Serve one call: look the method up, then :func:`_handle_rpc_call`.

    The RPC path adds no semantics of its own: reads keep no record and
    commands are deduplicated by the contract runtime on their own
    idempotency argument — the same store, key and TTL as a local call.
    """
    found = _lookup(core, service_name, method_name)
    if isinstance(found, Response):
        return found
    binding, runtime = found
    return await _handle_rpc_call(
        core=core,
        service_name=service_name,
        binding=binding,
        runtime=runtime,
        payload=payload,
        tenant_id=tenant_id,
        typed_result=accept_typed_hash is not None
        and accept_typed_hash == binding.contract_schema.hash,
        caller=caller,
    )


async def _handle_rpc_call(
    *,
    core: Any,
    service_name: str,
    binding: ContractProvider,
    runtime: ContractMethodRuntime,
    payload: RpcRequest,
    tenant_id: UUID | None = None,
    typed_result: bool = False,
    caller: Any = None,
) -> Response:
    """Bind and invoke one RPC call; map each failure to ONE status.

    * argument binding / validation failure (before the provider runs) -> 422
      (``contract_argument_invalid``, exactly like a local call);
    * an exception raised INSIDE the provider -> its ``HTTPException`` /
      ``http_status`` status, detail, headers and code (the same
      :class:`ContractCallError` a local caller gets), else 500 (logged).
    """
    try:
        args, kwargs = _decode_arguments(runtime, payload)
    except (ValueError, TypeError) as exc:
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="contract_argument_invalid",
            message=f"{service_name}.{runtime.name}: undecodable argument ({exc})",
        )
    # The caller facts bound as the call's request scope: its checks (the
    # end user, step-up, money dedupe, the commit-time re-check) run exactly
    # as for a local call, and a nested call carries them on (local ==
    # remote). None: a system call, as a local call with no request.
    scope = _caller_scope(core, caller)
    observed = _risk.ObservedEffects()
    try:
        # Re-bind the caller's tenant scope on the provider side. In the
        # monolith the contract runs under the request's bound TenantScope; the
        # same call over RPC must too, or TenantScoped queries in the provider
        # would run unscoped (cross-tenant leak) or fail closed. The tenant
        # comes ONLY from the verified RPC context; none => unbound, identical
        # to a monolith call made with no tenant in scope. The bound contract
        # opens the provider's own Unit of Work — exactly as for a local caller.
        with _request.bind_request_scope(scope), observed:
            if tenant_id is not None:
                with bind_tenant(tenant_id):
                    result = await binding.invoke(runtime, args, kwargs)
            else:
                result = await binding.invoke(runtime, args, kwargs)
        codec, wire = binding.encode_result_as(runtime, result, typed=typed_result)
        content: dict[str, Any] = {"ok": True, "result": wire}
        fx = _call_effects(scope, observed)
        if fx is not None:
            # MAC-bound with the body: what the execution (its nested calls
            # included, their uncertainty too) left on the caller's request
            # (G-SENS-04, G-UNIT-02, spec §4.5)
            content["fx"] = fx
        return _ok_response(
            content,
            contract_hash=binding.contract_schema.hash,
            codec=codec,
        )
    except ContractCallError as exc:
        detail = exc.detail
        return _error_response(
            status_code=exc.status_code,
            code=exc.code or "http_error",
            message=detail if isinstance(detail, str) else json.dumps(detail),
            details={"detail": detail, "headers": dict(exc.headers or {})},
            # a failed call's committed nested effects mark the caller alike
            fx=_call_effects(scope, observed),
        )
    except Exception as exc:  # normalize provider failures
        logger.error(
            "Unhandled RPC provider error for %s.%s: %s",
            service_name,
            runtime.name,
            describe_exception(exc),
            exc_info=True,
        )
        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="provider_error",
            message="Remote provider failed",
            fx=_call_effects(scope, observed),
        )


def _decode_arguments(
    runtime: ContractMethodRuntime, payload: RpcRequest
) -> tuple[list[Any], dict[str, Any]]:
    """Untagged argument values of a request in ``payload.codec``.

    Codec 2 values of fully typed parameters are already plain (validated by
    the bind step next); everything else is untagged from the tagged tree.
    """
    codec = payload.codec
    if codec != contract_wire.CODEC_TYPED:
        return (
            [contract_wire.decode_untyped(arg) for arg in payload.args],
            {
                key: contract_wire.decode_untyped(value)
                for key, value in payload.keyword_args.items()
            },
        )
    plans = runtime.param_plans
    names = list(runtime.signature.parameters)
    args = [
        plans[names[i]].from_wire(value, codec)
        if i < len(names)
        else contract_wire.decode_untyped(value)
        for i, value in enumerate(payload.args)
    ]
    kwargs = {
        key: plans[key].from_wire(value, codec)
        if key in plans
        else contract_wire.decode_untyped(value)
        for key, value in payload.keyword_args.items()
    }
    return args, kwargs
