"""The request's bearer token: the one ``Authorization`` parser of the
kernel and the per-request memo of its verified claims."""

from __future__ import annotations

from typing import Any

from sandloom.core.auth import tokens as _tokens

# Attribute under which a per-request decode cache lives on ``request.state``.
# Several independent guards (auth, tenant scope, idempotency) verify the same
# bearer token on one request; caching the parsed payload keyed by the raw token
# means the cryptographic verify runs once instead of once per guard.
_JWT_CACHE_ATTR = "_sandloom_jwt_payload_cache"
#: ``request.state`` key of the request's ``(token, access claims)``: the
#: header is parsed and the token verified once per request.
_BEARER_ATTR = "_sandloom_bearer"


def _state_mapping(request: Any) -> dict[str, Any] | None:
    """The dict behind ``request.state`` (Starlette keeps it in
    ``scope["state"]``), read without ``State.__getattr__``'s raising miss;
    ``None`` for a request-like object without an ASGI scope."""
    scope = getattr(request, "scope", None)
    if type(scope) is not dict:
        return None
    state = scope.get("state")
    if state is None:
        state = scope["state"] = {}
    return state if type(state) is dict else None


def decode_token_cached(request: Any, token: str) -> dict[str, Any] | None:
    """:func:`decode_token`, but verified at most once per request.

    The parsed payload is memoised on ``request.state`` keyed by the raw token,
    so a request whose auth guard, tenant scope and idempotency check all read
    the same bearer token pays a single cryptographic verify. Falls back to a
    plain decode when no request state is available (workers/scripts).
    """
    cache: dict[str, dict[str, Any] | None] | None
    mapping = _state_mapping(request)
    if mapping is not None:
        cache = mapping.get(_JWT_CACHE_ATTR)
        if cache is None:
            cache = mapping[_JWT_CACHE_ATTR] = {}
    else:
        state = getattr(request, "state", None)
        if state is None:
            return _tokens.decode_token(token)
        cache = getattr(state, _JWT_CACHE_ATTR, None)
        if cache is None:
            cache = {}
            setattr(state, _JWT_CACHE_ATTR, cache)
    if token in cache:
        return cache[token]
    payload = _tokens.decode_token(token)
    cache[token] = payload
    return payload


def bearer_token(request: Any) -> str | None:
    """The raw token of an ``Authorization: Bearer <token>`` header, or ``None``.

    The ONE bearer-header parser of the kernel (auth guards, tenant scope and
    idempotency all go through it): case-insensitive scheme, surrounding
    whitespace stripped, an empty token is no token.
    """
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    auth = headers.get("authorization") or ""
    scheme, _, token = auth.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def bearer_claims(request: Any) -> dict[str, Any] | None:
    """Verified claims of the request's ACCESS bearer token, or ``None``.

    Verified once per request (:func:`decode_token_cached`). Only an access
    token counts (``token_type == "access"``): a refresh / reset / other token
    type presented as a bearer is never treated as a principal. Revocation is
    NOT checked here — the auth guards call :func:`ensure_token_not_revoked`.
    """
    return bearer_token_and_claims(request)[1]


def bearer_token_and_claims(
    request: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    """``(raw token, verified access claims)`` of the request's bearer token.

    :func:`bearer_claims` plus the raw token it verified (``None`` when there
    is no bearer, ``(token, None)`` when it does not verify as an ACCESS
    token), for callers that memoise derived values per verified token.
    """
    mapping = _state_mapping(request)
    if mapping is not None:
        found: tuple[str | None, dict[str, Any] | None] | None = mapping.get(
            _BEARER_ATTR
        )
        if found is not None:
            return found  # parsed once per request (subject binding, guards)
    token = bearer_token(request)
    if token is None:
        result: tuple[str | None, dict[str, Any] | None] = (None, None)
    else:
        claims = decode_token_cached(request, token)
        if not claims or claims.get("token_type") != "access":
            result = (token, None)
        else:
            result = (token, claims)
    if mapping is not None:
        mapping[_BEARER_ATTR] = result
    return result
