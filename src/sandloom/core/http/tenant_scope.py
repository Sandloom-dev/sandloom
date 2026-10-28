"""``TenantScope`` — bind the request's tenant from its verified access token.

The HTTP half of the tenant scope (:mod:`sandloom.core.tenant` holds the ORM guard
and the context it enforces): a FastAPI dependency that resolves the tenant
from the ``X-Tenant-Id`` selector and the signed ``tenant_id`` / ``org_id`` /
``tenant_ids`` claims, rejects a revoked token, and binds the tenant context
for the endpoint's whole scope. It reads the bearer through
``sandloom.core.auth``, so it lives in the auth & HTTP layer (ADR-0007);
``sandloom.core.di`` re-exports it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from sandloom.core.auth.bearer import bearer_claims, bearer_token_and_claims
from sandloom.core.auth.verification import ensure_token_not_revoked
from sandloom.core.tenant_context import _current_tenant


def TenantScope(  # dependency factory, reads like a type
    *,
    header: str = "X-Tenant-Id",
    claim_keys: tuple[str, ...] = ("tenant_id", "org_id"),
    required: bool = True,
) -> Any:
    """FastAPI dependency that binds the request's tenant for its whole scope.

    Resolves the tenant id from the ``X-Tenant-Id`` header, else a JWT claim
    (``tenant_id``/``org_id``). Binds the contextvar before the endpoint runs and
    resets it afterwards, so ``scoped()`` / automatic enforcement see it. With
    ``required`` (default) a missing tenant is a 400; otherwise it yields
    ``None`` and leaves the context unbound.

    Usage::
        async def handler(tenant: Annotated[UUID, TenantScope()]):
            ...
    """

    async def _dep(request: Request) -> AsyncGenerator[uuid.UUID | None, None]:
        token, claims = bearer_token_and_claims(request)
        if claims:
            # A token issued before a revocation (e.g. membership removal) must
            # not keep selecting a tenant from its stale ``tenant_ids`` claim.
            await ensure_token_not_revoked(request, claims)
        else:
            claims = {}
        tid = _resolve_tenant(request, header, claim_keys, claims, token)
        if tid is None:
            if required:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="tenant context required",
                )
            yield None
            return
        token_var = _current_tenant.set(tid)
        try:
            yield tid
        finally:
            _current_tenant.reset(token_var)

    # Read by create_app's gate-order check (SANDLOOM-B007).
    _dep.__sandloom_binds_tenant__ = True  # type: ignore[attr-defined]
    return Depends(_dep)


def _resolve_tenant(  # noqa: C901
    request: Request,
    header: str,
    claim_keys: tuple[str, ...],
    claims: dict[str, Any] | None = None,
    token: str | None = None,
) -> uuid.UUID | None:
    raw = request.headers.get(header)
    selected = _parse_uuid(raw) if raw else None
    if claims is None:
        token, claims = bearer_token_and_claims(request)
        claims = claims or {}
    granted, keyed, preferred = _claimed_tenants(claims, claim_keys, token)

    request_core = getattr(request.app.state, "core", None)
    request_settings = getattr(request_core, "settings", None)
    mode = getattr(request_settings, "tenancy_mode", "multi")
    canonical = getattr(request_settings, "single_tenant_id", None)

    if mode == "single":
        if canonical is None:  # Settings validation normally makes this impossible.
            raise RuntimeError(
                "TENANCY_MODE=single requires a configured SINGLE_TENANT_ID"
            )
        canonical = uuid.UUID(str(canonical))
        if selected is not None and selected != canonical:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="tenant does not match the configured tenant",
            )
        if (granted or keyed) and canonical not in granted and canonical not in keyed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="token tenant does not match the configured tenant",
            )
        return canonical

    if selected is not None:
        if selected not in granted and selected not in keyed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="tenant is not authorized by the access token",
            )
        return selected

    if preferred is not None:
        return preferred
    # No ``claim_keys`` claim (else ``preferred`` is set): ``granted`` is all.
    if len(granted) == 1:
        return next(iter(granted))
    if len(granted) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{header} is required when the token grants multiple tenants",
        )
    return None


def _tenant_claims(request: Request) -> dict[str, Any]:
    return bearer_claims(request) or {}


#: Bound of :data:`_parsed_tenant_ids` (as the verified-token cache).
_TENANT_IDS_MEMO_SIZE = 2048
#: verified access token -> its parsed ``tenant_ids`` claim. A verified token's
#: claims are immutable, so the parse (≈ 0.7 µs per id, up to
#: ``TENANT_CLAIMS_MAX_ORGANIZATIONS`` ids) runs once per token, not per
#: request. Only a fully valid list is remembered: a malformed (but signed)
#: entry is re-parsed — and answers ``400`` — on every request.
_parsed_tenant_ids: dict[str, frozenset[uuid.UUID]] = {}


def _granted_tenant_ids(values: Any, token: str | None) -> frozenset[uuid.UUID]:
    if not isinstance(values, (list, tuple, set)):
        return frozenset()
    if token is not None:
        cached = _parsed_tenant_ids.get(token)
        if cached is not None:
            return cached
    parsed = frozenset(_parse_uuid(str(value)) for value in values)
    if token is not None:
        if len(_parsed_tenant_ids) >= _TENANT_IDS_MEMO_SIZE:
            _parsed_tenant_ids.pop(next(iter(_parsed_tenant_ids)), None)
        _parsed_tenant_ids[token] = parsed
    return parsed


def _claimed_tenants(
    claims: dict[str, Any], claim_keys: tuple[str, ...], token: str | None = None
) -> tuple[frozenset[uuid.UUID], tuple[uuid.UUID, ...], uuid.UUID | None]:
    """``(tenant_ids grant, claim_keys tenants, preferred)`` of ``claims``.

    ``token`` — the verified access token ``claims`` were decoded from — keys
    the parse memo; pass ``None`` for claims of any other origin.
    """
    keyed: tuple[uuid.UUID, ...] = ()
    for key in claim_keys:
        value = claims.get(key)
        if value:
            keyed = (*keyed, _parse_uuid(str(value)))
    granted = _granted_tenant_ids(claims.get("tenant_ids", ()), token)
    return granted, keyed, keyed[0] if keyed else None


def _parse_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid tenant id",
        ) from exc


__all__ = ["TenantScope"]
