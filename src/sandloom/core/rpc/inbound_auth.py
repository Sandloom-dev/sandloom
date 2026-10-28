"""Provider-side authentication of an RPC request: pair-MAC verification,
the command signature, the replay guards.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NoReturn

from fastapi import status

import sandloom.core.infra.access as _access
from sandloom.core import rpc_identity, rpc_pair_mac
from sandloom.core.config.settings import settings as _global_settings
from sandloom.core.observability import metrics
from sandloom.core.rpc.binding import _CALLER_SCOPE_KEY
from sandloom.core.rpc.errors import _RpcContextError
from sandloom.core.rpc.wire import _CTX_HEADER, _NONCE_HEADER

logger = logging.getLogger("sandloom.core.rpc")


def _core_settings(core: Any) -> Any:
    return getattr(core, "settings", None) or _global_settings


def _presents_credential(headers: Any) -> bool:
    """Does the request present internal-RPC credentials at all?

    ``False`` -> the hidden 404 (the endpoint's existence is not revealed to
    a request carrying neither a pair-MAC tag nor a service signature). A
    request with only a signature is a downgrade attempt: it is verified as
    a MAC request and refused (``401 rpc_auth_required``).
    """
    return bool(
        headers.get(rpc_pair_mac.AUTH_HEADER)
        or headers.get(rpc_identity.SVC_SIG_HEADER) is not None
    )


_AUTH_LOG_INTERVAL = 60.0


_auth_logged: dict[tuple[str, str | None], float] = {}


def _auth_reject(exc: rpc_pair_mac.AuthError, caller: str | None = None) -> NoReturn:
    metrics.rpc_auth_rejected_total.labels(exc.reason).inc()
    _log_auth_rejection(exc.reason, caller)
    raise _RpcContextError(exc.status_code, exc.code, exc.message) from exc


def _log_auth_rejection(reason: str, caller: str | None = None) -> None:
    # Ids / reason only: never a key, tag or context. At most one line per
    # (reason, caller) per minute: unauthenticated junk cannot flood the log
    # (the counter keeps the exact rate).
    now = time.monotonic()
    log_key = (reason, caller)
    if now - _auth_logged.get(log_key, -_AUTH_LOG_INTERVAL) >= _AUTH_LOG_INTERVAL:
        if len(_auth_logged) >= 256:
            _auth_logged.clear()
        _auth_logged[log_key] = now
        logger.warning(
            "Refused an internal RPC request (%s, caller %s)",
            reason,
            caller or "unauthenticated",
            extra={"event": "rpc.auth_rejected", "reason": reason},
        )


async def _verify_pair_mac(
    cfg: Any,
    headers: Any,
    *,
    http_method: str,
    target: str,
    body: bytes,
    contract: str,
    method: str | None,
    command: Any,
    scope: Any = None,
) -> dict[str, Any]:
    """Authenticate a pair-MAC request (401), check it is fresh and new
    (401 / 503), authorize it (403); return the verified context.

    Order: tag over the raw bytes FIRST (nothing is decoded before it), then
    the context, the time window and the nonce guard (reachable only by an
    authenticated caller), then the allowlist.
    """
    header = _pair_mac_header(headers)
    nonce = headers.get(_NONCE_HEADER)
    raw_context = headers.get(_CTX_HEADER)
    try:
        caller = rpc_pair_mac.verify_request(
            cfg,
            str(header),
            http_method=http_method,
            target=target,
            nonce=nonce,
            context=raw_context,
            body=body,
        )
    except rpc_pair_mac.AuthError as exc:
        _auth_reject(exc)
    assert nonce is not None and raw_context is not None  # noqa: S101 - type narrowing: verified above
    if scope is not None:
        # A verified caller: its response (an error included) is pair-keyed.
        scope[_CALLER_SCOPE_KEY] = (caller.service, caller.kid)
    # Authorize BEFORE the nonce guard: a peer holding a key but no
    # allowlist entry must not be able to fill the replay tables.
    if not rpc_identity.authorize(cfg, caller.service, contract, method):
        what = f"{contract}.{method}" if method else contract
        _log_auth_rejection("caller_forbidden", caller.service)
        raise _RpcContextError(
            status.HTTP_403_FORBIDDEN,
            "rpc_caller_forbidden",
            f"service {caller.service!r} is not allowed to call {what}",
        )
    if (command() and rpc_pair_mac.commands_require_signature(cfg)) or (
        rpc_pair_mac.all_calls_require_signature(cfg)
    ):
        _verify_command_signature(
            cfg,
            headers,
            caller=caller.service,
            http_method=http_method,
            target=target,
            body=body,
            context=raw_context,
            nonce=nonce,
        )
    try:
        context = await _fresh_pair_mac_context(
            cfg, caller.service, nonce, raw_context, command
        )
    except rpc_pair_mac.AuthError as exc:
        _auth_reject(exc, caller.service)
    return context


def _pair_mac_header(headers: Any) -> str:
    """The request's single ``X-SANDLOOM-RPC-Auth`` tag (refuses a tenant header,
    repeated auth headers and a missing tag) — nothing is verified yet."""
    if headers.get("X-Tenant-Id") is not None:
        raise _RpcContextError(
            status.HTTP_400_BAD_REQUEST,
            "rpc_unsigned_tenant",
            "X-Tenant-Id is not accepted on internal RPC; the tenant is "
            "propagated in the signed RPC context",
        )
    if any(
        len(headers.getlist(name)) > 1
        for name in (
            rpc_pair_mac.AUTH_HEADER,
            _NONCE_HEADER,
            _CTX_HEADER,
            rpc_identity.SVC_SIG_HEADER,
        )
    ):
        # One value each: the binding and the verifier must never read two
        # different nonces / tags (ADR-0004 security review, finding 1).
        _auth_reject(
            rpc_pair_mac.AuthError(
                401,
                "rpc_auth_invalid",
                "malformed",
                "RPC request authentication failed",
            )
        )
    header: str | None = headers.get(rpc_pair_mac.AUTH_HEADER)
    if not header:
        _auth_reject(
            rpc_pair_mac.AuthError(
                401,
                "rpc_auth_required",
                "required",
                "internal RPC accepts only pair-MAC authenticated requests",
            )
        )
    return header


async def _fresh_pair_mac_context(
    cfg: Any, caller: str, nonce: str, raw_context: str, command: Any
) -> dict[str, Any]:
    """The authenticated caller's context, once it is addressed to this
    provider, fresh and its nonce new (raises :class:`rpc_pair_mac.AuthError`)."""
    me = rpc_identity.service_id(cfg)
    skew = rpc_pair_mac.max_skew(cfg)
    context = rpc_pair_mac.decode_context(raw_context)
    if context.get("svc", caller) != caller or context.get("aud", me) != me:
        raise rpc_pair_mac.AuthError(
            401,
            "rpc_auth_invalid",
            "malformed",
            "RPC request authentication failed",
        )
    iat = rpc_pair_mac.check_fresh(context.get("iat"), skew=skew)
    _observe_clock_skew(caller, iat, skew)
    ttl = rpc_pair_mac.replay_ttl(iat, skew=skew)
    seen = rpc_pair_mac.replay_guard(cfg).add(caller, nonce, ttl)
    if seen == "replay":
        raise rpc_pair_mac.AuthError(
            401, "rpc_auth_replayed", "replayed", "RPC request was already served"
        )
    if seen in ("full", "quota"):
        raise rpc_pair_mac.AuthError(
            503,
            "rpc_replay_unavailable",
            "replay_unavailable" if seen == "full" else "caller_quota",
            "RPC replay guard is full; retry later",
        )
    guard = getattr(cfg, "rpc_replay_guard", "commands")
    if guard == "all" or (guard == "commands" and command()):
        await _shared_nonce(
            cfg,
            str(me),
            caller,
            nonce,
            rpc_pair_mac.shared_replay_ttl(skew),
        )
    return context


def _verify_command_signature(
    cfg: Any,
    headers: Any,
    *,
    caller: str,
    http_method: str,
    target: str,
    body: bytes,
    context: str,
    nonce: str,
) -> None:
    """A COMMAND also needs the caller's Ed25519 signature (KCI mitigation).

    The signed context must carry THIS attempt's nonce (``n``): a holder of
    the provider's X25519 key could otherwise re-MAC a captured signed
    command under fresh nonces and slip past the nonce guard.

    The pair MAC proves "caller or this provider"; whoever holds this
    provider's X25519 key can compute it for any caller. The signature can
    only come from the caller's own Ed25519 key, which the provider never
    holds: a leaked provider key forges reads at most, never effects.
    """
    signature = headers.get(rpc_identity.SVC_SIG_HEADER)
    if not signature:
        _auth_reject(
            rpc_pair_mac.AuthError(
                401,
                "rpc_auth_signature_required",
                "signature_required",
                "command calls need the caller's service signature "
                "(RPC_COMMANDS_REQUIRE_SIGNATURE)",
            ),
            caller,
        )
    try:
        signer, signed = rpc_identity.verify(
            cfg,
            http_method=http_method,
            target=target,
            body=body,
            context=context,
            signature=str(signature),
        )
    except rpc_identity.ServiceIdentityError:
        _auth_reject(
            rpc_pair_mac.AuthError(
                401,
                "rpc_auth_invalid",
                "signature_invalid",
                "RPC request authentication failed",
            ),
            caller,
        )
    if signer != caller or signed.get("n") != nonce:
        _auth_reject(
            rpc_pair_mac.AuthError(
                401,
                "rpc_auth_invalid",
                "signature_invalid",
                "RPC request authentication failed",
            ),
            caller,
        )


_skew_logged: dict[str, float] = {}


def _observe_clock_skew(caller: str, iat: int, skew: int) -> None:
    """Count (and log, at most once a minute per caller) an accepted request
    whose issue time is off by more than half the window: clocks drifting
    towards ``RPC_AUTH_MAX_SKEW_SECONDS`` — which the shared nonce TTL
    (``3 * skew``) and every caller's acceptance rely on staying under."""
    drift = time.time() - iat
    if abs(drift) <= skew / 2:
        return

    metrics.rpc_auth_clock_skew_total.labels(caller).inc()
    now = time.monotonic()
    if now - _skew_logged.get(caller, -_AUTH_LOG_INTERVAL) >= _AUTH_LOG_INTERVAL:
        if len(_skew_logged) >= 256:
            _skew_logged.clear()
        _skew_logged[caller] = now
        logger.warning(
            "RPC caller %s's clock is %.0f s off this provider's (window %s s): "
            "fix NTP before it exceeds RPC_AUTH_MAX_SKEW_SECONDS",
            caller,
            drift,
            skew,
            extra={"event": "rpc.clock_skew", "caller": caller},
        )


