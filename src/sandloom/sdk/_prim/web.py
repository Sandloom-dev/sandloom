"""New L2 route primitives of ``sandloom.sdk.web`` (spec §2.3, §4, §5.1).

Route deviations, the caller types and the operation-intent primitives,
re-exported from the kernel's public surface (``sandloom.core.di``): model E's
markers (``public``, ``no_idempotency``, ``replay_safe``, ``sensitive``,
``operation_intent``, ``writes``, ``allow_multi_db_write``) are no-op
dependencies the kernel reads off the route at startup and records in the
route snapshot; ``at_commit``, ``external_call``, ``release_check`` and
``request_operation_key`` are the unit and intent runtime (H1, WS-A);
``authorizes`` marks a custom authorization dependency the route snapshot
records (ADR-0009).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar, overload
from uuid import UUID

from fastapi import Depends

from sandloom.core.di import (
    DbSession,
    ResolvedAccount,
    TenantScope,
    allow_multi_db_write,
    at_commit,
    authorizes,
    external_call,
    no_idempotency,
    operation_intent,
    public,
    release_check,
    replay_safe,
    request_operation_key,
    sensitive,
    writes,
)
from sandloom.core.di import authenticates as _kernel_authenticates
from sandloom.shared.authz import current_context
from sandloom.shared.guards.service import current_service
from sandloom.shared.subject import UserContext

F = TypeVar("F", bound=Callable[..., Any])

#: The authenticated caller: the existing ``UserContext`` (roles,
#: permissions, ``claims``, ``claim()``), resolved by ``current_context``.
CurrentUser = Annotated[UserContext, Depends(current_context)]
#: A service principal (``SOURCE_INTERNAL``, the internal token); routes using
#: it get no user authentication injected by ``module_router()``.
CurrentService = Annotated[UserContext, Depends(current_service)]
#: The bound tenant of the request (``TenantScope()``).
TenantId = Annotated[UUID, TenantScope()]

#: The attribute a ``public()`` marker carries (the kernel's ``PublicDecl``:
#: ``reason``, ``issues_tokens``); ``module_router()`` reads it to inject
#: nothing on a public route.
PUBLIC_ATTR = "__sandloom_public__"
#: The attribute :func:`authenticates` stamps with the authenticator's kind
#: (``"user"`` / ``"service"``), so ``module_router()`` can tell a service
#: route without reading the kernel's registry.
AUTHENTICATES_ATTR = "sandloom_authenticates"
setattr(current_service, AUTHENTICATES_ATTR, "service")


@overload
def authenticates(fn: F, *, kind: Literal["user", "service"] = "user") -> F: ...
@overload
def authenticates(
    fn: None = None, *, kind: Literal["user", "service"] = "user"
) -> Callable[[F], F]: ...
def authenticates(
    fn: F | None = None, *, kind: Literal["user", "service"] = "user"
) -> F | Callable[[F], F]:
    """Mark ``fn`` (a FastAPI dependency) as authenticating the caller.

    ``@authenticates`` (a user subject) or ``@authenticates(kind="service")``
    (a system credential). Deny-by-default (ADR-0009, SANDLOOM-B008): every API
    route resolves an authenticator or declares ``public(reason=...)``; a
    ``module_router()`` route with a service authenticator gets no user
    authentication injected.
    """

    def _mark(target: F) -> F:
        marked = _kernel_authenticates(target, kind=kind)
        setattr(marked, AUTHENTICATES_ATTR, kind)
        return marked

    return _mark if fn is None else _mark(fn)


__all__ = [
    "CurrentService",
    "CurrentUser",
    "DbSession",
    "ResolvedAccount",
    "TenantId",
    "TenantScope",
    "allow_multi_db_write",
    "at_commit",
    "authenticates",
    "authorizes",
    "current_service",
    "external_call",
    "no_idempotency",
    "operation_intent",
    "public",
    "release_check",
    "replay_safe",
    "request_operation_key",
    "sensitive",
    "writes",
]
