"""``module_router()`` (L1, spec §2.3, §5.1).

The module's router: a derived prefix (``/api/v1/<module>``) and tags, and,
per route at decoration time, user authentication and the tenant binding:

- a route that declares ``public()`` or a service principal
  (``CurrentService``, an authenticator marked ``kind="service"``) gets
  nothing;
- every other route gets ``Depends(current_context)`` and, unless its
  binding is ``none``, ``TenantScope(required=...)`` (skipped when the route
  already binds the tenant itself, e.g. ``TenantId``).

There are no router-level auth dependencies, so a route never has to remove
inherited authentication. The L2 equivalent is a plain ``APIRouter`` with
those per-route dependencies, registered with ``core.include_router``. The
kernel guarantees (deny-by-default, sessions, re-checks, idempotency, write
admission) apply to any router, this one included.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Iterator, Sequence
from enum import Enum
from typing import Any, Literal

from fastapi import APIRouter, params
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.types import DecoratedCallable

from sandloom.sdk._decl._owner import (
    OWNER_ATTR,
    caller_module,
    module_package,
    short_name,
)
from sandloom.sdk._decl.errors import DeclarationError
from sandloom.sdk._prim.authz import current_context
from sandloom.sdk._prim.data import TenantScoped
from sandloom.sdk._prim.web import TenantScope, current_service

TenantBinding = Literal["derive", "required", "optional", "none"]
RouteTenant = Literal["required", "optional", "none"] | None

_BINDINGS = ("derive", "required", "optional", "none")
#: What a ``public()`` marker carries (the kernel's ``PublicDecl``).
PUBLIC_ATTR = "__sandloom_public__"
#: The kind ``sandloom.sdk.web.authenticates`` stamps on an authenticator.
AUTHENTICATES_ATTR = "sandloom_authenticates"


class RoutePolicyError(DeclarationError):
    """A route is both ``public()`` and authenticated (SANDLOOM-B008)."""

    sandloom_code = "SANDLOOM-B008"


def _calls(dependant: Dependant) -> Iterator[Any]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _calls(sub)


def _route_calls(
    path: str, endpoint: Callable[..., Any], declared: Sequence[Any]
) -> list[Any]:
    """Every dependency callable a route solves (declared and in the signature)."""
    calls: list[Any] = []
    for dep in declared:
        call = getattr(dep, "dependency", None)
        if call is not None:
            calls.append(call)
            calls.extend(_calls(get_dependant(path=path, call=call)))
    calls.extend(_calls(get_dependant(path=path, call=endpoint)))
    return calls


def _is_service_authenticator(call: Any) -> bool:
    return (
        call is current_service or getattr(call, AUTHENTICATES_ATTR, None) == "service"
    )


def _tenant_models(package: str) -> bool:
    """``package`` declares a ``TenantScoped`` model (its ``models`` first)."""
    dotted = f"{package}.models"
    try:
        found = importlib.util.find_spec(dotted) is not None
    except ModuleNotFoundError:
        found = False
    if found:
        importlib.import_module(dotted)
    stack = list(TenantScoped.__subclasses__())
    while stack:
        cls = stack.pop()
        module = getattr(cls, "__module__", "")
        if module == package or module.startswith(package + "."):
            return True
        stack.extend(cls.__subclasses__())
    return False


class ModuleRouter(APIRouter):
    """An ``APIRouter`` whose route decorators also take
    ``tenant: Literal["required", "optional", "none"] | None = None``.

    Built by :func:`module_router`; collected by discovery wherever it is
    defined in the module's ``api`` package.
    """

    def __init__(
        self,
        *,
        sandloom_owner: str,
        sandloom_tenant: TenantBinding = "derive",
        prefix: str = "",
        tags: list[str | Enum] | None = None,
        dependencies: Sequence[params.Depends] | None = None,
        **kwargs: Any,
    ) -> None:
        if sandloom_tenant not in _BINDINGS:
            raise DeclarationError(
                f"module_router(tenant={sandloom_tenant!r}): expected "
                f"one of {_BINDINGS}"
            )
        super().__init__(prefix=prefix, tags=tags, dependencies=dependencies, **kwargs)
        setattr(self, OWNER_ATTR, sandloom_owner)
        self.sandloom_tenant: TenantBinding = sandloom_tenant
        self._sandloom_tenant_models: bool | None = None

    # -- policy ------------------------------------------------------------
    def _derived_binding(self) -> Literal["required", "optional", "none"]:
        if self.sandloom_tenant != "derive":
            return self.sandloom_tenant
        if self._sandloom_tenant_models is None:
            owner = str(getattr(self, OWNER_ATTR))
            self._sandloom_tenant_models = _tenant_models(module_package(owner))
        return "required" if self._sandloom_tenant_models else "none"

    def route_policy(
        self,
        path: str,
        endpoint: Callable[..., Any],
        dependencies: Sequence[params.Depends] | None,
        tenant: RouteTenant,
    ) -> list[params.Depends]:
        """The dependencies :meth:`add_api_route` adds in front of the route's."""
        if tenant not in (None, "required", "optional", "none"):
            raise DeclarationError(
                f"route {path!r}: tenant={tenant!r} is not required, optional or none"
            )
        declared = [*self.dependencies, *(dependencies or ())]
        calls = _route_calls(path, endpoint, declared)
        public = any(hasattr(call, PUBLIC_ATTR) for call in calls)
        service = any(_is_service_authenticator(call) for call in calls)
        if public and (service or current_context in calls):
            raise RoutePolicyError(
                f"route {path!r} ({getattr(endpoint, '__qualname__', endpoint)}) "
                "declares public() and authenticates its caller",
                fix="drop public() for an authenticated route, or drop the "
                "CurrentUser / CurrentService parameter for a public one",
            )
        if public or service:
            return []
        injected: list[params.Depends] = [params.Depends(current_context)]
        binding = tenant or self._derived_binding()
        binds = any(getattr(call, "__sandloom_binds_tenant__", False) for call in calls)
        if binding != "none" and not binds:
            injected.append(TenantScope(required=binding == "required"))
        return injected

    # -- FastAPI surface ------------------------------------------------------
    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        tenant: RouteTenant = None,
        dependencies: Sequence[params.Depends] | None = None,
        **kwargs: Any,
    ) -> None:
        injected = self.route_policy(path, endpoint, dependencies, tenant)
        super().add_api_route(
            path, endpoint, dependencies=[*injected, *(dependencies or ())], **kwargs
        )

    def api_route(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        def decorator(func: DecoratedCallable) -> DecoratedCallable:
            self.add_api_route(path, func, tenant=tenant, **kwargs)
            return func

        return decorator

    def _verb(
        self, method: str, path: str, tenant: RouteTenant, kwargs: dict[str, Any]
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self.api_route(path, methods=[method], tenant=tenant, **kwargs)

    def get(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("GET", path, tenant, kwargs)

    def put(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("PUT", path, tenant, kwargs)

    def post(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("POST", path, tenant, kwargs)

    def delete(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("DELETE", path, tenant, kwargs)

    def options(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("OPTIONS", path, tenant, kwargs)

    def head(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("HEAD", path, tenant, kwargs)

    def patch(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("PATCH", path, tenant, kwargs)

    def trace(
        self, path: str, *, tenant: RouteTenant = None, **kwargs: Any
    ) -> Callable[[DecoratedCallable], DecoratedCallable]:
        return self._verb("TRACE", path, tenant, kwargs)


def module_router(
    *,
    prefix: str | None = None,
    tags: Sequence[str] | None = None,
    tenant: TenantBinding = "derive",
    dependencies: Sequence[params.Depends] = (),
) -> ModuleRouter:
    """The module's router (spec §5.1), in ``<module>/api.py``::

        router = module_router()

        @router.post("/", status_code=201)
        async def create_note(body: NoteIn, user: CurrentUser) -> NoteOut: ...

    ``prefix`` defaults to ``/api/v1/<module>`` and ``tags`` to
    ``[<module>]``. ``tenant="derive"`` binds the tenant (``required``) when
    the module declares a ``TenantScoped`` model, else none; a route's own
    ``tenant=`` overrides. The binding scopes data only; it never selects a
    billing account.
    """
    owner = caller_module(1)
    module = short_name(module_package(owner))
    return ModuleRouter(
        sandloom_owner=owner,
        sandloom_tenant=tenant,
        prefix=f"/api/v1/{module}" if prefix is None else prefix,
        tags=list(tags) if tags is not None else [module],
        dependencies=list(dependencies),
    )


__all__ = ["ModuleRouter", "RoutePolicyError", "TenantBinding", "module_router"]
