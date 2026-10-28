"""Response binding (:mod:`sandloom.core.rpc_binding`) wired into both sides: the
provider binds every response to its request nonce, the caller verifies it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response

from sandloom.core import rpc_binding
from sandloom.core.config.settings import settings as _global_settings
from sandloom.core.observability import metrics
from sandloom.core.rpc.client import _discard_pool, _transport
from sandloom.core.rpc.errors import _RpcRefused
from sandloom.core.rpc.outbound import _MacSigner
from sandloom.core.rpc.wire import _NONCE_HEADER, _unpack

logger = logging.getLogger("sandloom.core.rpc")


# --- response binding (sandloom.core.rpc_binding) ----------------------------------

_new_nonce = rpc_binding.new_nonce


_valid_nonce = rpc_binding.valid_nonce


#: Request scope key: (svc, X25519 kid) of a caller whose request MAC VERIFIED,
#: set by the handlers, read when the response is bound (per-pair key).
_CALLER_SCOPE_KEY = "sandloom.rpc_caller"


def _bind_response(response: Response, request: Request, cfg: Any) -> None:
    """Bind a provider response (plain-router path) to its request."""
    headers = request.headers
    if any(len(headers.getlist(n.decode())) > 1 for n in _BOUND_HEADERS):
        return  # never bind a request carrying two nonces / tags
    nonce = _valid_nonce(headers.get(_NONCE_HEADER))
    if nonce is None:
        return
    key = rpc_binding.provider_key(cfg, request.scope.get(_CALLER_SCOPE_KEY))
    for name, value in rpc_binding.response_headers(
        nonce=nonce,
        key=key,
        req_auth=rpc_binding.request_auth(headers),
        status_code=response.status_code,
        body=bytes(response.body),
    ):
        response.headers[name] = value


@dataclass(frozen=True)
class _BindingInputs:
    nonce: str
    req_auth: bytes


class _BindingSend:
    """ASGI ``send`` wrapper binding whatever response goes out (including
    the body cap's 413) to the request's nonce."""

    __slots__ = ("_send", "_inputs", "_scope", "_cfg", "_start", "_chunks")

    def __init__(self, send: Any, inputs: _BindingInputs, scope: Any, cfg: Any) -> None:
        self._send = send
        self._inputs = inputs
        self._scope = scope
        self._cfg = cfg
        self._start: dict[str, Any] | None = None
        self._chunks: list[bytes] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "http.response.start":
            self._start = message
            return
        if kind == "http.response.body" and self._start is not None:
            self._chunks.append(bytes(message.get("body", b"")))
            if message.get("more_body", False):
                return
            start, self._start = self._start, None
            body = b"".join(self._chunks)
            self._chunks = []
            inputs = self._inputs
            key = rpc_binding.provider_key(
                self._cfg, self._scope.get(_CALLER_SCOPE_KEY)
            )
            headers = list(start.get("headers") or ())
            for name, value in rpc_binding.response_headers(
                nonce=inputs.nonce,
                key=key,
                req_auth=inputs.req_auth,
                status_code=int(start["status"]),
                body=body,
            ):
                headers.append((name.lower().encode(), value.encode()))
            await self._send({**start, "headers": headers})
            await self._send({"type": "http.response.body", "body": body})
            return
        await self._send(message)


def _check_binding(
    client: Any,
    response: Any,
    *,
    nonce: str,
    sent: dict[str, str],
    peer: str,
    expect_service: str | None = None,
    cfg: Any = None,
) -> None:
    """Accept ``response`` only if it answers THE request that sent ``nonce``.

    Otherwise discard the client's connection pool and raise
    :class:`RpcResponseBindingError` (a transport error: retried once). The
    response must be keyed by the binding's pinned ``expect_service``.

    One exception: an error answer that echoes THIS request's nonce without a
    keyed binding is the provider refusing a request it could not
    authenticate (it keys only verified requests) — :class:`_RpcRefused`
    with its code as an unauthenticated diagnostic, never retried.
    """

    cfg = cfg if cfg is not None else _global_settings
    reason = rpc_binding.verify(
        cfg,
        response.headers,
        nonce=nonce,
        sent=sent,
        expect_service=expect_service,
        status_code=response.status_code,
        body=bytes(response.content),
    )
    if reason is None:
        return
    if reason == "unkeyed" and response.status_code >= 400:
        metrics.rpc_response_binding_rejected_total.labels(reason).inc()
        raise _RpcRefused(response.status_code, _refusal_code(response))
    metrics.rpc_response_binding_rejected_total.labels(reason).inc()
    _discard_pool(client)
    logger.warning(
        "Rejected an RPC response from %s that is not bound to its request "
        "(%s); the connection pool was discarded",
        peer,
        reason,
        extra={"event": "rpc.response_binding_rejected", "reason": reason},
    )
    raise _transport().RpcResponseBindingError(
        f"RPC response from {peer} is not bound to its request ({reason})"
    )


_REFUSAL_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _refusal_code(response: Any) -> str:
    """The error code of an unauthenticated refusal (diagnostic only)."""
    try:
        body = _unpack(response.content)
    except (ValueError, TypeError):  # msgpack errors are ValueErrors
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and _REFUSAL_CODE.fullmatch(code):
        return code
    return "rpc_refused"


async def _bound_get(
    client: Any,
    url: str,
    headers: dict[str, str],
    timeout: float,  # noqa: ASYNC109 - explicit per-call timeout is the API
    peer: str,
    expect_service: str | None = None,
    cfg: Any = None,
    signer: _MacSigner | None = None,
) -> Any:
    nonce = headers[_NONCE_HEADER] = _new_nonce()
    if signer is not None:
        signer.apply(headers, nonce)
    response = await client.get(url, headers=headers, timeout=timeout)
    _check_binding(
        client,
        response,
        nonce=nonce,
        sent=headers,
        peer=peer,
        expect_service=expect_service,
        cfg=cfg,
    )
    return response


_BOUND_HEADERS = frozenset(
    {
        b"x-sandloom-rpc-nonce",
        b"x-sandloom-rpc-auth",
        b"x-sandloom-rpc-service-signature",
        b"x-sandloom-rpc-context",
    }
)


#: Raw request header -> its name in :func:`rpc_binding.request_auth`.
_SIG_KEYS = {
    b"x-sandloom-rpc-service-signature": rpc_binding.SVC_SIG_HEADER,
    b"x-sandloom-rpc-auth": rpc_binding.REQ_MAC_HEADER,
}


def _binding_inputs(scope: Any) -> _BindingInputs | None:
    """What binding a response needs, from raw headers (``None``: no nonce)."""
    nonce: str | None = None
    sigs: dict[str, str] = {}
    seen: set[bytes] = set()
    for name, value in scope.get("headers") or ():
        if name in _BOUND_HEADERS:
            if name in seen:
                # Two nonces / tags: never bind (the verifier refuses it too).
                return None
            seen.add(name)
        if name == b"x-sandloom-rpc-nonce":
            nonce = _valid_nonce(value.decode("latin-1"))
        else:
            sig = _SIG_KEYS.get(name)
            if sig is not None:
                sigs[sig] = value.decode("latin-1")
    if nonce is None:
        return None
    return _BindingInputs(nonce, rpc_binding.request_auth(sigs))
