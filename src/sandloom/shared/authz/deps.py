"""FastAPI integration for the authorization algebra.

Entry points:

- :func:`current_context` — the single dependency that decodes the access token
  into a :class:`UserContext`, caching it on ``request.state`` so a request with
  several guards decodes the token only once.
- :func:`require` — turn a policy into a dependency for ``dependencies=[...]``
  (no request resource).
- :func:`authorize_resource` — load a resource by path id, authorize, and return
  the loaded object to the endpoint (single fetch, lazy: the resource is loaded
  only if a cheap claim atom hasn't already decided the policy).
- :func:`authorize` — inline check over an already-loaded resource.

A denial raises :class:`~sandloom.shared.authz.results.AccessDenied` (an
``HTTPException``) whose status is the decision's own ``http_status`` —
402 for payment/quota, 403 for forbidden, 501 when a contract is absent in this
deployment, 500 for a misconfigured policy — and whose body carries the
decision's ``reason`` as ``detail`` and its ``code``. A ``status_code`` argument
is only an explicit override for *definite* denials; an indeterminate
(misconfiguration / unavailable) decision always keeps its own status.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.di import (
    authenticates,
    decode_token_cached,
    ensure_token_not_revoked,
    get_db,
    sensitive_effect,
)
from sandloom.shared.authz.account import bind_authz_request
from sandloom.shared.authz.algebra import TIER_CLAIM, Requirement
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.enforce import SOURCE_HTTP, decide, record_denial
from sandloom.shared.authz.results import AccessDecision, AccessDenied
from sandloom.shared.subject import UserContext

_security = HTTPBearer(auto_error=False)
_STATE_KEY = "_authz_user_context"


@authenticates
async def current_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> UserContext:
    """Authenticated subject from the Bearer access token (cached per request)."""
    # ``request.state`` is a view of ``scope["state"]``; reading the dict skips
    # State.__getattr__'s raising miss on the first guard of every request.
    state = _state_dict(request)
    cached = (
        state.get(_STATE_KEY)
        if state is not None
        else getattr(request.state, _STATE_KEY, None)
    )
    if isinstance(cached, UserContext):
        return cached
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token_cached(request, credentials.credentials)
    if not payload:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    if payload.get("token_type") != "access":
        # Reject any other token kind signed with the same key.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    if payload.get("iat") is None:
        # The issuer (core.security.create_access_token) always stamps iat; a
        # token without it was not minted by the platform's issuer path.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")
    try:
        subject = UserContext(**payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims"
        ) from exc
    # Framework-wide revocation: a token issued before its subject's sessions
    # were revoked (deactivation, password reset, role/membership change) is a
    # 401 here, so every claims-only ``require(...)`` honours it too.
    await ensure_token_not_revoked(request, payload)
    if state is not None:
        state[_STATE_KEY] = subject
    else:
        setattr(request.state, _STATE_KEY, subject)
    bind_authz_request(request, subject)
    return subject


def _state_dict(request: Any) -> dict[str, Any] | None:
    """The dict behind ``request.state`` (``scope["state"]``), or ``None`` for a
    request-like object without an ASGI scope (attribute access then)."""
    scope = getattr(request, "scope", None)
    if type(scope) is not dict:
        return None
    state = scope.get("state")
    if state is None:
        state = scope["state"] = {}
    return state if type(state) is dict else None


def _denied(decision: AccessDecision, status_code: int | None) -> AccessDenied:
    """The HTTP error for a denial: the decision's status unless overridden.

    The override applies to definite denials only — a 501/500 indeterminate
    decision must never be relabelled (e.g. as 402) by a caller's override.
    """
    override = None if decision.indeterminate else status_code
    return AccessDenied(decision, status_code=override)


def _needs_db(policy: Requirement) -> bool:
    """True when the policy may reach a resource/live atom that uses ``ctx.db``."""
    return policy.tier > TIER_CLAIM


_GateFn = Callable[..., Awaitable[UserContext]]


def require(
    policy: Requirement,
    *,
    status_code: int | None = None,
) -> Callable[..., Awaitable[UserContext]]:
    """Return a dependency enforcing ``policy`` (no request resource).

    Use in ``dependencies=[Depends(require(...))]`` or as a parameter when the
    endpoint wants the resulting :class:`UserContext`. A denial answers with the
    decision's own status (e.g. ``Subscribed(of=Account.user())`` → 402, a
    missing contract → 501);
    ``status_code`` is an explicit override for definite denials.

    A claims-only policy (every atom tier 1) never opens a database session /
    Unit of Work; only policies that may reach a live atom depend on
    ``get_db``.

    Raises ``ValueError`` at wiring time if ``policy`` contains a resource-scoped
    atom (e.g. ``Owner``): ``require`` has no request resource, so such a policy
    belongs in :func:`authorize_resource` / :func:`authorize`.
    """
    if policy.mentions_resource():
        raise ValueError(
            "require() cannot evaluate a resource-scoped policy (e.g. Owner); "
            "use authorize_resource(...) or authorize(...) instead"
        )

    async def _check(
        request: Request, subject: UserContext, db: AsyncSession | None
    ) -> UserContext:
        ctx = AuthzContext(subject, request=request, db=db, source=SOURCE_HTTP)
        decision = await decide(policy, ctx)
        if not decision.allowed:
            record_denial(ctx, decision)
            raise _denied(decision, status_code)
        return subject

    if not _needs_db(policy):

        async def _claims_guard(
            request: Request,
            subject: UserContext = Depends(current_context),
        ) -> UserContext:
            return await _check(request, subject, None)

        return _tag_gate(_claims_guard, policy)

    async def _guard(
        request: Request,
        subject: UserContext = Depends(current_context),
        db: AsyncSession = Depends(get_db, scope="function"),
    ) -> UserContext:
        return await _check(request, subject, db)

    return _tag_gate(_guard, policy)


def _tag_gate(fn: _GateFn, policy: Requirement) -> _GateFn:
    """Record the org path params ``policy`` may need (read by create_app's
    gate-order check, SANDLOOM-B007: the gate must not run before TenantScope())."""
    fn.__sandloom_org_params__ = policy.org_params()  # type: ignore[attr-defined]
    # Read at wiring time: a module's derived ``consumes`` (Requirement.contracts).
    fn.__sandloom_policy__ = policy  # type: ignore[attr-defined]
    if "admin" in policy.risks():
        # ADR-0009 item 6: an admin gate carries its risk
        return sensitive_effect("admin")(fn)
    return fn


async def authorize(
    resource: Any,
    policy: Requirement,
    *,
    ctx: UserContext,
    request: Request | None = None,
    db: AsyncSession | None = None,
    status_code: int | None = None,
) -> None:
    """Inline check over an already-loaded ``resource``; raises on failure.

    The escape hatch for cases that don't fit :func:`authorize_resource`
    (multiple resources, ownership known only after a non-trivial query, authz
    interleaved with business logic). The endpoint owns the single fetch.
    Raises :class:`AccessDenied` with the decision's status (or the explicit
    ``status_code`` override for a definite denial).
    """
    az = AuthzContext(
        ctx, request=request, db=db, resource=resource, source=SOURCE_HTTP
    )
    decision = await decide(policy, az)
    if not decision.allowed:
        record_denial(az, decision)
        raise _denied(decision, status_code)


def authorize_resource(
    *,
    loader: Callable[[Any, AsyncSession], Awaitable[Any]],
    by: str,
    policy: Requirement,
    cast: Callable[[str], Any] | None = None,
    not_found_detail: str = "Not found",
    status_code: int | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Load a resource by path id, authorize, and return it to the endpoint.

    Args:
        loader: ``async (id_value, db) -> entity | None`` — the module supplies
            its own loader (keeps ``shared`` free of ORM imports).
        by: name of the path parameter holding the resource id.
        policy: requirement to enforce; resource atoms (``Owner``) read the
            loaded entity.
        cast: optional converter applied to the raw path value before ``loader``
            (e.g. ``UUID``). Defaults to passing the raw string.
        not_found_detail: 404 message when the loader returns ``None``.
        status_code: explicit status override for a definite denial (default:
            the decision's own status).

    The resource is fetched **once**: claim atoms evaluate first, so a failing
    cheap check raises without ever loading; otherwise the entity loaded for
    the ownership check is the same one returned to the endpoint.
    """

    async def _guard(
        request: Request,
        subject: UserContext = Depends(current_context),
        db: AsyncSession = Depends(get_db, scope="function"),
    ) -> Any:
        raw_id = request.path_params.get(by)
        if raw_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Missing path parameter '{by}'",
            )
        try:
            resource_id = cast(raw_id) if cast is not None else raw_id
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid '{by}'",
            ) from exc

        async def _load(_az: AuthzContext) -> Any:
            entity = await loader(resource_id, db)
            if entity is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_detail)
            return entity

        az = AuthzContext(
            subject, request=request, db=db, loader=_load, source=SOURCE_HTTP
        )
        decision = await decide(policy, az)
        if not decision.allowed:
            record_denial(az, decision)
            raise _denied(decision, status_code)
        return await az.get_resource()

    _guard.__sandloom_policy__ = policy  # type: ignore[attr-defined]
    return _guard
