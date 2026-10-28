"""Caller-side authentication: the pair-MAC (and, for commands, Ed25519
signature) headers of an outbound call or describe / readiness GET.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import sandloom.core.observability.request_context as _request_context
from sandloom.core import rpc_identity, rpc_pair_mac
from sandloom.core.rpc.context import _context_payload, _encode_context
from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.core.rpc.wire import _CONTENT_TYPE, _CTX_HEADER, _unb64


def _outbound_mac(
    cfg: Any, provider: str | None, service_name: str
) -> rpc_pair_mac.CallerKey:
    """This service's pair key towards the binding's provider.

    A binding whose key cannot be derived fails the call (``503
    rpc_auth_unavailable``) — startup refuses that configuration, so this is a
    live misconfiguration only.
    """
    try:
        return rpc_pair_mac.caller_key(cfg, provider)
    except rpc_pair_mac.PairKeyError as exc:
        raise RemoteServiceUnavailable(
            service_name,
            reason=f"no pair key for the provider ({exc})",
            code="rpc_auth_unavailable",
        ) from exc


@dataclass(frozen=True)
class _MacSigner:
    """Adds ``X-SANDLOOM-RPC-Auth`` for one attempt (the nonce is per attempt).

    With an ``identity`` (a signed call: commands, or every call with
    ``RPC_COMMANDS_REQUIRE_SIGNATURE=all``) the attempt's nonce also goes INTO
    the context as ``n`` and the context is re-signed: the signature covers
    the nonce, so a signed command can never be re-MACed under a fresh nonce
    by a holder of the provider's X25519 key (ADR-0004, KCI). Re-signing
    per attempt keeps the single transport retry safe.

    ``payload`` is the context before encoding (``context`` decoded): the
    per-attempt context is built from it directly, and the caller skips
    signing the nonce-less context this signer replaces anyway.
    """

    key: rpc_pair_mac.CallerKey
    http_method: str
    target: str
    context: str
    body: bytes
    identity: rpc_identity.CallerIdentity | None = None
    payload: dict[str, Any] | None = None

    def apply(self, headers: dict[str, str], nonce: str) -> None:
        context = self.context
        if self.identity is not None:
            payload = self.payload
            if payload is None:
                payload = json.loads(
                    _unb64(context), object_pairs_hook=rpc_identity._no_duplicate_keys
                )
            context = _encode_context({**payload, "n": nonce})
            headers[_CTX_HEADER] = context
            headers[rpc_identity.SVC_SIG_HEADER] = rpc_identity.sign(
                self.identity, self.http_method, self.target, self.body, context
            )
        headers[rpc_pair_mac.AUTH_HEADER] = self.key.header(
            http_method=self.http_method,
            target=self.target,
            nonce=nonce,
            context=context,
            body=self.body,
        )


def _get_auth(
    service_name: str,
    cfg: Any,
    mac_key: rpc_pair_mac.CallerKey,
    audience: str | None = None,
) -> tuple[dict[str, str], _MacSigner]:
    """Headers (+ per-attempt MAC signer) of a describe / readiness GET."""
    identity = (
        rpc_identity.signing_identity(cfg)
        if rpc_pair_mac.all_calls_require_signature(cfg)
        else None
    )
    headers = _rpc_headers()
    payload = _context_payload(identity, audience)
    context = _encode_context(payload)
    headers[_CTX_HEADER] = context
    # Signed (and the context re-built with the nonce) per attempt.
    signer = _MacSigner(mac_key, "GET", service_name, context, b"", identity, payload)
    return headers, signer


#: The static request headers (copied per call).
_BASE_HEADERS = {"Content-Type": _CONTENT_TYPE, "Accept": _CONTENT_TYPE}


def _rpc_headers() -> dict[str, str]:
    headers = _BASE_HEADERS.copy()
    # The caller's tenant travels in the MAC-covered RPC context (see _call),
    # never as a bare X-Tenant-Id header the provider would have to trust.
    request_id = _REQUEST_ID_VAR.get()
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


#: The current request id (sandloom.core.observability.request_context), bound by
#: assignment (keeps the ``.get()`` method-call fast path).
_REQUEST_ID_VAR = _request_context.request_id_var