_shared_nonce_warned: list[bool] = []


async def _shared_nonce(
    cfg: Any, provider: str, caller: str, nonce: str, ttl: int
) -> None:
    """``SET NX`` the nonce in the security cache (every replica / worker).

    A repeat is ``rpc_auth_replayed``; a cache outage fails closed in prod
    (``503 rpc_replay_unavailable``) and open elsewhere (counted, logged once).
    """

    try:
        created = await _access.security_cache().add(
            rpc_pair_mac.shared_replay_key(provider, caller, nonce),
            "1",
            ttl_seconds=ttl,
        )
    except Exception as exc:  # any cache failure
        if getattr(cfg, "is_prod", False):
            raise rpc_pair_mac.AuthError(
                503,
                "rpc_replay_unavailable",
                "replay_unavailable",
                "RPC replay guard is unavailable; retry later",
            ) from exc

        metrics.rpc_auth_rejected_total.labels("fail_open").inc()
        if not _shared_nonce_warned:
            _shared_nonce_warned.append(True)
            logger.warning(
                "RPC shared nonce guard unavailable (%s); accepted outside "
                "prod (fail open)",
                exc.__class__.__name__,
            )
        return
    if not created:
        raise rpc_pair_mac.AuthError(
            401, "rpc_auth_replayed", "replayed", "RPC request was already served"
        )
