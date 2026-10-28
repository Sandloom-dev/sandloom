"""The RPC response envelope: encoding (provider) and decoding into the
caller-side error of a failed call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

from fastapi import Response, status

from sandloom.core import contract_wire
from sandloom.core.module_api import (
    contract_error,
)
from sandloom.core.module_api._service_registry import (
    ServiceUnavailableInDeployment,
)
from sandloom.core.rpc.errors import RemoteServiceBadResponse, RemoteServiceUnavailable
from sandloom.core.rpc.wire import (
    _CODEC_HEADER,
    _CONTENT_TYPE,
    _CONTRACT_HASH_HEADER,
    _pack,
    _unpack,
)

#: Error codes that are transport / trust failures (-> 503 unavailable), with
#: the caller-facing reason.
_UNAVAILABLE_CODES: dict[str, str] = {
    "rpc_context_unverifiable": (
        "the provider cannot verify the signed caller context (configure the "
        "same INTERNAL_API_TOKEN on both services)"
    ),
    "rpc_context_invalid": "the provider rejected the signed caller context",
    "rpc_context_expired": "the signed caller context is stale (clock skew?)",
    "rpc_unsigned_tenant": "the provider refused an unsigned tenant header",
    # Service identity / pair MAC: a deployment problem (keys, allowlist,
    # clocks), never the end user's — so 503, not the provider's 401 / 403.
    "rpc_identity_invalid": "the provider rejected our service identity",
    "rpc_identity_unknown": "the provider does not know our service key",
    "rpc_caller_forbidden": (
        "the provider does not allow this service to call it (RPC_ALLOWED_CALLS)"
    ),
    "rpc_auth_invalid": "the provider rejected our request authentication",
    "rpc_auth_required": (
        "the provider requires pair-MAC authentication (RPC_DH_PRIVATE_KEY)"
    ),
    "rpc_auth_expired": (
        "our request is outside the provider's time window (clock skew?)"
    ),
    "rpc_auth_replayed": "the provider saw this request nonce before",
    "rpc_auth_signature_required": (
        "the provider requires our service signature on commands (give this "
        "service RPC_PRIVATE_KEY + RPC_KEY_ID)"
    ),
    "rpc_replay_unavailable": "the provider's replay guard is unavailable; retry later",
    "rpc_decode_error": "the provider could not decode the request",
    "rpc_idempotency_unavailable": (
        "the provider's idempotency store is unavailable; retry later"
    ),
    "rpc_in_progress": (
        "a previous attempt of this call is still running on the provider; "
        "retry later with the same idempotency key"
    ),
    "provider_error": "the provider failed (see its logs)",
}


_NOT_FOUND_CODES = frozenset({"service_not_found", "method_not_found"})


def _decode_envelope(
    service_name: str,
    response: Any,
    effects: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """The success envelope, or raise the caller-side error of a failed call.

    ``effects`` receives the call effects (``fx``) of the envelope, success
    or error, BEFORE a failure is raised: a failed call's committed effects
    mark the caller's request exactly as the same call made locally."""
    data = _open_envelope(service_name, response)
    fx = data.get("fx")
    if fx is not None and effects is not None:
        effects(fx)
    return _envelope_result(service_name, response, data)


def _open_envelope(service_name: str, response: Any) -> dict[str, Any]:
    """The well-formed envelope of ``response`` (a success, or an error with
    its ``error`` object), its ``fx`` not yet read; anything else raises the
    error of a reply without an envelope."""
    try:
        data = _unpack(response.content)
    except Exception:  # noqa: BLE001 - not a Sandloom envelope
        data = None
    if not isinstance(data, dict) or not (
        (data.get("ok") is True and response.status_code == status.HTTP_200_OK)
        or isinstance(data.get("error"), dict)
    ):
        _raise_without_envelope(service_name, response.status_code)
    return cast(dict[str, Any], data)


def _envelope_result(
    service_name: str, response: Any, data: dict[str, Any]
) -> dict[str, Any]:
    """The opened envelope ``data`` if it is a success, else raise its error."""
    if data.get("ok") is True and response.status_code == status.HTTP_200_OK:
        return data
    error = cast(dict[str, Any], data["error"])
    code = str(error.get("code") or "")
    error_status = int(error.get("status") or response.status_code)
    message = error.get("message") or "Remote service call failed"
    if code in _UNAVAILABLE_CODES:
        raise RemoteServiceUnavailable(
            service_name, reason=_UNAVAILABLE_CODES[code], code=code
        )
    if code in _NOT_FOUND_CODES:
        # The provider does not expose it in THIS deployment (older version,
        # wrong binding): indeterminate, like a contract that is not deployed.
        raise ServiceUnavailableInDeployment(service_name, reason=str(message))
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    details = cast(dict[str, Any], details)
    headers = details.get("headers")
    raise contract_error(
        status_code=error_status,
        detail=details.get("detail", message),
        headers=(
            {str(k): str(v) for k, v in headers.items()}
            if isinstance(headers, dict) and headers
            else None
        ),
        code=None if code in {"", "http_error"} else code,
    )


def _raise_without_envelope(service_name: str, status_code: int) -> None:
    """A reply that is not Sandloom's RPC envelope (gateway / auth / routing)."""
    if status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}:
        raise RemoteServiceUnavailable(
            service_name,
            reason="the provider rejected our internal token",
            code="rpc_unauthorized",
        )
    if status_code == status.HTTP_404_NOT_FOUND:
        raise RemoteServiceUnavailable(
            service_name,
            reason="no RPC endpoint at the binding URL (or RPC disabled)",
            code="rpc_endpoint_not_found",
        )
    if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        raise RemoteServiceUnavailable(service_name, reason="provider error")
    raise RemoteServiceBadResponse(service_name)


def _ok_response(
    content: dict[str, Any],
    *,
    contract_hash: str | None = None,
    codec: int = contract_wire.CODEC_TAGGED,
) -> Response:
    headers: dict[str, str] = {}
    if contract_hash:
        headers[_CONTRACT_HASH_HEADER] = contract_hash
    if codec != contract_wire.CODEC_TAGGED:
        headers[_CODEC_HEADER] = str(codec)
    return Response(
        content=_pack(content), media_type=_CONTENT_TYPE, headers=headers or None
    )


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    fx: dict[str, Any] | None = None,
) -> Response:
    content: dict[str, Any] = {
        "ok": False,
        "error": {
            "type": _error_type(status_code),
            "status": status_code,
            "code": code,
            "message": message,
            "details": details or {},
        },
    }
    if fx:
        content["fx"] = fx
    return Response(
        status_code=status_code,
        media_type=_CONTENT_TYPE,
        content=_pack(content),
    )


def _error_type(
    status_code: int,
) -> Literal["validation_error", "domain_error", "provider_error"]:
    if status_code == status.HTTP_422_UNPROCESSABLE_CONTENT:
        return "validation_error"
    if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        return "provider_error"
    return "domain_error"
