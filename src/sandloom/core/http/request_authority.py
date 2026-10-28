"""Isolate authorization/transaction context at each HTTP request boundary."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, cast

from fastapi import FastAPI

from sandloom.core import contract_context as contract_vars
from sandloom.core import tenant_context
from sandloom.core.auth import marks
from sandloom.core.unit_of_work import scope as uow_scope

#: ``(outer, request, contract)`` context variables of the boundary, each a
#: tuple of ``(variable, value)`` in set order; resolved on first use.
_VARS: tuple[Any, Any, Any] | None = None


def _boundary_vars() -> tuple[Any, Any, Any]:
    """The variables :class:`RequestAuthorityMiddleware` isolates, in order.

    ``request`` mirrors ``unit_of_work._isolated_request_authority_scope`` and
    ``contract`` ``contract_context._isolated_contract_request_scope`` exactly (same
    variables, same values, same order); a test pins the equivalence.
    """
    global _VARS
    if _VARS is None:
        outer: tuple[tuple[ContextVar[Any], Any], ...] = ((marks._issue_floors, None),)
        request: tuple[tuple[ContextVar[Any], Any], ...] = (
            (uow_scope._request_scope, None),
            (uow_scope._admission, None),
            (uow_scope._current_uow_session, None),
            (uow_scope._active_uow_sessions, ()),
            (uow_scope._read_only_scope, None),
            (tenant_context._current_tenant, None),
            (tenant_context._bypass, False),
        )
        contract: tuple[tuple[ContextVar[Any], Any], ...] = (
            (contract_vars._contract_session_var, None),
            (contract_vars._contract_session_override, None),
            (contract_vars._contract_target, None),
            (contract_vars._contract_reads_var, None),
            (contract_vars._read_scope_var, None),
        )
        _VARS = (outer, request, contract)
    return _VARS


class RequestAuthorityMiddleware:
    """A new HTTP request never shares its caller's authority / transaction.

    Sets every boundary variable to its fresh value and restores the caller's
    on exit — also for nested in-process ASGI calls, so it never skips a set
    because a variable already holds the default. Unrolled (no generator
    context managers): the same sets and resets, in the same order, as
    ``_isolated_request_authority_scope`` inside the issue-floor reset.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        outer, request, contract = _boundary_vars()
        ((floors, floors_value),) = outer
        issue_floors = floors.set(floors_value)
        try:
            request_tokens = [(var, var.set(value)) for var, value in request]
            try:
                contract_tokens = [(var, var.set(value)) for var, value in contract]
                try:
                    await self.app(scope, receive, send)
                finally:
                    for var, token in reversed(contract_tokens):
                        var.reset(token)
            finally:
                for var, token in reversed(request_tokens):
                    var.reset(token)
        finally:
            floors.reset(issue_floors)


def install_request_authority(app: FastAPI) -> None:
    """Keep one boundary outside regular HTTP and internal RPC middleware."""
    app.user_middleware = [
        item
        for item in app.user_middleware
        if cast(Any, item.cls) is not RequestAuthorityMiddleware
    ]
    app.add_middleware(RequestAuthorityMiddleware)
